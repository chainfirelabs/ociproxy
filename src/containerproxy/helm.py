"""Classic HTTP Helm sources with public sharing and node-local private fetching.

Indexes are rewritten using the safe YAML node graph; archive digests are checked
when provided. Index/chart pointers reference content-addressed blobs. Private
namespaces are node-local, while shared public pointers and verified bytes use
the public-v2 replication feed. Sources/credentials never move with private data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
import yaml

from .config import Config, HelmRepoConfig
from .metrics import Metrics
from .resolver import ResolveError, Resolver, _as_resolve_error
from .store import HELM_NS, ManifestRecord, Store
from .upstream import UpstreamError

log = logging.getLogger("containerproxy.helm")

HELM_INDEX_TYPE = "application/vnd.containerproxy.helm.index.v1+json"
HELM_CHART_TYPE = "application/vnd.containerproxy.helm.chart.v1+json"
INDEX_LAYER_TYPE = "application/x-yaml"
CHART_LAYER_TYPE = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"

# Pointer key of a repo's index. No chart can be named "index.yaml": the
# chart route rejects it.
INDEX_REPO = "index.yaml"
INDEX_REF = "index"

MAX_INDEX_BYTES = 512 << 20
READ_CHUNK = 1 << 20

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_Dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)
_STR_TAG = "tag:yaml.org,2002:str"


def helm_ns(name: str) -> str:
    return HELM_NS + name


def chart_url(chart: str, version: str) -> str:
    """Relative URL of a chart archive inside a proxied repo."""
    c, v = quote(chart, safe=""), quote(version, safe="+")
    return f"charts/{c}/{v}/{c}-{v}.tgz"


def pointer_bytes(media_type: str, layer_type: str, digest: str, size: int,
                  annotations: dict[str, str] | None = None) -> bytes:
    """Deterministic pointer document (same content -> same digest on every node)."""
    doc: dict = {
        "schemaVersion": 2,
        "mediaType": media_type,
        "layers": [{"mediaType": layer_type, "digest": digest, "size": size}],
    }
    if annotations:
        doc["annotations"] = annotations
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def pointer_blob(rec: ManifestRecord) -> str:
    return json.loads(rec.bytes)["layers"][0]["digest"]


def pointer_annotations(rec: ManifestRecord) -> dict[str, str]:
    return json.loads(rec.bytes).get("annotations") or {}


def _normalize_digest(value: str | None) -> str | None:
    """The index's ``digest`` is bare hex (sometimes 'sha256:'-prefixed)."""
    if not value:
        return None
    hexpart = value.removeprefix("sha256:").strip().lower()
    if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
        return None
    return "sha256:" + hexpart


def _valid_segment(value: str) -> bool:
    return bool(value) and "/" not in value and value not in (".", "..")


def rewrite_index(
    body: bytes, base_url: str
) -> tuple[bytes, list[tuple[str, str, str, str | None]]]:
    """Point every chart URL in an index.yaml back at the proxy.

    Returns (rewritten index, [(chart, version, original url, digest)]).
    Entries without a usable name/version/url are left untouched.
    """
    root = yaml.compose(body, Loader=_Loader)
    if not isinstance(root, yaml.MappingNode):
        raise ResolveError(502, "helm index is not a YAML mapping")
    entries = next((v for k, v in root.value
                    if isinstance(k, yaml.ScalarNode) and k.value == "entries"), None)
    if not isinstance(entries, yaml.MappingNode):
        raise ResolveError(502, "helm index has no entries mapping")
    repo_base = base_url.rstrip("/") + "/"
    sources: list[tuple[str, str, str, str | None]] = []
    for key, versions in entries.value:
        chart = getattr(key, "value", None)
        if not isinstance(chart, str) or not _valid_segment(chart) or chart == INDEX_REPO:
            continue
        if not isinstance(versions, yaml.SequenceNode):
            continue
        for item in versions.value:
            if not isinstance(item, yaml.MappingNode):
                continue
            fields = {k.value: v for k, v in item.value if isinstance(k, yaml.ScalarNode)}
            version, urls = fields.get("version"), fields.get("urls")
            if not isinstance(version, yaml.ScalarNode) or not _valid_segment(version.value):
                continue
            if not isinstance(urls, yaml.SequenceNode) or not urls.value:
                continue
            first = urls.value[0]
            if not isinstance(first, yaml.ScalarNode) or not first.value:
                continue
            digest = fields.get("digest")
            sources.append((
                chart, version.value, urljoin(repo_base, first.value),
                _normalize_digest(digest.value if isinstance(digest, yaml.ScalarNode) else None),
            ))
            urls.value = [yaml.ScalarNode(_STR_TAG, chart_url(chart, version.value))]
    out = yaml.serialize(root, Dumper=_Dumper, encoding="utf-8",
                         allow_unicode=True, width=1 << 30)
    return out, sources


class HelmRepoClient:
    """Fetches a classic repo's index and chart archives.

    Credentials are only ever sent to the repo's own origin: index entries
    may point anywhere (GitHub releases, CDNs), and Helm itself does the
    same unless --pass-credentials is given. httpx strips Authorization on
    cross-origin redirects.
    """

    def __init__(self, repo: HelmRepoConfig, http: httpx.AsyncClient):
        self.repo = repo
        self.http = http

    def _auth(self, url: str):
        if not self.repo.username:
            return None
        a, b = httpx.URL(url), httpx.URL(self.repo.base_url)
        if (a.scheme, a.host, a.port) != (b.scheme, b.host, b.port):
            return None
        return (self.repo.username, self.repo.password or "")

    async def fetch_index(
        self, etag: str | None, last_modified: str | None
    ) -> tuple[bytes | None, str | None, str | None]:
        """(body | None if unchanged, etag, last_modified)."""
        url = f"{self.repo.base_url}/index.yaml"
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        req = self.http.build_request("GET", url, headers=headers)
        resp = await self.http.send(req, stream=True, auth=self._auth(url))
        try:
            if resp.status_code == 304:
                return None, etag, last_modified
            if resp.status_code != 200:
                body = await resp.aread()
                raise UpstreamError(resp.status_code, f"helm index {url}: {body[:200]!r}")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_INDEX_BYTES:
                    raise UpstreamError(502, f"helm index {url} exceeds {MAX_INDEX_BYTES} bytes")
                chunks.append(chunk)
            return b"".join(chunks), resp.headers.get("etag"), resp.headers.get("last-modified")
        finally:
            await resp.aclose()

    async def open_chart(self, url: str) -> httpx.Response:
        """Start a streaming chart download. Caller must close the response."""
        req = self.http.build_request("GET", url)
        resp = await self.http.send(req, stream=True, auth=self._auth(url))
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            raise UpstreamError(resp.status_code, f"helm chart {url}: {body[:200]!r}")
        return resp


class HelmResolver:
    """Resolves Helm index/chart pointers: local store -> primary -> source repo."""

    def __init__(
        self,
        store: Store,
        config: Config,
        http: httpx.AsyncClient,
        resolver: Resolver,
        primary=None,  # replication.PrimaryClient | None
        metrics: Metrics | None = None,
    ):
        self.store = store
        self.config = config
        self.resolver = resolver
        self.primary = primary
        self.metrics = metrics or Metrics()
        self.clients = {r.name: HelmRepoClient(r, http) for r in config.helm_repos}
        self._inflight: dict[tuple, asyncio.Task] = {}

    async def aclose(self) -> None:
        tasks = list(self._inflight.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def namespace(self, name: str) -> str:
        source = self.config.helm_repo(name)
        if source is None:
            raise ResolveError(404, "unknown Helm repository")
        return HELM_NS + source.storage_name

    def direct(self, name: str) -> bool:
        source = self.config.helm_repo(name)
        return self.config.is_primary or bool(source and source.sharing == "local")

    def knows(self, name: str) -> bool:
        return self.config.helm_repo(name) is not None

    # -- public --------------------------------------------------------------

    async def index_path(self, name: str) -> Path:
        rec = await self.index_record(name)
        return await self.resolver.resolve_blob(rec.upstream, rec.repo, pointer_blob(rec))

    async def chart_path(self, name: str, chart: str, version: str) -> Path:
        rec = await self.chart_record(name, chart, version)
        return await self.resolver.resolve_blob(rec.upstream, rec.repo, pointer_blob(rec))

    async def record(self, namespace: str, repo: str, reference: str) -> ManifestRecord:
        """Pointer lookup for /internal/pull/manifest (primary side)."""
        name = namespace[len(HELM_NS):]
        if not self.knows(name):
            raise ResolveError(404, f"unknown helm repo {name!r}")
        if repo == INDEX_REPO and reference == INDEX_REF:
            return await self.index_record(name)
        return await self.chart_record(name, repo, reference)

    # -- index -----------------------------------------------------------------

    async def index_record(self, name: str) -> ManifestRecord:
        rec = self.store.get_manifest(self.namespace(name), INDEX_REPO, INDEX_REF)
        if rec is not None and not self.store.has_blob(pointer_blob(rec)) and self.direct(name):
            rec = None
        if rec is not None and time.time() - rec.checked_at < self.config.tag_ttl:
            self._count("index", "hit")
        else:
            rec = await self._dedup(("index", name), lambda: self._refresh_index(name, rec))
        self.store.mark_pulled(rec)
        return rec

    async def _refresh_index(self, name: str, rec: ManifestRecord | None) -> ManifestRecord:
        self.metrics.inc("containerproxy_source_fetches_total", kind="helm-index")
        try:
            if self.direct(name):
                fresh = await self._index_from_source(name, rec)
            else:
                assert self.primary is not None
                fresh = await self.primary.pull_manifest(self.namespace(name), INDEX_REPO, INDEX_REF)
                if fresh is None:
                    raise ResolveError(404, f"primary does not have helm repo {name!r}")
        except Exception as e:  # noqa: BLE001 - with a cached copy: serve it
            err = _as_resolve_error(e)
            if err.status != 404 or rec is not None:
                self.metrics.inc("containerproxy_source_errors_total")
            if rec is None:
                raise err
            log.warning("refreshing helm index %s failed, serving cached: %s", name, err.detail)
            self._count("index", "stale")
            # Back off for one TTL instead of re-trying (and stalling) every request.
            await asyncio.to_thread(self.store.touch_manifest, rec.upstream, rec.repo, rec.reference)
            return rec
        if fresh is None or (rec is not None and fresh.digest == rec.digest):
            assert rec is not None
            await asyncio.to_thread(self.store.touch_manifest, rec.upstream, rec.repo, rec.reference)
            self._count("index", "revalidated")
            return rec
        await asyncio.to_thread(
            self.store.put_manifest, fresh.upstream, fresh.repo, fresh.reference,
            fresh.digest, fresh.media_type, fresh.bytes,
        )
        self._count("index", "miss" if rec is None else "refreshed")
        log.info("stored helm index %s (%s)", name, fresh.digest)
        return fresh

    async def _index_from_source(
        self, name: str, rec: ManifestRecord | None
    ) -> ManifestRecord | None:
        """Fetch + rewrite the upstream index; None if it is unchanged."""
        client = self.clients[name]
        ann = pointer_annotations(rec) if rec is not None else {}
        body, etag, last_modified = await client.fetch_index(
            ann.get("etag"), ann.get("last_modified")
        )
        if body is None:
            return None
        upstream_digest = _sha256(body)
        if rec is not None and ann.get("upstream_digest") == upstream_digest:
            return None
        try:
            rewritten, sources = await asyncio.to_thread(rewrite_index, body, client.repo.base_url)
        except yaml.YAMLError as e:
            raise ResolveError(502, f"helm index {name} is not valid YAML: {e}")
        digest = await self._store_bytes(rewritten, origin=(self.namespace(name), INDEX_REPO))
        # Sources before the pointer: once the new index is served, every
        # chart it lists must be resolvable.
        await asyncio.to_thread(self.store.replace_helm_sources, name, sources)
        annotations = {"upstream_digest": upstream_digest}
        if etag:
            annotations["etag"] = etag
        if last_modified:
            annotations["last_modified"] = last_modified
        body = pointer_bytes(HELM_INDEX_TYPE, INDEX_LAYER_TYPE, digest, len(rewritten), annotations)
        return ManifestRecord(self.namespace(name), INDEX_REPO, INDEX_REF, _sha256(body),
                              HELM_INDEX_TYPE, body)

    async def _store_bytes(self, data: bytes, *, origin=None) -> str:
        tmp_path, writer = self.store.new_blob_tmp()
        try:
            await writer.awrite(data)
            writer.close()
        except BaseException:
            writer.abort()
            raise
        await asyncio.to_thread(self.store.commit_blob, tmp_path, writer.digest, writer.size, origin=origin)
        return writer.digest

    # -- charts ----------------------------------------------------------------

    async def chart_record(self, name: str, chart: str, version: str) -> ManifestRecord:
        ns = self.namespace(name)
        rec = self.store.get_manifest(ns, chart, version)
        key = ("chart", name, chart, version)
        if not self.direct(name):
            if rec is None:
                self._count("chart", "miss")
                rec = await self._dedup(key, lambda: self._chart_from_primary(name, chart, version))
            else:
                self._count("chart", "hit")
            self.store.mark_pulled(rec)
            return rec

        src = self.store.helm_source(name, chart, version)
        if rec is not None and self.store.has_blob(pointer_blob(rec)) and (src is None or src[1] is None or src[1] == pointer_blob(rec)):
            self._count("chart", "hit")
            self.store.mark_pulled(rec)
            return rec
        if src is None:
            # Not listed by the index we have: it may be newer than it.
            await self.index_record(name)
            src = self.store.helm_source(name, chart, version)
            if src is None:
                raise ResolveError(404, f"chart {chart} {version} not found in helm repo {name!r}")
        self._count("chart", "miss" if rec is None else "refreshed")
        url, digest = src
        rec = await self._dedup(key, lambda: self._chart_from_source(name, chart, version, url, digest))
        self.store.mark_pulled(rec)
        return rec

    async def _chart_from_primary(self, name: str, chart: str, version: str) -> ManifestRecord:
        assert self.primary is not None
        self.metrics.inc("containerproxy_source_fetches_total", kind="helm-chart")
        try:
            rec = await self.primary.pull_manifest(self.namespace(name), chart, version)
        except httpx.HTTPError as e:
            self.metrics.inc("containerproxy_source_errors_total")
            raise _as_resolve_error(e)
        if rec is None:
            raise ResolveError(404, f"chart {chart} {version} not found in helm repo {name!r}")
        if self.store.manifest_digest(rec.upstream, rec.repo, rec.reference) != rec.digest:
            await asyncio.to_thread(
                self.store.put_manifest, rec.upstream, rec.repo, rec.reference,
                rec.digest, rec.media_type, rec.bytes,
            )
        return rec

    async def _chart_from_source(
        self, name: str, chart: str, version: str, url: str, digest: str | None
    ) -> ManifestRecord:
        self.metrics.inc("containerproxy_source_fetches_total", kind="helm-chart")
        if digest is not None and self.store.has_blob(digest) and self.resolver.cached_blob_allowed(self.namespace(name), chart, digest):
            await asyncio.to_thread(self.store.touch_blob, digest)
            size = self.store.blob_path(digest).stat().st_size
        else:
            digest, size = await self._download_chart(name, url, digest, chart)
        body = pointer_bytes(HELM_CHART_TYPE, CHART_LAYER_TYPE, digest, size)
        rec = ManifestRecord(self.namespace(name), chart, version, _sha256(body), HELM_CHART_TYPE, body)
        if self.store.manifest_digest(rec.upstream, chart, version) != rec.digest:
            await asyncio.to_thread(
                self.store.put_manifest, rec.upstream, chart, version,
                rec.digest, rec.media_type, rec.bytes,
            )
            log.info("stored helm chart %s/%s %s (%s)", name, chart, version, digest)
        return rec

    async def _download_chart(
        self, name: str, url: str, expected: str | None, chart: str
    ) -> tuple[str, int]:
        """Download an archive into the store. Charts are small, so it is
        stored whole before it is served; the digest is verified when the
        index lists one."""
        try:
            resp = await self.clients[name].open_chart(url)
        except UpstreamError as e:
            self.metrics.inc("containerproxy_source_errors_total")
            raise ResolveError(e.status, e.detail)
        except httpx.HTTPError as e:
            self.metrics.inc("containerproxy_source_errors_total")
            raise _as_resolve_error(e)
        tmp_path, writer = self.store.new_blob_tmp()
        try:
            async for chunk in resp.aiter_bytes(READ_CHUNK):
                await writer.awrite(chunk)
            writer.close()
            if expected is not None and writer.digest != expected:
                raise ResolveError(502, f"digest mismatch fetching {url}: index says {expected}, "
                                        f"got {writer.digest}")
        except BaseException as e:
            writer.abort()
            if not isinstance(e, asyncio.CancelledError):
                self.metrics.inc("containerproxy_source_errors_total")
            if isinstance(e, httpx.HTTPError):
                raise _as_resolve_error(e)
            raise
        finally:
            await resp.aclose()
        await asyncio.to_thread(self.store.commit_blob, tmp_path, writer.digest, writer.size, origin=(self.namespace(name), chart))
        self.metrics.inc("containerproxy_source_bytes_total", writer.size)
        return writer.digest, writer.size

    # -- helpers ---------------------------------------------------------------

    def _count(self, kind: str, result: str) -> None:
        self.metrics.inc("containerproxy_helm_requests_total", kind=kind, result=result)

    async def _dedup(self, key: tuple, coro_fn):
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(coro_fn())
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        return await asyncio.shield(task)
