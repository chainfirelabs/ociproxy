"""Resolve node-local private sources directly and shared public misses via primary.

Concurrent downloads are coalesced within a source/repository scope. Clients can
stream from the fetch as it proceeds; SHA-256 is checked before committing data
or granting byte provenance. A public digest assertion alone never authorizes
reuse of privately cached bytes. Background fetches survive client disconnects.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx

from .isolation import direct_source
from .config import Config
from .metrics import Metrics
from .notify import ChangeSignal
from .store import LOCAL, ManifestRecord, Store
from .upstream import UpstreamClient, UpstreamError

log = logging.getLogger("containerproxy.resolver")

READ_CHUNK = 1 << 20

# Bound on remembered source tag lists ((upstream, repo) entries).
TAG_LIST_CACHE_SIZE = 4096

# OCI referrers tag schema fallback ("sha256-<hex>", plus cosign-style
# ".sig"/".att"/".sbom" suffixes). Clients probe these on every pull after the
# referrers API 404s, and treat anything but 404 as fatal.
_REFERRERS_TAG_RE = re.compile(r"[a-z0-9]+-[a-f0-9]{32,}(\.[a-z]+)?")


class ResolveError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"resolve failed ({status}): {detail}")


def _as_resolve_error(e: BaseException) -> ResolveError:
    if isinstance(e, ResolveError):
        return e
    if isinstance(e, UpstreamError):
        return ResolveError(e.status, e.detail)
    return ResolveError(502, f"{e.__class__.__name__}: {e}")


class BlobFetch:
    """One in-progress download of a blob into the store.

    The download runs as its own task; any number of readers stream the
    bytes from the temp file as they land. Readers never cancel it.
    """

    def __init__(
        self,
        store: Store,
        digest: str,
        open_source: Callable[[], Awaitable[httpx.Response]],
        metrics: Metrics,
        origin=None,
    ):
        self.store = store
        self.digest = digest
        self.metrics = metrics
        self.origin = origin
        self.tmp_path, self.writer = store.new_blob_tmp()
        self.size = 0  # bytes written (and flushed) to tmp_path
        self.content_length: str | None = None
        self.committed = False
        self.error: ResolveError | None = None
        self._opened = asyncio.Event()
        self._progress = ChangeSignal()
        self.task = asyncio.create_task(self._run(open_source))

    @property
    def finished(self) -> bool:
        return self.committed or self.error is not None

    async def _run(self, open_source) -> None:
        try:
            resp = await open_source()
        except BaseException as e:
            self.writer.abort()
            self._fail(e)
            if isinstance(e, asyncio.CancelledError):
                raise
            return
        # With Content-Encoding the header counts encoded bytes, not the
        # blob's; don't promise a length we won't deliver.
        if not resp.headers.get("content-encoding"):
            self.content_length = resp.headers.get("content-length")
        self._opened.set()
        try:
            async for chunk in resp.aiter_bytes():
                await self.writer.awrite(chunk)
                self.size = self.writer.size
                self._progress.fire()
            self.writer.close()
            if self.writer.digest != self.digest:
                raise ResolveError(
                    502, f"digest mismatch fetching blob {self.digest}: got {self.writer.digest}"
                )
            # A reader racing this rename either already holds the inode
            # open or gets FileNotFoundError and reopens the final path.
            await asyncio.to_thread(
                self.store.commit_blob, self.tmp_path, self.digest, self.writer.size,
                origin=self.origin() if self.origin else None,
            )
            self.committed = True
            self.metrics.inc("containerproxy_source_bytes_total", self.writer.size)
            log.debug("stored blob %s (%d bytes)", self.digest, self.writer.size)
        except BaseException as e:
            self.writer.abort()
            self._fail(e)
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            await resp.aclose()
            self._progress.fire()

    def _fail(self, e: BaseException) -> None:
        self.error = _as_resolve_error(e)
        self.metrics.inc("containerproxy_source_errors_total")
        self._opened.set()
        self._progress.fire()

    async def wait_open(self) -> str | None:
        """Wait until the source answered; returns its content length."""
        await self._opened.wait()
        if self.error is not None and not self.size:
            raise self.error
        return self.content_length

    async def wait_done(self) -> Path:
        while not self.finished:
            await self._progress.token().wait()
        if self.error is not None:
            raise self.error
        return self.store.blob_path(self.digest)

    async def stream(self) -> AsyncIterator[bytes]:
        try:
            f = open(self.tmp_path, "rb")  # noqa: SIM115
        except FileNotFoundError:
            # Already committed (renamed) or failed (unlinked).
            f = open(await self.wait_done(), "rb")  # noqa: SIM115
        try:
            pos = 0
            while True:
                token = self._progress.token()
                if pos < self.size:
                    chunk = await asyncio.to_thread(f.read, min(self.size - pos, READ_CHUNK))
                    if not chunk:
                        raise ResolveError(502, f"blob {self.digest} temp file truncated")
                    pos += len(chunk)
                    yield chunk
                    continue
                if self.error is not None:
                    raise self.error
                if self.committed:
                    return
                await token.wait()
        finally:
            f.close()


class Resolver:
    def __init__(
        self,
        store: Store,
        config: Config,
        clients: dict[str, UpstreamClient],
        primary=None,  # replication.PrimaryClient | None
        metrics: Metrics | None = None,
    ):
        self.store = store
        self.config = config
        self.clients = clients
        self.primary = primary
        self.metrics = metrics or Metrics()
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._blob_fetches: dict[tuple, BlobFetch] = {}
        # (upstream, repo) -> (fetched_at, tags from the source)
        self._tag_lists: dict[tuple[str, str], tuple[float, list[str]]] = {}

    async def aclose(self) -> None:
        tasks = [f.task for f in self._blob_fetches.values()] + list(self._inflight.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- public --------------------------------------------------------------

    async def resolve_manifest(self, upstream: str, repo: str, reference: str) -> ManifestRecord:
        rec = self.store.get_manifest(upstream, repo, reference)
        if rec is None and ":" in reference:
            rec = self.store.get_manifest_by_digest(upstream, repo, reference)
        if rec is not None:
            if self._is_stale(rec):
                rec = await self._dedup(
                    ("revalidate", upstream, repo, reference),
                    lambda: self._revalidate(rec),
                )
            else:
                self.metrics.inc("containerproxy_manifest_requests_total", result="hit")
            self.store.mark_pulled(rec)
            return rec
        self.metrics.inc("containerproxy_manifest_requests_total", result="miss")
        try:
            return await self._dedup(
                ("manifest", upstream, repo, reference),
                lambda: self._fetch_manifest(upstream, repo, reference),
            )
        except ResolveError as e:
            # A referrers probe must not fail a pull while the source is
            # unreachable (a replica whose primary is down, a primary whose
            # upstream is): with nothing cached, "no referrers" is the answer.
            if e.status >= 500 and _REFERRERS_TAG_RE.fullmatch(reference):
                log.debug("referrers tag %s/%s:%s: source failed (%s), answering 404",
                          upstream, repo, reference, e.detail)
                raise ResolveError(404, f"manifest unknown: {upstream}/{repo}:{reference}")
            raise

    async def list_tags(self, upstream: str, repo: str) -> list[str] | None:
        """Sorted tags: cached here, plus the source's list for mirrors.

        None if neither this node nor the source knows the repo. If the
        source fails but tags are cached, the cached tags are returned.
        """
        cached = await asyncio.to_thread(self.store.list_tags, upstream, repo, None, None)
        if upstream == LOCAL:
            return cached  # pushes: this node's copy is complete
        try:
            remote = await self._source_tags(upstream, repo)
        except ResolveError as e:
            if cached is None:
                raise
            log.warning("listing tags of %s/%s failed, serving cached: %s", upstream, repo, e.detail)
            # Back off for one TTL instead of re-trying on every listing.
            self._remember_tags(upstream, repo, [])
            return cached
        if remote is None:
            return cached
        return sorted(set(cached or ()) | set(remote))

    async def _source_tags(self, upstream: str, repo: str) -> list[str] | None:
        key = (upstream, repo)
        hit = self._tag_lists.get(key)
        if hit is not None and time.time() - hit[0] < self.config.tag_ttl:
            return hit[1]
        return await self._dedup(("tags", upstream, repo), lambda: self._fetch_tags(upstream, repo))

    async def _fetch_tags(self, upstream: str, repo: str) -> list[str] | None:
        self.metrics.inc("containerproxy_source_fetches_total", kind="tags")
        try:
            if direct_source(self.config, upstream):
                client = self.clients.get(upstream)
                if client is None:
                    raise ResolveError(404, f"unknown upstream {upstream!r}")
                try:
                    tags = await client.list_tags(repo)
                except UpstreamError as e:
                    raise ResolveError(e.status, e.detail)
            else:
                assert self.primary is not None
                tags = await self.primary.pull_tags(upstream, repo)
                if tags is None:
                    raise ResolveError(404, f"primary does not know {upstream}/{repo}")
        except ResolveError as e:
            if e.status != 404:
                self.metrics.inc("containerproxy_source_errors_total")
            raise
        except httpx.HTTPError as e:
            self.metrics.inc("containerproxy_source_errors_total")
            raise _as_resolve_error(e)
        self._remember_tags(upstream, repo, tags)
        return tags

    def _remember_tags(self, upstream: str, repo: str, tags: list[str]) -> None:
        key = (upstream, repo)
        self._tag_lists.pop(key, None)
        if len(self._tag_lists) >= TAG_LIST_CACHE_SIZE:
            self._tag_lists.pop(next(iter(self._tag_lists)))  # oldest insert
        self._tag_lists[key] = (time.time(), tags)

    def blob_in_flight(self, digest: str) -> bool:
        return any(key[2] == digest for key in self._blob_fetches)

    def cached_blob_allowed(self, upstream: str, repo: str, digest: str) -> bool:
        if upstream == LOCAL:
            return True  # local pushes were explicitly published by a local pusher
        if self.store.has_blob_grant(upstream, repo, digest):
            return True
        return self.store.public_blob(digest) and self.store.repo_references_blob(upstream, repo, digest)

    async def authorize_blob(self, upstream: str, repo: str, digest: str):
        if self.cached_blob_allowed(upstream, repo, digest):
            return None
        # A manifest/digest assertion is not evidence that a public source owns
        # privately cached bytes. Stream a new source download and grant ownership
        # ONLY when its actual bytes pass SHA-256 verification at commit.
        fetch = self._blob_fetch(upstream, repo, digest)
        await fetch.wait_open()
        return fetch

    async def resolve_blob(self, upstream: str, repo: str, digest: str) -> Path:
        """Ensure a blob is in the store (joining any in-flight download) and
        return its path. Used by HEAD requests and the replica tail; GET
        requests should use open_blob_stream() to stream while fetching.
        """
        if self.store.has_blob(digest) and self.cached_blob_allowed(upstream, repo, digest):
            return self.store.blob_path(digest)
        return await self._blob_fetch(upstream, repo, digest).wait_done()

    async def open_blob_stream(self, upstream: str, repo: str, digest: str):
        """Stream a blob from its source while it is fetched into the store.

        The source is opened EAGERLY so upstream errors (404, 502, ...)
        surface before the caller sends response headers. Returns
        ``(content_length | None, chunk_iterator)``.
        """
        fetch = self._blob_fetch(upstream, repo, digest)
        clen = await fetch.wait_open()
        return clen, fetch.stream()

    # -- blobs -------------------------------------------------------------------

    def _blob_fetch(self, upstream: str, repo: str, digest: str) -> BlobFetch:
        key = (upstream, repo, digest)
        fetch = self._blob_fetches.get(key)
        if fetch is not None and fetch.error is None:
            return fetch
        self.metrics.inc("containerproxy_blob_requests_total", result="miss")
        self.metrics.inc("containerproxy_source_fetches_total", kind="blob")
        fetch = BlobFetch(
            self.store, digest, lambda: self._open_source(upstream, repo, digest), self.metrics,
            origin=self._origin_check(upstream, repo),
        )
        self._blob_fetches[key] = fetch

        def forget(_t, fetch=fetch):
            if self._blob_fetches.get(key) is fetch:
                del self._blob_fetches[key]

        fetch.task.add_done_callback(forget)
        return fetch

    def _origin_check(self, upstream, repo):
        if upstream == "@public-feed" and not self.config.is_primary:
            return lambda: (upstream, repo)
        def source():
            if upstream.startswith("helm/"):
                return next((r for r in self.config.helm_repos if "helm/"+r.storage_name == upstream), None)
            return next((u for u in self.config.upstreams if u.storage_name == upstream), None)
        initial = source()
        fingerprint = (initial.base_url, initial.sharing) if initial else None
        def origin():
            current = source()
            if current and (current.base_url, current.sharing) == fingerprint:
                return upstream, repo
            return None  # source removed/repointed while its download was in flight
        return origin

    async def _open_source(self, upstream: str, repo: str, digest: str) -> httpx.Response:
        """Open a streaming GET for the blob from upstream or the primary."""
        if direct_source(self.config, upstream):
            client = self.clients.get(upstream)
            if client is None:
                raise ResolveError(
                    404, f"blob {digest} not found and no upstream '{upstream}' to fetch from"
                )
            try:
                return await client.open_blob(repo, digest)
            except UpstreamError as e:
                raise ResolveError(e.status, e.detail)
        assert self.primary is not None
        return await self.primary.open_blob(digest, upstream=upstream, repo=repo)

    # -- manifests ---------------------------------------------------------------

    def _is_stale(self, rec: ManifestRecord) -> bool:
        if ":" in rec.reference:
            return False  # digest reference: immutable
        if (self.config.is_primary or rec.upstream == LOCAL) and rec.upstream not in self.clients:
            return False  # primary's own pushes are authoritative
        return time.time() - rec.checked_at >= self.config.tag_ttl

    async def _revalidate(self, rec: ManifestRecord) -> ManifestRecord:
        """Re-check a cached tag against its source; serve the cache on failure."""
        upstream, repo, reference = rec.upstream, rec.repo, rec.reference
        self.metrics.inc("containerproxy_source_fetches_total", kind="revalidate")
        try:
            if direct_source(self.config, upstream):
                client = self.clients[upstream]
                digest = await client.head_manifest(repo, reference)
                if digest is not None and digest == rec.digest:
                    await asyncio.to_thread(self.store.touch_manifest, upstream, repo, reference)
                    self.metrics.inc("containerproxy_manifest_requests_total", result="revalidated")
                    return rec
                fm = await client.fetch_manifest(repo, reference)
                fresh = ManifestRecord(upstream, repo, reference, fm.digest, fm.media_type, fm.bytes)
            else:
                assert self.primary is not None
                fresh = await self.primary.pull_manifest(upstream, repo, reference)
                if fresh is None:
                    raise ResolveError(404, "primary no longer has this tag")
        except Exception as e:  # noqa: BLE001 - any failure means "serve cached"
            log.warning(
                "revalidating %s/%s:%s failed, serving cached %s: %s",
                upstream, repo, reference, rec.digest, e,
            )
            self.metrics.inc("containerproxy_source_errors_total")
            self.metrics.inc("containerproxy_manifest_requests_total", result="stale")
            # Back off for one TTL instead of re-trying (and stalling) every pull.
            await asyncio.to_thread(self.store.touch_manifest, upstream, repo, reference)
            return rec
        if fresh.digest == rec.digest:
            await asyncio.to_thread(self.store.touch_manifest, upstream, repo, reference)
            self.metrics.inc("containerproxy_manifest_requests_total", result="revalidated")
            return rec
        await asyncio.to_thread(
            self.store.put_manifest,
            upstream, repo, reference, fresh.digest, fresh.media_type, fresh.bytes,
        )
        self.metrics.inc("containerproxy_manifest_requests_total", result="refreshed")
        log.info("tag %s/%s:%s moved %s -> %s", upstream, repo, reference, rec.digest, fresh.digest)
        return fresh

    async def _fetch_manifest(self, upstream: str, repo: str, reference: str) -> ManifestRecord:
        self.metrics.inc("containerproxy_source_fetches_total", kind="manifest")
        try:
            if direct_source(self.config, upstream):
                client = self.clients.get(upstream)
                if client is None:
                    raise ResolveError(404, f"unknown upstream {upstream!r}")
                try:
                    fm = await client.fetch_manifest(repo, reference)
                except UpstreamError as e:
                    raise ResolveError(e.status, e.detail)
                rec = ManifestRecord(upstream, repo, reference, fm.digest, fm.media_type, fm.bytes)
            else:
                assert self.primary is not None
                rec = await self.primary.pull_manifest(upstream, repo, reference)
                if rec is None:
                    raise ResolveError(404, f"primary does not have {upstream}/{repo}:{reference}")
        except ResolveError as e:
            if e.status != 404:
                self.metrics.inc("containerproxy_source_errors_total")
            raise
        except httpx.HTTPError as e:
            self.metrics.inc("containerproxy_source_errors_total")
            raise _as_resolve_error(e)
        if self.store.manifest_digest(upstream, repo, reference) != rec.digest:
            await asyncio.to_thread(
                self.store.put_manifest,
                upstream, repo, reference, rec.digest, rec.media_type, rec.bytes,
            )
            log.info("stored manifest %s/%s:%s (%s)", upstream, repo, reference, rec.digest)
        return rec

    # -- in-flight dedup -------------------------------------------------------

    async def _dedup(self, key: tuple, coro_fn):
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(coro_fn())
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        return await asyncio.shield(task)
