"""Wire one independent registry node, local administration and public-v2 replication.

/v2 and /helm enforce local accounts and visibility. /internal requires only
this node's administration secret and acting-user roles. /replication/v2 accepts
separate, per-peer credentials on a primary and exports only proven public data.
Legacy account replication and incoming fan-out/ingest routes are removed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse,
)
from starlette.routing import Route

from .isolation import PROTOCOL, shared_repo
from .accounts_api import AccountsAPI
from .auth import AccessPolicy, Authenticator, Principal, role_at_least
from .config import Config, validate_source_scope
from .helm import INDEX_REPO, HelmResolver
from .metrics import Metrics
from .notify import ChangeSignal
from .replication import TAIL_SERIAL_KEY, PrimaryClient, ReplicaTail, event_to_json
from .resolver import ResolveError, Resolver
from .sources import SourceRegistry
from .sources_api import SourcesAPI
from .store import HELM_NS, LOCAL, Store, ensure_node_id, parse_digest
from .upstream import UpstreamClient

log = logging.getLogger("ociproxy.api")

REGISTRY_VERSION_HEADER = ("Docker-Distribution-Api-Version", "registry/2.0")


def registry_error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"errors": [{"code": code, "message": message, "detail": {}}]},
        status_code=status,
        headers=headers,
    )


def _secret_equal(given: str | None, expected: str | None) -> bool:
    """Constant-time comparison (no timing leak of how much matched)."""
    if given is None or expected is None:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


class BadRequest(Exception):
    """Invalid client input; rendered as a 400."""


def _int_param(request: Request, name: str, default: int | None, lo: int, hi: int | None = None) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise BadRequest(f"{name} must be an integer")
    if v < lo or (hi is not None and v > hi):
        raise BadRequest(f"{name} must be between {lo} and {hi}" if hi is not None else f"{name} must be >= {lo}")
    return v


def _float_param(request: Request, name: str, default: float, lo: float, hi: float) -> float:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        raise BadRequest(f"{name} must be a number")
    return min(max(v, lo), hi)


async def _json_object(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise BadRequest("body is not valid JSON")
    if not isinstance(data, dict):
        raise BadRequest("body must be a JSON object")
    return data


def _str_field(data: dict, key: str, *, required: bool = True) -> str | None:
    v = data.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or (required and not v):
        raise BadRequest(f"{key!r} must be a non-empty string")
    return v


async def _wait_disconnect(request: Request) -> None:
    """Return once the client disconnects (the request body is unused)."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


class UploadSession:
    __slots__ = ("id", "repo", "username", "tmp_path", "writer", "created", "last_active", "busy")

    def __init__(self, id: str, repo: str, username: str, tmp_path: Path, writer):
        self.id = id
        self.repo = repo
        self.username = username  # only its owner may continue it
        self.tmp_path = tmp_path
        self.writer = writer
        self.created = self.last_active = time.monotonic()
        self.busy = 0  # requests currently streaming into it (never expired)


class App:
    """Wires store, resolver, replication, and routes for one node."""

    def __init__(self, config: Config):
        if not config.secret:
            # An empty secret used to leave every /internal endpoint open:
            # anyone could register a "replica" (receiving all content), make
            # the primary POST to arbitrary URLs, or inject manifests into a
            # replica's cache.
            raise ValueError(
                "a node secret is required: set OCIPROXY_SECRET or node.secret "
                "(generate with: openssl rand -hex 32)"
            )
        for source in [*config.upstreams, *config.helm_repos]:
            validate_source_scope(source)
        if config.replication_token and config.replication_token == config.secret:
            raise ValueError("replication and local administration secrets must differ")
        tokens = [p["token"] for p in config.replication_peers.values()]
        if config.secret in tokens or len(tokens) != len(set(tokens)):
            raise ValueError("each replication peer needs a unique token, separate from the local secret")
        if not config.is_primary and not (config.replication_id and config.replication_token):
            raise ValueError("replicas require REPLICATION_ID and REPLICATION_TOKEN for public-v2")
        self.config = config
        self.store = Store(config.storage_path)
        try:
            self.store.initialize_isolation()
        except Exception:
            self.store.close()
            raise
        self.store.replication_enabled = config.is_primary
        self.store.shareable_repo = lambda upstream, repo: shared_repo(self.config, upstream, repo)
        self.node_id = ensure_node_id(config.storage_path)
        self.metrics = Metrics()
        # Wakes public changelog long-polls on transferable changes.
        self.changes = ChangeSignal()
        self.sync_generation = uuid.uuid4().hex
        self.store.add_listener(self.changes.notify)
        # Upstream traffic honors HTTP_PROXY/HTTPS_PROXY/NO_PROXY (trust_env)
        # or an explicit OCIPROXY_UPSTREAM_PROXY.
        self.upstream_http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=100),
            proxy=config.upstream_proxy,
            trust_env=True,
        )
        # Node-to-node replication traffic is NEVER proxied: a corporate proxy
        # cannot reach cluster-internal hostnames (primary/replica-1/...).
        self.internal_http = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=100),
            trust_env=False,
        )
        self.clients = {u.storage_name: UpstreamClient(u, self.upstream_http) for u in config.upstreams}
        self.primary_client: PrimaryClient | None = None
        if not config.is_primary:
            self.primary_client = PrimaryClient(
                config.primary_url, config.replication_token, self.internal_http, node_id=config.replication_id
            )
        self.resolver = Resolver(
            self.store, config, self.clients, self.primary_client, metrics=self.metrics
        )
        self.helm = HelmResolver(
            self.store, config, self.upstream_http, self.resolver, self.primary_client,
            metrics=self.metrics,
        )
        # Merges web-UI-managed upstreams / Helm repos into config.upstreams,
        # config.helm_repos and both client dicts, now and on every change.
        self.sources = SourceRegistry(
            self.store, config, self.upstream_http, self.clients, self.helm.clients
        )
        if config.is_primary and self.store.get_meta("public_log_rebuild"):
            self.store.rebuild_public_log()
        self.auth = Authenticator(self.store, config)
        self.policy = AccessPolicy(config)
        self.accounts = AccountsAPI(self)
        self.sources_api = SourcesAPI(self)
        self.uploads: dict[str, UploadSession] = {}
        self._upload_sweeper: asyncio.Task | None = None
        self.tail: ReplicaTail | None = None
        self._maintenance: asyncio.Task | None = None
        self.app = self._build_app()

    # -- lifecycle -----------------------------------------------------------

    @asynccontextmanager
    async def lifespan(self, app: Starlette):
        self.changes.bind(asyncio.get_running_loop())
        if not self.config.is_primary:
            advertised = self.config.advertised_url or f"http://{self.config.listen}:{self.config.port}"
            self.tail = ReplicaTail(
                self.store, self.primary_client, self.node_id, advertised.rstrip("/"),
                fetch_blob=lambda digest: self.resolver.resolve_blob("@public-feed", "", digest),
                local_sources=self.sources,
            )
            self.tail.start()
        if self.config.gc_interval > 0:
            self._maintenance = asyncio.create_task(self._maintenance_loop())
        self._upload_sweeper = asyncio.create_task(self._sweep_uploads_loop())
        if self.config.is_primary and self.policy.anything_private and not (
            self.auth.has_bootstrap or self.store.list_users()
        ):
            log.warning("some repos are private but no users exist: create one with "
                        "`ociproxy user add <name> --role admin`")
        log.info("ociproxy %s started (node %s)", self.config.role, self.node_id[:8])
        yield
        for task in (self._maintenance, self._upload_sweeper):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for closer in (self.tail and self.tail.stop,):
            if closer:
                await closer()
        await self.helm.aclose()
        await self.resolver.aclose()
        for session in list(self.uploads.values()):
            session.writer.abort()
        await self.upstream_http.aclose()
        await self.internal_http.aclose()
        self.store.close()

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.gc_interval)
            try:
                await self.run_maintenance()
            except Exception:  # noqa: BLE001
                log.exception("maintenance run failed")

    async def _sweep_uploads_loop(self) -> None:
        interval = min(60.0, self.config.upload_idle_timeout / 4)
        while True:
            await asyncio.sleep(interval)
            self.sweep_uploads()

    def sweep_uploads(self) -> int:
        """Drop upload sessions idle longer than upload_idle_timeout (a client
        that started a push and went away). Returns how many were dropped."""
        cutoff = time.monotonic() - self.config.upload_idle_timeout
        expired = [s for s in self.uploads.values() if not s.busy and s.last_active < cutoff]
        for session in expired:
            self.uploads.pop(session.id, None)
            session.writer.abort()
            log.info("dropped idle upload session %s (%s, %s bytes)",
                     session.id, session.repo, session.writer.size)
        if expired:
            self.metrics.inc("ociproxy_uploads_expired_total", len(expired))
        return len(expired)

    async def run_maintenance(self) -> None:
        """Garbage-collect, then compact the changelog."""
        result = await asyncio.to_thread(
            self.store.gc, self.config.gc_manifest_max_idle, self.config.gc_blob_grace
        )
        compacted = await asyncio.to_thread(self.store.compact_changelog)
        self.metrics.inc("ociproxy_gc_runs_total")
        self.metrics.inc("ociproxy_gc_deleted_manifests_total", result.manifests)
        self.metrics.inc("ociproxy_gc_deleted_blobs_total", result.blobs)
        self.metrics.inc("ociproxy_gc_freed_bytes_total", result.bytes)
        if result.manifests or result.blobs or compacted:
            log.info("maintenance: expired %d manifest(s), deleted %d blob(s) (%d bytes), "
                     "compacted %d changelog row(s)",
                     result.manifests, result.blobs, result.bytes, compacted)

    def _build_app(self) -> Starlette:
        routes = [
            Route("/", self.index),
            Route("/metrics", self.metrics_endpoint, methods=["GET"]),
            Route("/v2/", self.ping),
            Route("/v2/_catalog", self.catalog),
            Route("/v2/{rest:path}", self.v2_dispatch, methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"]),
            Route("/helm/{name}/index.yaml", self.helm_index, methods=["GET", "HEAD"]),
            Route("/helm/{name}/charts/{chart}/{version}/{file}", self.helm_chart, methods=["GET", "HEAD"]),
            Route("/replication/v2/register", self.internal_register, methods=["POST"]),
            Route("/replication/v2/changelog", self.internal_changelog, methods=["GET"]),
            Route("/replication/v2/pull/manifest", self.internal_pull_manifest, methods=["GET", "HEAD"]),
            Route("/replication/v2/pull/tags", self.internal_pull_tags, methods=["GET"]),
            Route("/replication/v2/pull/blob/{digest:path}", self.internal_pull_blob, methods=["GET", "HEAD"]),
            Route("/replication/v2/catalog", self.replication_catalog, methods=["GET"]),
            Route("/replication/v2/sources", self.replication_publication, methods=["POST"]),
            Route("/internal/overview", self.internal_overview, methods=["GET"]),
            Route("/internal/sync", self.accounts._guard(self.internal_sync), methods=["POST"]),
            Route("/internal/images", self.internal_images, methods=["GET"]),
            Route("/internal/upstreams", self.internal_upstreams, methods=["GET"]),
            *self.accounts.routes(),
            *self.sources_api.routes(),
        ]
        return Starlette(
            routes=routes,
            lifespan=self.lifespan,
            exception_handlers={BadRequest: self._bad_request},
        )

    async def _bad_request(self, request: Request, exc: Exception) -> Response:
        return registry_error(400, "BAD_REQUEST", str(exc))

    # -- guards --------------------------------------------------------------

    def check_internal(self, request: Request) -> bool:
        return _secret_equal(request.headers.get("x-ociproxy-secret"), self.config.secret)

    def check_replication(self, request: Request) -> bool:
        if not self.config.is_primary:
            return False
        peer = self.config.replication_peers.get(request.headers.get("x-ociproxy-peer", ""))
        return bool(peer and _secret_equal(request.headers.get("authorization"), f"Bearer {peer['token']}"))

    async def replication_catalog(self, request: Request) -> Response:
        if not self.check_replication(request):
            return registry_error(403, "DENIED", "invalid replication credential")
        return JSONResponse(self.store.config_sources() or {"protocol": PROTOCOL, "upstreams": [], "helm": []})

    async def replication_publication(self, request: Request) -> Response:
        from .sources import SourceError, spec_from_request
        if not self.check_replication(request):
            return registry_error(403, "DENIED", "invalid replication credential")
        owner = request.headers["x-ociproxy-peer"]
        if not self.config.replication_peers[owner].get("publish", False):
            return registry_error(403, "DENIED", "peer cannot publish catalog entries")
        data = await _json_object(request)
        kind, name = data.get("kind"), data.get("name")
        try:
            spec = spec_from_request(kind, name, data)
            if spec["sharing"] != "public" or spec["visibility"] != "public" or data.get("credentials"):
                raise SourceError("only anonymous public sources can be published")
            row = self.store.get_source(kind, name)
            if row and (row["spec"].get("owner") != owner or row["spec"].get("sharing") != "public"):
                raise SourceError("catalog entry belongs to another owner", 403)
            self.sources.check_conflicts(kind, name, spec)
            if data.get("action") == "delete":
                if not row:
                    raise SourceError("unknown catalog entry", 404)
                await asyncio.to_thread(self.store.delete_source, kind, name, expected_owner=owner)
            else:
                if row and row["spec"].get("base_url") != spec["base_url"]:
                    await asyncio.to_thread(self.store.drop_namespace, name if kind == "oci" else HELM_NS+name)
                await asyncio.to_thread(self.store.put_source, kind, name,
                    {**spec, "owner": owner, "configured": bool(data.get("configured"))}, None, expected_owner=owner)
        except FileExistsError:
            return registry_error(409, "DENIED", "catalog name/prefix conflicts with another source")
        except PermissionError:
            return registry_error(403, "DENIED", "catalog source belongs to another owner")
        except SourceError as exc:
            return registry_error(exc.status, "DENIED", "public catalog conflict" if exc.status == 409 else str(exc))
        return JSONResponse({"ok": True, "protocol": PROTOCOL})

    def _note_replica(self, request: Request, pull: bool = False) -> None:
        """Attribute an internal request to the replica that sent it.

        Replicas stamp X-OCIProxy-Node on every internal call, so any
        request refreshes liveness and actual content pulls are recorded
        separately (last_pull_at). Unknown node ids are ignored.
        """
        node = request.headers.get("x-ociproxy-peer")
        if not node:
            return
        if pull:
            self.store.record_pull(node)
        else:
            self.store.touch_replica(node)

    @staticmethod
    def require_auth(message: str = "authentication required") -> Response:
        return registry_error(401, "UNAUTHORIZED", message,
                              headers={"Www-Authenticate": 'Basic realm="ociproxy"'})

    async def authorize_pull(self, request: Request, path: str) -> Response | None:
        """None if the request may pull ``path``; else the error response.
        Public repos never look at credentials (clients send them anyway)."""
        if self.policy.is_public(path):
            return None
        if await self.auth.authenticate(request) is None:
            return self.require_auth("pull authentication required")
        return None

    async def authorize_push(self, request: Request) -> Principal | Response:
        principal = await self.auth.authenticate(request)
        if principal is None:
            return self.require_auth("push credentials missing or invalid")
        if not role_at_least(principal.role, "pusher"):
            return registry_error(403, "DENIED", f"user {principal.username!r} may not push")
        return principal

    def require_primary(self) -> Response | None:
        if not self.config.is_primary:
            return registry_error(405, "UNSUPPORTED", "only the primary serves this endpoint")
        return None

    # -- public: basic ---------------------------------------------------------

    async def index(self, request: Request) -> Response:
        # No node id or upstream list: this page is public.
        return Response(f"ociproxy {self.config.role}\n")

    async def ping(self, request: Request) -> Response:
        return JSONResponse({}, headers={REGISTRY_VERSION_HEADER[0]: REGISTRY_VERSION_HEADER[1]})

    async def catalog(self, request: Request) -> Response:
        # Repo names are not filtered per visibility: any signed-in user may
        # pull every repo, so the catalog is all-or-nothing.
        if self.policy.anything_private and await self.auth.authenticate(request) is None:
            return self.require_auth()
        n = _int_param(request, "n", 100, 0, 1000)
        last = request.query_params.get("last")
        repos = self.store.catalog(None, last, n) if n else []
        headers = {}
        if n and len(repos) == n:
            headers["Link"] = f'</v2/_catalog?n={n}&last={quote(repos[-1], safe="")}>; rel="next"'
        return JSONResponse({"repositories": repos}, headers=headers)

    async def metrics_endpoint(self, request: Request) -> Response:
        # Same trust as /internal; Prometheus can send it as a bearer token.
        bearer = request.headers.get("authorization", "")
        if not (self.check_internal(request)
                or _secret_equal(bearer, f"Bearer {self.config.secret}")):
            return registry_error(403, "DENIED", "bad secret")
        stats = self.store.blob_stats()
        _, manifests = self.store.list_manifests(limit=0)
        current = self.store.current_serial()
        gauges = [
            ("ociproxy_info", "Node role and id.",
             {"role": self.config.role, "node_id": self.node_id}, 1),
            ("ociproxy_blobs", "Blobs in the store.", {}, stats["count"]),
            ("ociproxy_blob_bytes", "Bytes of blobs in the store.", {}, stats["total_size"]),
            ("ociproxy_manifests", "Manifests in the store.", {}, manifests),
            ("ociproxy_changelog_serial", "Highest changelog serial.", {}, current),
            ("ociproxy_uploads_in_progress", "Open push upload sessions.", {}, len(self.uploads)),
        ]
        if self.config.is_primary:
            for node, url, acked, _lp in self.store.live_replicas():
                gauges.append((
                    "ociproxy_replica_lag",
                    "Changelog events a live replica has not acknowledged.",
                    {"node_id": node, "base_url": url}, max(current - acked, 0),
                ))
        elif self.tail is not None:
            gauges.append(("ociproxy_replica_tail_serial",
                           "Primary changelog serial this replica has applied.", {},
                           self.tail.serial))
        return PlainTextResponse(
            self.metrics.render(gauges), media_type="text/plain; version=0.0.4"
        )

    # -- public: /v2 dispatch ---------------------------------------------------

    async def v2_dispatch(self, request: Request) -> Response:
        rest = request.path_params["rest"]
        method = request.method

        if rest.endswith("/tags/list"):
            if method in ("GET", "HEAD"):
                return await self.list_tags(request, rest[: -len("/tags/list")])
            return registry_error(405, "UNSUPPORTED", f"{method} not allowed on tags")

        if "/manifests/" in rest:
            head, ref = rest.rsplit("/manifests/", 1)
            if method in ("GET", "HEAD"):
                return await self.pull_manifest(request, head, ref)
            if method == "PUT":
                return await self.push_manifest(request, head, ref)
            return registry_error(405, "UNSUPPORTED", f"{method} not allowed on manifests")

        if "/blobs/uploads" in rest:
            return await self.upload_dispatch(request, rest)

        if "/blobs/" in rest:
            head, digest = rest.rsplit("/blobs/", 1)
            if method in ("GET", "HEAD"):
                return await self.pull_blob(request, head, digest)
            return registry_error(405, "UNSUPPORTED", f"{method} not allowed on blobs")

        return registry_error(404, "NAME_UNKNOWN", f"unrecognized v2 path: {rest}")

    def resolve_namespace(self, head: str) -> tuple[str, str]:
        """Map a path head to (upstream_name, repo)."""
        first, _, remainder = head.partition("/")
        u = self.config.upstream_by_prefix(first)
        if u is not None and remainder:
            return u.storage_name, u.normalize_repo(remainder)
        return LOCAL, head

    async def list_tags(self, request: Request, head: str) -> Response:
        """Tags of a repo: pushed tags, plus for mirrored repos the source
        registry's full list merged with what is cached here."""
        if (denied := await self.authorize_pull(request, head)) is not None:
            return denied
        n = _int_param(request, "n", None, 0, 10000)
        last = request.query_params.get("last")
        upstream, repo = self.resolve_namespace(head)
        try:
            tags = await self.resolver.list_tags(upstream, repo)
            if tags is None and upstream == LOCAL:
                dflt = self.config.default_upstream()
                if dflt is not None:
                    tags = await self.resolver.list_tags(dflt.storage_name, dflt.normalize_repo(repo))
        except ResolveError as e:
            if e.status == 404:
                return registry_error(404, "NAME_UNKNOWN", f"repository {head!r} not known")
            return registry_error(e.status, "UNKNOWN", e.detail)
        if tags is None:
            return registry_error(404, "NAME_UNKNOWN", f"repository {head!r} not known")
        if last:
            tags = [t for t in tags if t > last]
        if n is not None:
            tags = tags[:n]
        headers = {REGISTRY_VERSION_HEADER[0]: REGISTRY_VERSION_HEADER[1]}
        if n and len(tags) == n:
            headers["Link"] = (f'</v2/{quote(head, safe="/")}/tags/list?n={n}'
                               f'&last={quote(tags[-1], safe="")}>; rel="next"')
        return JSONResponse({"name": head, "tags": tags}, headers=headers)

    async def pull_manifest(self, request: Request, head: str, ref: str) -> Response:
        if (denied := await self.authorize_pull(request, head)) is not None:
            return denied
        upstream, repo = self.resolve_namespace(head)
        try:
            rec = await self.resolver.resolve_manifest(upstream, repo, ref)
        except ResolveError as e:
            if e.status == 404 and upstream == LOCAL:
                dflt = self.config.default_upstream()
                if dflt is not None:
                    try:
                        rec = await self.resolver.resolve_manifest(dflt.storage_name, dflt.normalize_repo(repo), ref)
                    except ResolveError as e2:
                        return registry_error(e2.status, "MANIFEST_UNKNOWN", e2.detail)
                else:
                    return registry_error(404, "MANIFEST_UNKNOWN", e.detail)
            else:
                return registry_error(e.status, "MANIFEST_UNKNOWN", e.detail)
        headers = {
            "Docker-Content-Digest": rec.digest,
            REGISTRY_VERSION_HEADER[0]: REGISTRY_VERSION_HEADER[1],
        }
        if request.method == "HEAD":
            return Response(
                status_code=200,
                headers={**headers, "content-type": rec.media_type,
                        "content-length": str(len(rec.bytes))},
            )
        return Response(rec.bytes, media_type=rec.media_type, headers=headers)

    async def pull_blob(self, request: Request, head: str, digest: str) -> Response:
        if (denied := await self.authorize_pull(request, head)) is not None:
            return denied
        try:
            parse_digest(digest)
        except ValueError:
            return registry_error(400, "DIGEST_INVALID", f"malformed digest {digest!r}")
        upstream, repo = self.resolve_namespace(head)
        # Blobs share a global content-addressed cache. A public path must
        # not make private layers readable just by knowing their digest.
        # In mixed-visibility installations, anonymous pulls need a cached
        # manifest in this repo that actually references the requested blob.
        proof_fetch = None
        if self.policy.is_public(head):
            belongs = await asyncio.to_thread(self.store.repo_references_blob, upstream, repo, digest)
            if not belongs and upstream == LOCAL:
                dflt = self.config.default_upstream()
                if dflt is not None:
                    belongs = await asyncio.to_thread(
                        self.store.repo_references_blob, dflt.storage_name, dflt.normalize_repo(repo), digest)
            needs_proof = not belongs or (upstream != LOCAL
                and not self.store.get_manifest_by_digest(upstream, repo, digest)
                and not self.resolver.cached_blob_allowed(upstream, repo, digest))
            if needs_proof and await self.auth.authenticate(request) is None:
                target, target_repo = upstream, repo
                if upstream == LOCAL:
                    dflt = self.config.default_upstream()
                    if dflt is None:
                        if not self.store.has_blob(digest):
                            return registry_error(404, "BLOB_UNKNOWN", "blob not found")
                        return self.require_auth("repository ownership is required for this blob")
                    target, target_repo = dflt.storage_name, dflt.normalize_repo(repo)
                try:
                    proof_fetch = await self.resolver.authorize_blob(target, target_repo, digest)
                except ResolveError as exc:
                    return registry_error(exc.status, "BLOB_UNKNOWN", exc.detail)
        # Some clients fetch manifest digests (indexes, children) via the blob
        # endpoint. Serve those with the manifest's real media type — a
        # generic octet-stream here breaks containerd's unpack.
        rec = self.store.get_manifest_by_digest(upstream, repo, digest)
        if rec is None and upstream == LOCAL:
            dflt = self.config.default_upstream()
            if dflt is not None:
                rec = self.store.get_manifest_by_digest(dflt.storage_name, dflt.normalize_repo(repo), digest)
                if rec is not None:
                    upstream = dflt.storage_name
        if rec is not None:
            mheaders = {
                "Docker-Content-Digest": rec.digest,
                REGISTRY_VERSION_HEADER[0]: REGISTRY_VERSION_HEADER[1],
            }
            if request.method == "HEAD":
                return Response(status_code=200, headers={
                    **mheaders, "content-type": rec.media_type,
                    "content-length": str(len(rec.bytes))})
            return Response(rec.bytes, media_type=rec.media_type, headers=mheaders)
        headers = {
            "Docker-Content-Digest": digest,
            REGISTRY_VERSION_HEADER[0]: REGISTRY_VERSION_HEADER[1],
        }
        if proof_fetch is not None:
            if request.method == "HEAD":
                try:
                    path = await proof_fetch.wait_done()
                except ResolveError as exc:
                    return registry_error(exc.status, "BLOB_UNKNOWN", exc.detail)
                return Response(status_code=200, headers={**headers, "content-length":str(path.stat().st_size)})
            if proof_fetch.content_length:
                headers["content-length"] = proof_fetch.content_length
            return StreamingResponse(proof_fetch.stream(), media_type="application/octet-stream", headers=headers)
        # Local fast path: serve straight from the store (Range supported).
        path: Path | None = None
        if self.store.has_blob(digest):
            path = self.store.blob_path(digest)
            self.metrics.inc("ociproxy_blob_requests_total", result="hit")
            if request.method == "HEAD" and upstream == LOCAL:
                # A pusher checking before skipping the upload: keep the blob
                # out of GC until its manifest arrives.
                await asyncio.to_thread(self.store.touch_blob, digest)

        if path is None and request.method == "GET":
            # Stream from the source while teeing into the store: the client
            # sees bytes immediately instead of waiting for the whole
            # download before the first response byte (large layers used to
            # stall the daemon past its header timeout).
            try:
                clen, agen = await self._open_blob_with_fallback(upstream, repo, digest)
            except ResolveError as e:
                return registry_error(e.status, "BLOB_UNKNOWN", e.detail)
            if clen:
                headers["content-length"] = clen
            return StreamingResponse(agen, media_type="application/octet-stream", headers=headers)

        if path is None:
            # HEAD: fully resolve into the store, then answer.
            try:
                path = await self._resolve_blob_with_fallback(upstream, repo, digest)
            except ResolveError as e:
                return registry_error(e.status, "BLOB_UNKNOWN", e.detail)

        if request.method == "HEAD":
            return Response(status_code=200, headers={**headers, "content-length": str(path.stat().st_size)})
        return FileResponse(str(path), media_type="application/octet-stream", headers=headers)

    async def _open_blob_with_fallback(self, upstream: str, repo: str, digest: str):
        try:
            return await self.resolver.open_blob_stream(upstream, repo, digest)
        except ResolveError:
            if upstream != LOCAL:
                raise
            dflt = self.config.default_upstream()
            if dflt is None:
                raise
            return await self.resolver.open_blob_stream(dflt.storage_name, dflt.normalize_repo(repo), digest)

    async def _resolve_blob_with_fallback(self, upstream: str, repo: str, digest: str) -> Path:
        try:
            return await self.resolver.resolve_blob(upstream, repo, digest)
        except ResolveError:
            if upstream != LOCAL:
                raise
            dflt = self.config.default_upstream()
            if dflt is None:
                raise
            return await self.resolver.resolve_blob(dflt.storage_name, dflt.normalize_repo(repo), digest)

    # -- public: classic Helm repos ------------------------------------------------

    @staticmethod
    def _helm_error(e: ResolveError) -> Response:
        # Source-side auth failures must not reach the client as 401/403:
        # Helm would read them as "wrong credentials for this proxy".
        status = e.status if e.status == 404 or e.status >= 500 else 502
        return PlainTextResponse(e.detail + "\n", status_code=status)

    async def helm_index(self, request: Request) -> Response:
        if (denied := await self.authorize_pull(request, f"helm/{request.path_params['name']}")) is not None:
            return denied
        name = request.path_params["name"]
        if not self.helm.knows(name):
            return PlainTextResponse(f"unknown helm repo {name!r}\n", status_code=404)
        try:
            path = await self.helm.index_path(name)
        except ResolveError as e:
            return self._helm_error(e)
        return FileResponse(str(path), media_type="application/x-yaml")

    async def helm_chart(self, request: Request) -> Response:
        if (denied := await self.authorize_pull(request, f"helm/{request.path_params['name']}")) is not None:
            return denied
        p = request.path_params
        name, chart, version, file = p["name"], p["chart"], p["version"], p["file"]
        if not self.helm.knows(name):
            return PlainTextResponse(f"unknown helm repo {name!r}\n", status_code=404)
        if file.endswith(".prov"):
            return PlainTextResponse("provenance files are not proxied\n", status_code=404)
        if not file.endswith(".tgz") or chart == INDEX_REPO:
            return PlainTextResponse("not found\n", status_code=404)
        try:
            path = await self.helm.chart_path(name, chart, version)
        except ResolveError as e:
            return self._helm_error(e)
        return FileResponse(str(path), media_type="application/gzip")

    # -- public: node-local push ---------------------------------------------

    async def push_manifest(self, request: Request, head: str, ref: str) -> Response:
        principal = await self.authorize_push(request)
        if isinstance(principal, Response):
            return principal
        first, _, remainder = head.partition("/")
        if self.config.upstream_by_prefix(first) is not None and remainder:
            return registry_error(400, "DENIED", f"prefix {first!r} is reserved for an upstream mirror")
        body = await request.body()
        try:
            json.loads(body)
        except ValueError:
            return registry_error(400, "MANIFEST_INVALID", "manifest body is not valid JSON")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        media = request.headers.get("content-type", "application/vnd.docker.distribution.manifest.v2+json").split(";")[0].strip()
        await asyncio.to_thread(self.store.put_manifest, LOCAL, head, ref, digest, media, body)
        log.info("pushed manifest %s:%s (%s)", head, ref, digest)
        return Response(status_code=201, headers={"Docker-Content-Digest": digest})

    async def upload_dispatch(self, request: Request, rest: str) -> Response:
        principal = await self.authorize_push(request)
        if isinstance(principal, Response):
            return principal
        # rest looks like '<repo>/blobs/uploads' or '<repo>/blobs/uploads/<uuid>'
        head, _, tail = rest.partition("/blobs/uploads")
        repo = head
        first, _, remainder = head.partition("/")
        if self.config.upstream_by_prefix(first) is not None and remainder:
            return registry_error(400, "DENIED", f"prefix {first!r} is reserved for an upstream mirror")

        if request.method == "POST":
            return await self.upload_start(request, repo, principal)
        if not tail.startswith("/"):
            return registry_error(404, "UNKNOWN_UPLOAD", "no such upload session")
        sid = tail[1:]
        session = self.uploads.get(sid)
        # Another user's session is indistinguishable from a missing one.
        if session is None or session.username != principal.username:
            return registry_error(404, "UNKNOWN_UPLOAD", f"no upload session {sid}")
        if request.method == "PATCH":
            return await self.upload_patch(request, session)
        if request.method == "PUT":
            return await self.upload_put(request, session)
        if request.method == "DELETE":
            session.writer.abort()
            self.uploads.pop(sid, None)
            return Response(status_code=204)
        return registry_error(405, "UNSUPPORTED", request.method)

    async def upload_start(self, request: Request, repo: str, principal: Principal) -> Response:
        mount = request.query_params.get("mount")
        if mount:
            if self.store.has_blob(mount):
                await asyncio.to_thread(self.store.touch_blob, mount)
                return Response(status_code=201, headers={
                    "Docker-Content-Digest": mount,
                    "Location": f"/v2/{repo}/blobs/{mount}",
                })
        if len(self.uploads) >= self.config.upload_max_sessions:
            self.sweep_uploads()
            if len(self.uploads) >= self.config.upload_max_sessions:
                return registry_error(429, "TOOMANYREQUESTS", "too many uploads in progress",
                                      headers={"Retry-After": "60"})
        tmp_path, writer = self.store.new_blob_tmp()
        sid = uuid.uuid4().hex
        self.uploads[sid] = UploadSession(sid, repo, principal.username, tmp_path, writer)
        location = f"/v2/{repo}/blobs/uploads/{sid}"
        return Response(
            status_code=202,
            headers={
                "Location": location,
                "Docker-Upload-UUID": sid,
                "Range": "0-0",
            },
        )

    async def _receive_into(self, request: Request, session: UploadSession) -> None:
        """Stream a request body into a session; it can't expire meanwhile."""
        session.busy += 1
        try:
            async for chunk in request.stream():
                await session.writer.awrite(chunk)
                session.last_active = time.monotonic()
        finally:
            session.busy -= 1
            session.last_active = time.monotonic()

    async def upload_patch(self, request: Request, session: UploadSession) -> Response:
        try:
            await self._receive_into(request, session)
        except Exception as e:
            return registry_error(400, "BAD_REQUEST", f"upload chunk failed: {e}")
        end = max(session.writer.size - 1, 0)
        location = f"/v2/{session.repo}/blobs/uploads/{session.id}"
        return Response(status_code=202, headers={"Location": location, "Range": f"0-{end}"})

    async def upload_put(self, request: Request, session: UploadSession) -> Response:
        digest = request.query_params.get("digest")
        if not digest:
            return registry_error(400, "DIGEST_INVALID", "PUT requires ?digest=")
        try:
            parse_digest(digest)
        except ValueError:
            return registry_error(400, "DIGEST_INVALID", f"malformed digest {digest!r}")
        try:
            await self._receive_into(request, session)
        except Exception as e:
            session.writer.abort()
            self.uploads.pop(session.id, None)
            return registry_error(400, "BAD_REQUEST", f"upload finalize failed: {e}")
        session.writer.close()
        self.uploads.pop(session.id, None)
        if session.writer.digest != digest:
            session.writer.abort()
            return registry_error(400, "DIGEST_INVALID", f"expected {digest}, computed {session.writer.digest}")
        await asyncio.to_thread(self.store.commit_blob, session.tmp_path, digest, session.writer.size)
        log.info("pushed blob %s (%d bytes)", digest, session.writer.size)
        return Response(status_code=201, headers={
            "Docker-Content-Digest": digest,
            "Location": f"/v2/{session.repo}/blobs/{digest}",
        })

    # -- internal ---------------------------------------------------------------

    async def internal_register(self, request: Request) -> Response:
        if not self.check_replication(request):
            return registry_error(403, "DENIED", "bad secret")
        err = self.require_primary()
        if err:
            return err
        data = await _json_object(request)
        if data.get("protocol") != PROTOCOL:
            return registry_error(409, "DENIED", "public-v2 protocol required")
        node_id = request.headers["x-ociproxy-peer"]
        base_url = _str_field(data, "base_url")
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BadRequest("base_url must be an absolute http(s) URL")
        self.store.register_replica(node_id, base_url)
        log.info("replica %s registered at %s", node_id[:8], base_url)
        return JSONResponse({"ok": True, "protocol": PROTOCOL, "current": self.store.current_serial()})

    async def internal_changelog(self, request: Request) -> Response:
        if not self.check_replication(request):
            return registry_error(403, "DENIED", "bad secret")
        err = self.require_primary()
        if err:
            return err
        since = _int_param(request, "since", 0, 0)
        wait = _float_param(request, "wait", 25.0, 0.0, 60.0)
        node = request.headers.get("x-ociproxy-peer")
        if node:
            # The replica has applied everything up to `since`.
            self.store.ack_serial(node, since)
        deadline = time.monotonic() + wait
        generation = request.query_params.get("sync_generation", self.sync_generation)
        # A replica that goes away must not pin this handler (and delay
        # server shutdown) for the rest of the long-poll.
        disconnected = asyncio.ensure_future(_wait_disconnect(request))
        try:
            while True:
                token = self.changes.token()
                events, scanned = self.store.public_events(since)
                remaining = deadline - time.monotonic()
                if (scanned > since or events or remaining <= 0 or disconnected.done()
                        or generation != self.sync_generation):
                    return JSONResponse({
                        "events": [event_to_json(e) for e in events],
                        "protocol": PROTOCOL, "scanned": scanned,
                        "current": self.store.current_serial(),
                        "node_id": self.node_id,
                        "sync_generation": self.sync_generation,
                    })
                changed = asyncio.ensure_future(token.wait())
                await asyncio.wait(
                    {changed, disconnected}, timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                changed.cancel()
        finally:
            disconnected.cancel()

    async def internal_pull_manifest(self, request: Request) -> Response:
        if not self.check_replication(request):
            return registry_error(403, "DENIED", "bad secret")
        err = self.require_primary()
        if err:
            return err
        self._note_replica(request, pull=True)
        upstream = request.query_params.get("upstream", LOCAL)
        repo = request.query_params.get("repo", "")
        ref = request.query_params.get("reference", "")
        if not shared_repo(self.config, upstream, repo):
            return registry_error(404, "NAME_UNKNOWN", "not a shared public repository")
        try:
            if upstream.startswith(HELM_NS):
                rec = await self.helm.record(upstream, repo, ref)
            else:
                rec = await self.resolver.resolve_manifest(upstream, repo, ref)
        except ResolveError as e:
            return registry_error(e.status, "MANIFEST_UNKNOWN", e.detail)
        return Response(
            rec.bytes,
            media_type=rec.media_type,
            headers={
                "Docker-Content-Digest": rec.digest,
                "X-OCIProxy-Upstream": rec.upstream,
                "X-OCIProxy-Repo": rec.repo,
            },
        )

    async def internal_pull_tags(self, request: Request) -> Response:
        if not self.check_replication(request):
            return registry_error(403, "DENIED", "bad secret")
        err = self.require_primary()
        if err:
            return err
        self._note_replica(request, pull=True)
        upstream = request.query_params.get("upstream", LOCAL)
        repo = request.query_params.get("repo", "")
        if not shared_repo(self.config, upstream, repo):
            return registry_error(404, "NAME_UNKNOWN", "not a shared public repository")
        try:
            tags = await self.resolver.list_tags(upstream, repo)
        except ResolveError as e:
            return registry_error(e.status, "NAME_UNKNOWN", e.detail)
        if tags is None:
            return registry_error(404, "NAME_UNKNOWN", f"{upstream}/{repo} not known")
        return JSONResponse({"tags": tags})

    async def internal_pull_blob(self, request: Request) -> Response:
        if not self.check_replication(request):
            return registry_error(403, "DENIED", "bad secret")
        err = self.require_primary()
        if err:
            return err
        self._note_replica(request, pull=True)
        digest = request.path_params["digest"]
        try:
            parse_digest(digest)
        except ValueError:
            return registry_error(400, "DIGEST_INVALID", f"malformed digest {digest!r}")
        upstream = request.query_params.get("upstream", LOCAL)
        repo = request.query_params.get("repo", "")
        proof_fetch = None
        if upstream == "@public-feed":
            permitted = self.store.public_blob(digest)
        else:
            permitted = shared_repo(self.config, upstream, repo)
            if permitted:
                try:
                    proof_fetch = await self.resolver.authorize_blob(upstream, repo, digest)
                except ResolveError as exc:
                    return registry_error(exc.status, "BLOB_UNKNOWN", exc.detail)
        if not permitted:
            return registry_error(404, "BLOB_UNKNOWN", "not a referenced public blob")
        if proof_fetch is not None:
            if request.method == "HEAD":
                try:
                    path = await proof_fetch.wait_done()
                except ResolveError as exc:
                    return registry_error(exc.status, "BLOB_UNKNOWN", exc.detail)
                return Response(status_code=200, headers={"content-length":str(path.stat().st_size)})
            headers = {"Docker-Content-Digest":digest}
            if proof_fetch.content_length:
                headers["content-length"] = proof_fetch.content_length
            return StreamingResponse(proof_fetch.stream(),media_type="application/octet-stream",headers=headers)
        # Stream from upstream while teeing into the primary's store so the
        # replica (and ultimately its client) sees bytes immediately even
        # when the primary itself is cold on this blob.
        if self.store.has_blob(digest):
            return FileResponse(str(self.store.blob_path(digest)),
                               media_type="application/octet-stream",
                               headers={"Docker-Content-Digest": digest})
        if request.method == "GET":
            try:
                clen, agen = await self.resolver.open_blob_stream(upstream, repo, digest)
            except ResolveError as e:
                return registry_error(e.status, "BLOB_UNKNOWN", e.detail)
            headers = {"Docker-Content-Digest": digest}
            if clen:
                headers["content-length"] = clen
            return StreamingResponse(agen, media_type="application/octet-stream", headers=headers)
        try:
            path = await self.resolver.resolve_blob(upstream, repo, digest)
        except ResolveError as e:
            return registry_error(e.status, "BLOB_UNKNOWN", e.detail)
        return Response(status_code=200,
                       headers={"Docker-Content-Digest": digest,
                                "content-length": str(path.stat().st_size)})

    # -- internal: admin surface (web UI) -------------------------------------

    def _sanitized_upstreams(self) -> list[dict]:
        """Upstream descriptors safe to show the UI: never credentials."""
        return self.sources.describe(self.policy)

    async def internal_sync(self, request: Request) -> Response:
        self.accounts._acting(request, "admin")
        if self.config.is_primary:
            self.sync_generation = uuid.uuid4().hex
            self.changes.notify()
            message = "Connected replicas have been prompted to sync."
        elif self.tail is not None:
            await self.tail.request_sync()
            message = "Public catalog and content sync requested."
        else:
            return registry_error(503, "UNAVAILABLE", "replication is not running")
        return JSONResponse({"message": message}, status_code=202)

    async def internal_overview(self, request: Request) -> Response:
        if not self.check_internal(request):
            return registry_error(403, "DENIED", "invalid internal secret")
        _, manifest_count = self.store.list_manifests(limit=0)
        replicas = [
            {"node_id": n, "base_url": b, "acked_serial": a, "last_pull_at": p}
            for n, b, a, p in self.store.live_replicas()
        ]
        data = {
            "role": self.config.role,
            "node_id": self.node_id,
            "managed_sources": self.sources.managed_enabled,
            "upstreams": self._sanitized_upstreams(),
            "replicas": replicas,
            "blobs": self.store.blob_stats(),
            "manifests": manifest_count,
        }
        if self.config.is_primary:
            data["serial"] = self.store.current_serial()
        else:
            # How far this replica has applied the primary's changelog.
            data["primary_url"] = self.config.primary_url
            data["primary_connected"] = bool(self.tail and self.tail.connected)
            data["primary_last_contact_at"] = self.tail.last_contact_at if self.tail else None
            data["primary_serial"] = self.tail.primary_serial if self.tail else None
            data["synced_serial"] = (max(self.tail.serial, self.tail.caught_up_to)
                                     if self.tail is not None
                                     else int(self.store.get_meta(TAIL_SERIAL_KEY) or 0))
        return JSONResponse(data)

    async def internal_images(self, request: Request) -> Response:
        if not self.check_internal(request):
            return registry_error(403, "DENIED", "invalid internal secret")
        limit = _int_param(request, "limit", 200, 0, 1000)
        offset = _int_param(request, "offset", 0, 0)
        items, total = self.store.list_manifests(limit=limit, offset=offset)
        return JSONResponse({"items": items, "total": total, "returned": len(items)})

    async def internal_upstreams(self, request: Request) -> Response:
        if not self.check_internal(request):
            return registry_error(403, "DENIED", "invalid internal secret")
        return JSONResponse({"items": self._sanitized_upstreams()})


def create_app(config: Config) -> Starlette:
    return App(config).app
