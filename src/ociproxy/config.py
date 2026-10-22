"""Node-local TOML/environment configuration.

OCIPROXY_* environment overrides win over TOML. SECRET, bootstrap accounts,
private upstreams, Helm sources, access rules and managed-source settings belong
only to this node. Public-v2 uses separate REPLICATION_ID/REPLICATION_TOKEN on
replicas and REPLICATION_PEERS (JSON allowlist) on the primary. Sources default
to sharing=local and visibility=private. See docs/ISOLATION.md for migration/setup.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field

import tomllib

ENV_PREFIX = "OCIPROXY_"

# Upstream names/prefixes and Helm repo names: one URL path segment.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

VISIBILITIES = ("public", "private")


def _visibility(value, where: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    v = str(value).strip().lower()
    if v not in VISIBILITIES:
        raise ValueError(f'{where}: visibility must be "public" or "private", got {value!r}')
    return v


def _env(name: str) -> str | None:
    v = os.environ.get(ENV_PREFIX + name)
    return v if v not in (None, "") else None


@dataclass
class UpstreamConfig:
    name: str
    base_url: str
    prefix: str
    username: str | None = None
    password: str | None = None
    default: bool = False
    # Docker-Hub-style official images: a single-segment repo gets this
    # namespace prepended ("nginx" -> "library/nginx"). None = no rewrite.
    official_namespace: str | None = None
    # "public" | "private"; None inherits the default visibility.
    visibility: str | None = "private"
    sharing: str = "local"

    @property
    def storage_name(self) -> str:
        return self.name if self.sharing == "public" else f"private:{self.name}"

    def normalize_repo(self, repo: str) -> str:
        if self.official_namespace and "/" not in repo:
            return f"{self.official_namespace}/{repo}"
        return repo


@dataclass
class HelmRepoConfig:
    """A classic HTTP Helm chart repository (index.yaml + .tgz files),
    served at /helm/<name>/."""
    name: str
    base_url: str
    username: str | None = None
    password: str | None = None
    visibility: str | None = "private"
    sharing: str = "local"

    @property
    def storage_name(self) -> str:
        return self.name if self.sharing == "public" else f"private:{self.name}"


@dataclass
class AccessRule:
    """Visibility for client-visible repo paths matching a glob, e.g.
    "internal/*", "ghcr/myorg/*", "helm/stable". ``*`` also matches "/"."""
    path: str
    visibility: str


@dataclass
class Config:
    role: str  # "primary" | "replica"
    storage_path: str
    listen: str = "127.0.0.1"
    port: int = 5001
    secret: str = ""
    primary_url: str | None = None
    # Separate from secret: only public replication endpoints accept these.
    replication_id: str = ""
    replication_token: str = ""
    replication_peers: dict = field(default_factory=dict)
    push_username: str | None = None
    push_password: str | None = None
    upstreams: list[UpstreamConfig] = field(default_factory=list)
    helm_repos: list[HelmRepoConfig] = field(default_factory=list)
    advertised_url: str | None = None
    # Explicit proxy for upstream registry fetches. Unset -> httpx trust_env
    # picks up HTTP_PROXY / HTTPS_PROXY / NO_PROXY from the environment.
    # Internal node-to-node traffic is NEVER proxied.
    upstream_proxy: str | None = None
    # Visibility of anything no access rule / upstream / helm repo sets:
    # "public" (anonymous pulls) or "private" (any signed-in user).
    default_visibility: str = "public"
    access_rules: list[AccessRule] = field(default_factory=list)
    # Push upload sessions: dropped after this long without data; capped.
    upload_idle_timeout: float = 900.0
    upload_max_sessions: int = 256
    # Seconds a cached tag (not a digest) is served before it is revalidated
    # against its source; 0 revalidates on every pull. Digests are immutable
    # and never revalidated.
    tag_ttl: float = 300.0
    # Maintenance (see store.gc / store.compact_changelog).
    gc_interval: float = 3600.0
    gc_manifest_max_idle: float = 0.0  # seconds; 0 = never expire manifests
    gc_blob_grace: float = 86400.0
    # Upstreams / Helm repos added in the web UI (see sources.py). When off,
    # stored ones are ignored and the UI can't add more.
    managed_sources: bool = True

    @property
    def is_primary(self) -> bool:
        return self.role == "primary"

    def upstream_by_prefix(self, prefix: str) -> UpstreamConfig | None:
        for u in self.upstreams:
            if u.prefix == prefix:
                return u
        return None

    def default_upstream(self) -> UpstreamConfig | None:
        for u in self.upstreams:
            if u.default:
                return u
        return None

    def helm_repo(self, name: str) -> HelmRepoConfig | None:
        for r in self.helm_repos:
            if r.name == name:
                return r
        return None


def _sharing(entry: dict) -> str:
    sharing = entry.get("sharing", "local")
    if sharing not in ("local", "public"):
        raise ValueError('sharing must be "local" or "public"')
    return sharing


def validate_source_scope(source) -> None:
    if source.sharing not in ("local", "public"):
        raise ValueError('sharing must be "local" or "public"')
    if source.sharing == "public" and (
        source.visibility != "public" or source.username or source.password
    ):
        raise ValueError("shared sources require visibility=public and anonymous upstream access")
    from urllib.parse import urlsplit
    url = urlsplit(source.base_url)
    if url.username or url.password:
        raise ValueError("base_url must not contain credentials")


def parse_upstream(entry: dict) -> UpstreamConfig:
    """One upstream entry (file, env or web UI), validated on its own."""
    u = UpstreamConfig(
        name=entry["name"],
        base_url=entry["base_url"].rstrip("/"),
        prefix=entry.get("prefix") or entry["name"],
        username=entry.get("username"),
        password=entry.get("password"),
        default=bool(entry.get("default", False)),
        official_namespace=entry.get("official_namespace") or None,
    )
    u.sharing = _sharing(entry)
    u.visibility = _visibility(entry.get("visibility", "private"), f"upstream {u.name}")
    validate_source_scope(u)
    if not u.base_url.startswith(("http://", "https://")):
        raise ValueError(f"upstream {u.name}: base_url must be http(s)")
    for value in (u.name, u.prefix):
        if not _NAME_RE.fullmatch(value):
            raise ValueError(f"upstream {u.name}: invalid name/prefix {value!r}")
    if u.official_namespace is not None and not _NAME_RE.fullmatch(u.official_namespace):
        raise ValueError(f"upstream {u.name}: invalid official_namespace {u.official_namespace!r}")
    return u


def parse_helm_repo(entry: dict) -> HelmRepoConfig:
    """One classic Helm repo entry (file, env or web UI), validated on its own."""
    r = HelmRepoConfig(
        name=entry["name"],
        base_url=entry["base_url"].rstrip("/"),
        username=entry.get("username"),
        password=entry.get("password"),
    )
    r.sharing = _sharing(entry)
    r.visibility = _visibility(entry.get("visibility", "private"), f"helm repo {r.name}")
    validate_source_scope(r)
    if not _NAME_RE.fullmatch(r.name):
        raise ValueError(f"helm repo {r.name!r}: invalid name")
    if not r.base_url.startswith(("http://", "https://")):
        raise ValueError(f"helm repo {r.name}: base_url must be http(s)")
    return r


def _parse_upstreams(entries: list[dict]) -> list[UpstreamConfig]:
    upstreams: list[UpstreamConfig] = []
    seen: set[str] = set()
    for entry in entries:
        u = parse_upstream(entry)
        for other in seen:
            if other == u.prefix or other == u.name:
                raise ValueError(f"upstream {u.name}: duplicate name/prefix")
        seen.add(u.prefix)
        seen.add(u.name)
        upstreams.append(u)
    return upstreams


def _parse_helm_repos(entries: list[dict]) -> list[HelmRepoConfig]:
    repos: list[HelmRepoConfig] = []
    for entry in entries:
        r = parse_helm_repo(entry)
        if any(other.name == r.name for other in repos):
            raise ValueError(f"helm repo {r.name}: duplicate name")
        repos.append(r)
    return repos


def _number(name: str, value, *, minimum: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if v < minimum:
        raise ValueError(f"{name} must be >= {minimum:g}, got {v:g}")
    return v


def load_config(
    path: str | None = None,
    *,
    role: str | None = None,
    listen: str | None = None,
    port: int | None = None,
) -> Config:
    """Load TOML + environment. ``role``/``listen``/``port`` are CLI
    overrides: they win over everything and go through the same validation."""
    raw: dict = {}
    if path:
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    node = raw.get("node", {})
    server = raw.get("server", {})
    storage = raw.get("storage", {})
    auth = raw.get("auth", {})

    role = role or _env("ROLE") or node.get("role", "primary")
    if role not in ("primary", "replica"):
        raise ValueError(f"role must be 'primary' or 'replica', got {role!r}")

    replication = raw.get("replication", {})
    replication_id = _env("REPLICATION_ID") or replication.get("id", "")
    replication_token = _env("REPLICATION_TOKEN") or replication.get("token", "")
    replication_peers = replication.get("peers", {})
    if _env("REPLICATION_PEERS"):
        replication_peers = json.loads(_env("REPLICATION_PEERS"))
    if not isinstance(replication_peers, dict):
        raise ValueError("replication.peers must be a mapping of peer IDs to token/publish settings")
    for peer_id, peer in replication_peers.items():
        if not _NAME_RE.fullmatch(peer_id) or not isinstance(peer, dict) or not peer.get("token"):
            raise ValueError("each replication peer needs a valid ID and a nonempty token")
        if not isinstance(peer["token"], str) or not isinstance(peer.get("publish", False), bool):
            raise ValueError("peer token must be a string and publish must be a boolean")
    primary_url = _env("PRIMARY_URL") or node.get("primary_url")
    if role == "replica" and not primary_url:
        raise ValueError("replicas require a primary: set OCIPROXY_PRIMARY_URL or node.primary_url")
    if role == "primary" and primary_url:
        raise ValueError("primary nodes must not set a primary_url")

    listen = listen or _env("LISTEN") or server.get("listen", "127.0.0.1")
    port = int(_number("port", port or _env("PORT") or server.get("port", 5001), minimum=1))
    if port > 65535:
        raise ValueError(f"port must be <= 65535, got {port}")
    secret = _env("SECRET") or node.get("secret", "")
    storage_path = os.path.abspath(_env("STORAGE_PATH") or storage.get("path", "./var"))

    advertised = _env("ADVERTISED_URL") or node.get("advertised_url")
    if role == "replica" and not advertised:
        # In containers the hostname is the service name (unique per scaled
        # replica), for the replication status display.
        advertised = f"http://{socket.gethostname()}:{port}"

    push_username = _env("PUSH_USERNAME") or auth.get("push_username")
    push_password = _env("PUSH_PASSWORD") or auth.get("push_password")

    access = raw.get("access", {})
    default_visibility = _env("DEFAULT_VISIBILITY") or access.get("default_visibility")
    if default_visibility is None:
        # Older spelling: pull_auth = "none" (public) | "basic" (private).
        pull_auth = (_env("PULL_AUTH") or auth.get("pull_auth") or "none").strip().lower()
        if pull_auth not in ("none", "basic"):
            raise ValueError(f'pull_auth must be "none" or "basic", got {pull_auth!r}')
        default_visibility = "private" if pull_auth == "basic" else "public"
    default_visibility = _visibility(default_visibility, "access.default_visibility", required=True)
    access_rules = []
    for i, rule in enumerate(access.get("rule", [])):
        path = rule.get("path")
        if not isinstance(path, str) or not path.strip("/"):
            raise ValueError(f"access.rule[{i}]: path must be a non-empty string")
        access_rules.append(AccessRule(
            path.strip("/"), _visibility(rule.get("visibility"), f"access.rule[{i}]", required=True),
        ))

    uploads = raw.get("uploads", {})
    upload_idle_timeout = _number(
        "uploads.idle_timeout", _env("UPLOAD_IDLE_TIMEOUT") or uploads.get("idle_timeout", 900),
        minimum=1,
    )
    upload_max_sessions = int(_number(
        "uploads.max_sessions", _env("UPLOAD_MAX_SESSIONS") or uploads.get("max_sessions", 256),
        minimum=1,
    ))

    cache = raw.get("cache", {})
    tag_ttl = _number("tag_ttl", _env("TAG_TTL") or cache.get("tag_ttl", 300))

    gc = raw.get("gc", {})
    gc_interval = _number("gc.interval", _env("GC_INTERVAL") or gc.get("interval", 3600))
    gc_idle_days = _number(
        "gc.manifest_max_idle_days",
        _env("GC_MANIFEST_MAX_IDLE_DAYS") or gc.get("manifest_max_idle_days", 0),
    )
    gc_blob_grace = _number("gc.blob_grace", _env("GC_BLOB_GRACE") or gc.get("blob_grace", 86400))

    network = raw.get("network", {})

    sources = raw.get("sources", {})
    managed = _env("MANAGED_SOURCES")
    if managed is None:
        managed_sources = bool(sources.get("managed", True))
    elif managed.strip().lower() in ("1", "true", "yes", "on"):
        managed_sources = True
    elif managed.strip().lower() in ("0", "false", "no", "off"):
        managed_sources = False
    else:
        raise ValueError(f"OCIPROXY_MANAGED_SOURCES must be true or false, got {managed!r}")

    upstream_proxy = _env("UPSTREAM_PROXY") or network.get("upstream_proxy")

    env_upstreams = _env("UPSTREAMS")

    if env_upstreams is not None:
        try:
            entries = json.loads(env_upstreams)
        except ValueError as e:
            raise ValueError(f"OCIPROXY_UPSTREAMS is not valid JSON: {e}")
        upstreams = _parse_upstreams(entries)
    else:
        upstreams = _parse_upstreams(raw.get("upstream", []))

    env_helm = _env("HELM_REPOS")
    if env_helm is not None:
        try:
            helm_entries = json.loads(env_helm)
        except ValueError as e:
            raise ValueError(f"OCIPROXY_HELM_REPOS is not valid JSON: {e}")
        helm_repos = _parse_helm_repos(helm_entries)
    else:
        helm_repos = _parse_helm_repos(raw.get("helm", []))

    return Config(
        role=role,
        storage_path=storage_path,
        listen=listen,
        port=port,
        secret=secret,
        upstream_proxy=upstream_proxy,
        default_visibility=default_visibility,
        access_rules=access_rules,
        upload_idle_timeout=upload_idle_timeout,
        upload_max_sessions=upload_max_sessions,
        tag_ttl=tag_ttl,
        gc_interval=gc_interval,
        gc_manifest_max_idle=gc_idle_days * 86400,
        gc_blob_grace=gc_blob_grace,
        managed_sources=managed_sources,
        replication_id=replication_id,
        replication_token=replication_token,
        replication_peers=replication_peers,
        primary_url=primary_url.rstrip("/") if primary_url else None,
        push_username=push_username,
        push_password=push_password,
        upstreams=upstreams,
        helm_repos=helm_repos,
        advertised_url=advertised,
    )
