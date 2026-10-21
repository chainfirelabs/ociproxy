"""Public-v2 pull-only replication, with per-peer machine credentials.

Only the sanitized public catalog, public manifests and referenced public blobs
are transferred. Accounts, tokens, private source definitions and local cache
content never participate. Legacy push/ingest and account replay are disabled.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .isolation import PROTOCOL
from .store import SOURCE_KIND, ChangeEvent, ManifestRecord, Store

log = logging.getLogger("containerproxy.replication")

TAIL_SERIAL_KEY = "tail_serial"
TAIL_PRIMARY_KEY = "tail_primary"
# Events per /replication/v2/changelog response (Store.changelog_since's default).
CHANGELOG_BATCH = 500


class PrimaryClient:
    """Replica-side client for the primary's /internal endpoints."""

    def __init__(
        self, base_url: str, secret: str, http: httpx.AsyncClient, node_id: str = ""
    ):
        self.base_url = base_url
        self.http = http
        self._headers = {"Authorization": f"Bearer {secret}", "X-ContainerProxy-Peer": node_id}
        self.sync_generation = ""
        if node_id:
            # Lets the primary attribute every request (liveness + pulls)
            # to this specific replica, not just to "some internal caller".
            self._headers["X-ContainerProxy-Node"] = node_id

    async def register(self, node_id: str, advertised_url: str) -> None:
        resp = await self.http.post(
            f"{self.base_url}/replication/v2/register",
            json={"node_id": node_id, "base_url": advertised_url, "protocol": PROTOCOL},
            headers=self._headers,
        )
        resp.raise_for_status()

    async def catalog(self) -> dict:
        response = await self.http.get(f"{self.base_url}/replication/v2/catalog", headers=self._headers)
        response.raise_for_status()
        data = response.json()
        if data.get("protocol") != PROTOCOL:
            raise ValueError("incompatible replication catalog")
        return data

    async def publish(self, kind: str, name: str, data: dict, *, delete=False) -> None:
        response = await self.http.post(f"{self.base_url}/replication/v2/sources",
            headers=self._headers, json={**data, "kind": kind, "name": name,
                                        "action": "delete" if delete else "save"})
        response.raise_for_status()

    async def changelog_since(
        self, serial: int, wait: float = 25.0
    ) -> tuple[list[ChangeEvent], int, str | None]:
        """Returns (events, primary's current serial, primary's node id)."""
        resp = await self.http.get(
            f"{self.base_url}/replication/v2/changelog",
            params={"since": serial, "wait": wait, "sync_generation": self.sync_generation},
            headers=self._headers,
            timeout=wait + 15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("protocol") != PROTOCOL:
            raise ValueError("incompatible replication feed")
        self.scanned = int(data["scanned"])
        self.sync_generation = data.get("sync_generation", "")
        events = [
            ChangeEvent(
                serial=e["serial"],
                kind=e["kind"],
                digest=e["digest"],
                upstream=e.get("upstream"),
                repo=e.get("repo"),
                reference=e.get("reference"),
                bytes=base64.b64decode(e["bytes_b64"]) if e.get("bytes_b64") else None,
                media_type=e.get("media_type"),
            )
            for e in data["events"]
        ]
        return events, data["current"], data.get("node_id")

    async def pull_manifest(self, upstream: str, repo: str, reference: str) -> ManifestRecord | None:
        resp = await self.http.get(
            f"{self.base_url}/replication/v2/pull/manifest",
            params={"upstream": upstream, "repo": repo, "reference": reference},
            headers=self._headers,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return ManifestRecord(
            upstream=resp.headers.get("x-containerproxy-upstream", upstream),
            repo=resp.headers.get("x-containerproxy-repo", repo),
            reference=reference,
            digest=resp.headers["docker-content-digest"],
            media_type=resp.headers["content-type"].split(";")[0].strip(),
            bytes=resp.content,
        )

    async def pull_tags(self, upstream: str, repo: str) -> list[str] | None:
        resp = await self.http.get(
            f"{self.base_url}/replication/v2/pull/tags",
            params={"upstream": upstream, "repo": repo},
            headers=self._headers,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return [t for t in resp.json().get("tags", []) if isinstance(t, str)]

    async def open_blob(self, digest: str, upstream: str | None = None, repo: str | None = None) -> httpx.Response:
        params = {}
        if upstream:
            params["upstream"] = upstream
        if repo:
            params["repo"] = repo
        req = self.http.build_request(
            "GET", f"{self.base_url}/replication/v2/pull/blob/{digest}",
            params=params, headers={**self._headers, "Accept-Encoding": "identity"},
        )
        resp = await self.http.send(req, stream=True)
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            from .resolver import ResolveError

            raise ResolveError(resp.status_code, f"primary blob {digest}: {body[:200]!r}")
        return resp


def describe_error(e: Exception) -> str:
    """Readable one-liner for a log: httpx timeouts have an empty message,
    so name the error type and the URL it was trying."""
    text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    if isinstance(e, httpx.RequestError):
        try:
            text += f" ({e.request.method} {e.request.url.copy_with(query=None)})"
        except RuntimeError:  # no request attached
            pass
    return text


def event_to_json(ev: ChangeEvent) -> dict[str, Any]:
    d: dict[str, Any] = {
        "serial": ev.serial,
        "kind": ev.kind,
        "digest": ev.digest,
        "upstream": ev.upstream,
        "repo": ev.repo,
        "reference": ev.reference,
        "media_type": ev.media_type,
    }
    if ev.bytes is not None:
        d["bytes_b64"] = base64.b64encode(ev.bytes).decode()
    return d


class ReplicaTail:
    """Runs inside a replica: registers, heartbeats, tails the changelog."""

    def __init__(
        self,
        store: Store,
        client: PrimaryClient,
        node_id: str,
        advertised_url: str,
        fetch_blob: Callable[[str], Awaitable[Path]],
        local_sources=None,
    ):
        self.store = store
        self.client = client
        self.node_id = node_id
        self.advertised_url = advertised_url
        # Joins any in-flight download of the same blob (client pull, ingest).
        self.fetch_blob = fetch_blob
        self.local_sources = local_sources
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.serial = int(store.get_meta(TAIL_SERIAL_KEY) or 0)
        # Primary's serial when it last had nothing newer for us: we're
        # caught up to it even if that serial's event was compacted away.
        self.caught_up_to = 0
        self.primary_id = store.get_meta(TAIL_PRIMARY_KEY)
        self.connected = False
        self.last_contact_at: float | None = None
        self.primary_serial: int | None = None
        self._sync_generation = ""
        self._sync_lock = asyncio.Lock()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def request_sync(self) -> None:
        """Interrupt polling or retry backoff and refresh from the saved position."""
        async with self._sync_lock:
            if self._stop.is_set():
                raise RuntimeError("replication is stopping")
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self.connected = False
            self.start()

    async def _refresh_catalog(self) -> None:
        catalog = await self.client.catalog()
        await asyncio.to_thread(self.store.apply_source_event, "config", json.dumps(catalog).encode())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.connected = False

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.client.register(self.node_id, self.advertised_url)
                # Explicitly shared replica config sources may be proposed by
                # peers granted publication permission. Never send credentials.
                if self.local_sources is not None:
                    from .sources import config_snapshot
                    proposal = config_snapshot(type("Local", (), {
                        "upstreams": self.local_sources.local_upstreams,
                        "helm_repos": self.local_sources.local_helm})())
                    known = await self.client.catalog()
                    for kind, key in (("oci", "upstreams"), ("helm", "helm")):
                        for source in proposal[key]:
                            same = next((entry for entry in known.get(key, []) if entry["name"] == source["name"]), None)
                            if same and all(same.get(k) == v for k, v in source.items()):
                                continue
                            try:
                                await self.client.publish(kind, source["name"], {**source, "configured": True})
                            except httpx.HTTPStatusError as exc:
                                log.warning("public config publication refused (%d)", exc.response.status_code)
                await self._refresh_catalog()
                self.connected = True
                self.last_contact_at = time.time()
                backoff = 1.0
                await self._tail()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.connected = False
                log.warning("replica tail error: %s (retry in %.0fs)", describe_error(e), backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    def _save(self) -> None:
        self.store.set_meta(TAIL_SERIAL_KEY, str(self.serial))

    async def _tail(self) -> None:
        while not self._stop.is_set():
            events, current, primary_id = await self.client.changelog_since(self.serial, wait=25)
            self.connected = True
            self.last_contact_at = time.time()
            self.primary_serial = current
            generation = getattr(self.client, "sync_generation", "")
            if generation != self._sync_generation:
                await self._refresh_catalog()
                self._sync_generation = generation
            if primary_id and primary_id != self.primary_id:
                changed = self.primary_id is not None
                self.primary_id = primary_id
                self.store.set_meta(TAIL_PRIMARY_KEY, primary_id)
                if changed:
                    # Serials of a different primary mean nothing to us.
                    log.warning("primary identity changed to %s: replaying its changelog",
                                primary_id[:8])
                    self.serial = 0
                    self._save()
                    continue
            if current < self.serial:
                log.warning("primary serial %d is behind ours (%d): replaying its changelog",
                            current, self.serial)
                self.serial = 0
                self._save()
                continue
            for ev in events:
                await self.apply_event(ev)
                self.serial = ev.serial
            self.serial = max(self.serial, getattr(self.client, "scanned", self.serial))
            await asyncio.to_thread(self._save)
            if self.serial >= current:
                # A short batch is everything the primary had for us.
                self.caught_up_to = current

    async def apply_event(self, ev: ChangeEvent) -> None:
        """Idempotently apply one changelog event."""
        from .resolver import ResolveError

        if ev.kind == "blob":
            if not self.store.has_blob(ev.digest):
                try:
                    await self.fetch_blob(ev.digest)
                except ResolveError as e:
                    if e.status != 404:
                        raise
                    # Garbage-collected on the primary since the event was
                    # written; nothing to replicate.
                    log.debug("skipping blob %s: gone from primary", ev.digest)
        elif ev.kind == "manifest":
            if not self.store.shareable_repo(ev.upstream, ev.repo):
                return  # local/private namespace or conflicting public catalog
            assert ev.bytes is not None
            # Replace on digest change so moved tags propagate. Blobs are NOT
            # pre-fetched here: blob events warm them, and anything still
            # missing is fetched lazily when a client asks for it.
            if self.store.manifest_digest(ev.upstream, ev.repo, ev.reference) != ev.digest:
                await asyncio.to_thread(
                    self.store.put_manifest,
                    ev.upstream, ev.repo, ev.reference, ev.digest,
                    ev.media_type or "application/octet-stream", ev.bytes,
                )
                log.info("replicated manifest %s/%s:%s", ev.upstream, ev.repo, ev.reference)
        elif ev.kind == SOURCE_KIND:
            assert ev.bytes is not None and ev.reference is not None
            await asyncio.to_thread(self.store.apply_source_event, ev.reference, ev.bytes)
        else:
            raise ValueError("nonpublic event rejected by public-v2")
