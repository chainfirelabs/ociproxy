"""Merge independent node-local sources with an explicitly public catalog.

Private credentials use this node's own AES-GCM key and never leave the node.
Local sources survive catalog updates; conflicts fail closed instead of replacing
local private definitions. Source names are stable IDs within each source kind.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from dataclasses import dataclass, field

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import Config, HelmRepoConfig, UpstreamConfig, parse_helm_repo, parse_upstream
from .helm import HelmRepoClient
from .store import HELM_NS, LOCAL, SOURCE_TYPES, Store
from .upstream import UpstreamClient

log = logging.getLogger("containerproxy.sources")

# Names a managed upstream may not take: "local" is the namespace of pushed
# images, "helm" the first path segment of Helm repo visibility rules.
RESERVED_NAMES = frozenset({LOCAL, HELM_NS.rstrip("/")})

_SPEC_KEYS = {
    "oci": ("base_url", "prefix", "official_namespace", "default", "visibility", "sharing"),
    "helm": ("base_url", "visibility", "sharing"),
}


class SourceError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class CredentialBox:
    """AES-256-GCM, key = HKDF-SHA256(node secret). The entry's kind/name is
    authenticated data, so a ciphertext can't be moved to another entry."""

    VERSION = "v1"

    def __init__(self, secret: str):
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None,
            info=b"containerproxy managed source credentials v1",
        ).derive(secret.encode())
        self._aead = AESGCM(key)

    def seal(self, kind: str, name: str, username: str, password: str) -> str:
        nonce = os.urandom(12)
        plain = json.dumps({"username": username, "password": password}).encode()
        ct = self._aead.encrypt(nonce, plain, f"{kind}/{name}".encode())
        return f"{self.VERSION}:" + base64.b64encode(nonce + ct).decode()

    def open(self, kind: str, name: str, sealed: str) -> tuple[str, str] | None:
        """(username, password), or None if it can't be decrypted (the node
        secret changed, or the value was tampered with)."""
        version, _, data = sealed.partition(":")
        if version != self.VERSION:
            return None
        try:
            raw = base64.b64decode(data, validate=True)
            plain = self._aead.decrypt(raw[:12], raw[12:], f"{kind}/{name}".encode())
            d = json.loads(plain)
            return d["username"], d["password"]
        except (ValueError, InvalidTag, KeyError, TypeError):
            return None


@dataclass
class ManagedEntry:
    kind: str
    name: str
    spec: dict
    created_at: float
    updated_at: float
    has_credentials: bool = False
    credentials_unreadable: bool = False
    shadowed_by: str | None = None
    # Parsed config (credentials included) when the entry is valid.
    parsed: UpstreamConfig | HelmRepoConfig | None = field(default=None, repr=False)


def spec_from_request(kind: str, name: str, data: dict) -> dict:
    """Validate an admin's create/update body into a stored spec (no
    credentials). Raises SourceError."""
    if kind not in SOURCE_TYPES:
        raise SourceError('kind must be "oci" or "helm"')
    if not isinstance(name, str) or not name:
        raise SourceError("name is required")
    allowed = _SPEC_KEYS[kind]
    spec = {k: data[k] for k in allowed if data.get(k) not in (None, "")}
    for key in ("base_url", "prefix", "official_namespace", "visibility", "sharing"):
        if key in spec and not isinstance(spec[key], str):
            raise SourceError(f"{key} must be a string")
    if "default" in spec and not isinstance(spec["default"], bool):
        raise SourceError("default must be true or false")
    if not spec.get("default"):
        spec.pop("default", None)
    if "base_url" not in spec:
        raise SourceError("base_url is required")
    try:
        url = httpx.URL(spec["base_url"])
    except httpx.InvalidURL:
        raise SourceError("base_url is not a valid URL") from None
    if url.userinfo:
        raise SourceError("put credentials in the credentials fields, not in base_url")
    if url.query or url.fragment:
        raise SourceError("base_url must not have a query string or fragment")
    try:
        parse = parse_upstream if kind == "oci" else parse_helm_repo
        parsed = parse({"name": name, **spec})
    except (ValueError, KeyError) as e:
        raise SourceError(str(e)) from None
    spec["sharing"] = parsed.sharing
    spec["visibility"] = parsed.visibility
    spec["base_url"] = parsed.base_url  # normalized (no trailing slash)
    if kind == "oci":
        for value in {name, parsed.prefix}:
            if value.lower() in RESERVED_NAMES:
                raise SourceError(f"{value!r} is reserved")
    return spec


def config_snapshot(config: Config) -> dict:
    """What replicas need of the primary's config-file sources: routing and
    visibility, never credentials."""
    return {
        "protocol": "public-v2",
        "upstreams": [
            {"name": u.name, "base_url": u.base_url, "prefix": u.prefix,
             "official_namespace": u.official_namespace, "default": u.default,
             "visibility": "public", "sharing": "public", "id": f"oci:{u.name}"}
            for u in config.upstreams if u.sharing == "public"
        ],
        "helm": [{"name": r.name, "base_url": r.base_url, "visibility": "public", "sharing": "public", "id": f"helm:{r.name}"}
                 for r in config.helm_repos if r.sharing == "public"],
    }


def _parse_snapshot(snapshot: dict) -> tuple[list[UpstreamConfig], list[HelmRepoConfig]]:
    if snapshot.get("protocol") != "public-v2":
        raise ValueError("legacy catalog rejected")
    upstreams, helm_repos = [], []
    for kind, parse, out in (("upstreams", parse_upstream, upstreams),
                             ("helm", parse_helm_repo, helm_repos)):
        for entry in snapshot.get(kind) or []:
            try:
                if any(k in entry for k in ("credentials", "username", "password")):
                    raise ValueError("credentials are forbidden in public catalogs")
                if entry.get("sharing") != "public" or entry.get("visibility") != "public":
                    raise ValueError("catalog contains a nonpublic source")
                out.append(parse(entry))
            except (ValueError, KeyError, TypeError) as e:
                raise ValueError("invalid public catalog entry") from e
    return upstreams, helm_repos


class SourceRegistry:
    """Merged view of config and managed sources; see the module docstring."""

    def __init__(self, store: Store, config: Config, http: httpx.AsyncClient,
                 upstream_clients: dict, helm_clients: dict):
        self.store = store
        self.config = config
        self.http = http
        self.upstream_clients = upstream_clients
        self.helm_clients = helm_clients
        # This node's own config-file entries.
        self.local_upstreams = list(config.upstreams)
        self.local_helm = list(config.helm_repos)
        # The config entries in effect: the local ones on the primary, the
        # primary's (once received) on a replica.
        self.static_upstreams = list(self.local_upstreams)
        self.static_helm = list(self.local_helm)
        self.managed_enabled = config.managed_sources
        self.box = CredentialBox(config.secret)
        self.managed: list[ManagedEntry] = []
        self._snapshot: dict | None = None
        self._lock = threading.RLock()
        self.catalog_items: dict[tuple[str, str], dict] = {}
        store.add_source_listener(self.reload)
        self.reload()

    def _use_primary_config(self) -> None:
        snapshot = self.store.config_sources()
        if snapshot is None:
            return
        upstreams, helm = _parse_snapshot(snapshot)
        self.catalog_items = {
            (kind, entry["name"]): entry
            for kind, key in (("oci", "upstreams"), ("helm", "helm"))
            for entry in snapshot.get(key, [])
        }
        # A node's private/configured sources always remain local. Conflicting
        # shared names/prefixes are not imported, and their content is rejected.
        local_rows = self.store.list_sources()
        local_names = {u.name for u in self.local_upstreams} | {u.prefix for u in self.local_upstreams}
        local_names |= {r["name"] for r in local_rows if r["kind"] == "oci"}
        local_names |= {r["spec"].get("prefix", r["name"]) for r in local_rows if r["kind"] == "oci"}
        shared = [u for u in upstreams if not ({u.name, u.prefix} & local_names)]
        # Identical explicitly shared config entries are allowed and use the
        # primary's definition; a local private definition can never be replaced.
        for u in upstreams:
            mine = next((x for x in self.local_upstreams if x.name == u.name), None)
            if mine and mine.sharing == "public" and mine.base_url == u.base_url and mine.prefix == u.prefix:
                shared.append(u)
        private_config = [u for u in self.local_upstreams if u.sharing == "local"]
        self.static_upstreams = private_config + list({u.name: u for u in shared}.values())
        local_helm_names = {r.name for r in self.local_helm} | {r["name"] for r in local_rows if r["kind"] == "helm"}
        self.static_helm = [r for r in self.local_helm if r.sharing == "local"] + [
            r for r in helm if r.name not in local_helm_names or any(
                x.name == r.name and x.sharing == "public" and x.base_url == r.base_url for x in self.local_helm)]
        self._snapshot = snapshot

    # -- merge -----------------------------------------------------------------

    def _static_owner(self, kind: str, name: str, prefix: str | None) -> str | None:
        """Name of the config entry a managed one collides with, if any."""
        if kind == "helm":
            return name if any(r.name == name for r in self.static_helm) else None
        mine = {name, prefix or name}
        for u in self.static_upstreams:
            if mine & {u.name, u.prefix}:
                return u.name
        return None

    def _load(self, row: dict) -> ManagedEntry:
        kind, name = row["kind"], row["name"]
        entry = ManagedEntry(kind, name, row["spec"], row["created_at"], row["updated_at"])
        creds = None
        if row["credentials"]:
            creds = self.box.open(kind, name, row["credentials"])
            entry.has_credentials = creds is not None
            entry.credentials_unreadable = creds is None
            if creds is None:
                log.warning("credentials of %s source %r can't be decrypted (node secret "
                            "changed?); using it anonymously until they are re-entered", kind, name)
        try:
            parse = parse_upstream if kind == "oci" else parse_helm_repo
            body = {"name": name, **row["spec"]}
            if creds is not None:
                body["username"], body["password"] = creds
            entry.parsed = parse(body)
        except (ValueError, KeyError) as e:
            log.error("ignoring invalid %s source %r: %s", kind, name, e)
        prefix = getattr(entry.parsed, "prefix", None)
        entry.shadowed_by = self._static_owner(kind, name, prefix)
        return entry

    def reload(self) -> None:
        """Rebuild the merged lists and clients from config + the store."""
        with self._lock:
            if not self.config.is_primary:
                self._use_primary_config()
            managed = [self._load(r) for r in self.store.list_sources()]
            upstreams = list(self.static_upstreams)
            helm_repos = list(self.static_helm)
            if self.managed_enabled:
                has_default = any(u.default for u in upstreams)
                for e in managed:
                    if e.parsed is None or e.shadowed_by:
                        continue
                    if e.kind == "oci":
                        if e.parsed.default and has_default:
                            e.parsed.default = False
                        has_default = has_default or e.parsed.default
                        upstreams.append(e.parsed)
                    else:
                        helm_repos.append(e.parsed)
            self.managed = managed
            self.config.upstreams[:] = upstreams
            self.config.helm_repos[:] = helm_repos
            _sync(self.upstream_clients, {u.storage_name: u for u in upstreams},
                  lambda c: c.upstream, lambda u: UpstreamClient(u, self.http))
            _sync(self.helm_clients, {r.name: r for r in helm_repos},
                  lambda c: c.repo, lambda r: HelmRepoClient(r, self.http))
            if self.config.is_primary:
                snapshot = config_snapshot(self.config)
                for kind, key in (("oci", "upstreams"), ("helm", "helm")):
                    for item in snapshot[key]:
                        row = next((e for e in managed if e.kind == kind and e.name == item["name"]), None)
                        item["owner"] = row.spec.get("owner", "") if row else ""
                        item["managed"] = row is not None and not row.spec.get("configured", False)
                self.store.publish_config_sources(snapshot)
            # Catalog removals, scope changes and URL changes revoke cached
            # shared manifests before they can be served on the next request.
            active = {u.name: u.base_url for u in upstreams if u.sharing == "public"}
            active.update({HELM_NS+r.name: r.base_url for r in helm_repos if r.sharing == "public"})
            for namespace, old_url in getattr(self, "_shared_urls", {}).items():
                if active.get(namespace) != old_url:
                    self.store.drop_namespace(namespace)
            self._shared_urls = active

    # -- queries ---------------------------------------------------------------

    def get_managed(self, kind: str, name: str) -> ManagedEntry | None:
        return next((e for e in self.managed if e.kind == kind and e.name == name), None)

    def is_static(self, kind: str, name: str) -> bool:
        pool = self.static_upstreams if kind == "oci" else self.static_helm
        return any(x.name == name for x in pool)

    def resolve(self, kind: str, name: str) -> UpstreamConfig | HelmRepoConfig | None:
        """The active entry of that kind and name (config or managed)."""
        if kind == "oci":
            return next((u for u in self.config.upstreams if u.name == name), None)
        return self.config.helm_repo(name)

    def check_conflicts(self, kind: str, name: str, spec: dict) -> None:
        """Refuse a managed entry that would collide with another entry or
        hide pushed images. Raises SourceError(409)."""
        if kind == "helm":
            if self.is_static("helm", name):
                raise SourceError(f"helm repo {name!r} is defined in the configuration", 409)
            return
        prefix = spec.get("prefix") or name
        mine = {name, prefix}
        owner = self._static_owner("oci", name, prefix)
        if owner:
            raise SourceError(f"name/prefix collides with configured upstream {owner!r}", 409)
        for e in self.managed:
            if e.kind != "oci" or e.name == name:
                continue
            theirs = {e.name, e.spec.get("prefix") or e.name}
            if mine & theirs:
                raise SourceError(f"name/prefix collides with upstream {e.name!r}", 409)
        if spec.get("default"):
            other = next((u.name for u in self.config.upstreams
                          if u.default and u.name != name), None)
            if other:
                raise SourceError(f"{other!r} is already the default upstream; "
                                  "unset it there first", 409)
        current = self.get_managed("oci", name)
        current_prefix = (current.spec.get("prefix") or name) if current else None
        if prefix != current_prefix and self.store.has_local_repos_under(prefix):
            raise SourceError(f"pushed images live under {prefix}/; an upstream with this "
                              "prefix would hide them", 409)

    def describe(self, policy) -> list[dict]:
        """Every entry for the admin UI, config first. Never credentials."""
        items = []
        for u in self.static_upstreams:
            items.append(_describe_oci(u, policy, managed=False))
        for e in self.managed:
            if e.kind != "oci":
                continue
            items.append(self._describe_managed(e, policy))
        for r in self.static_helm:
            items.append(_describe_helm(r, policy, managed=False))
        for e in self.managed:
            if e.kind != "helm":
                continue
            items.append(self._describe_managed(e, policy))
        for item in items:
            catalog = self.catalog_items.get((item["kind"], item["name"]), {})
            item["owner"] = catalog.get("owner", "")
            if not self.config.is_primary and item.get("sharing") == "public":
                item["managed"] = bool(catalog.get("managed") and catalog.get("owner") == self.config.replication_id)
                item["editable"] = item["managed"]
            else:
                item["editable"] = item["managed"]
        return items

    def _describe_managed(self, e: ManagedEntry, policy) -> dict:
        active = (self.managed_enabled and not e.shadowed_by and e.parsed is not None)
        if e.kind == "oci":
            if e.parsed is not None:
                d = _describe_oci(e.parsed, policy if active else None, managed=True)
            else:
                d = {"kind": "oci", "name": e.name, "base_url": e.spec.get("base_url"),
                     "prefix": e.spec.get("prefix") or e.name,
                     "official_namespace": e.spec.get("official_namespace"),
                     "default": bool(e.spec.get("default")), "visibility": None,
                     "own_visibility": e.spec.get("visibility")}
        else:
            if e.parsed is not None:
                d = _describe_helm(e.parsed, policy if active else None, managed=True)
            else:
                d = {"kind": "helm", "name": e.name, "base_url": e.spec.get("base_url"),
                     "prefix": HELM_NS + e.name, "official_namespace": None, "default": False,
                     "visibility": None, "own_visibility": e.spec.get("visibility")}
        d.update(
            has_credentials=e.has_credentials,
            credentials_unreadable=e.credentials_unreadable,
            active=active,
            shadowed_by=e.shadowed_by,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
        return d


def _describe_oci(u: UpstreamConfig, policy, *, managed: bool) -> dict:
    return {
        "kind": "oci", "name": u.name, "base_url": u.base_url, "prefix": u.prefix,
        "official_namespace": u.official_namespace, "default": u.default,
        "has_credentials": bool(u.username),
        "visibility": policy.visibility(f"{u.prefix}/_") if policy else u.visibility,
        "sharing": u.sharing, "id": f"oci:{u.name}", "own_visibility": u.visibility, "managed": managed, "active": True,
    }


def _describe_helm(r: HelmRepoConfig, policy, *, managed: bool) -> dict:
    return {
        "kind": "helm", "name": r.name, "base_url": r.base_url, "prefix": HELM_NS + r.name,
        "official_namespace": None, "default": False, "has_credentials": bool(r.username),
        "visibility": policy.visibility(HELM_NS + r.name) if policy else r.visibility,
        "sharing": r.sharing, "id": f"helm:{r.name}", "own_visibility": r.visibility, "managed": managed, "active": True,
    }


def _sync(clients: dict, wanted: dict, config_of, make) -> None:
    """Make ``clients`` hold one client per wanted entry, keeping a client
    whose config did not change. Mutates in place (shared with resolvers)."""
    for name in list(clients):
        if name not in wanted:
            clients.pop(name, None)
    for name, cfg in wanted.items():
        current = clients.get(name)
        if current is None or config_of(current) != cfg:
            clients[name] = make(cfg)


async def probe(http: httpx.AsyncClient, kind: str, base_url: str,
                auth: tuple[str, str] | None, *, proxied: bool = False) -> dict:
    """Can the primary reach this source (through the upstream proxy)?

    OCI: ``GET <base>/v2/`` must answer 200 or 401 (a registry asking for a
    token). Helm: ``GET <base>/index.yaml`` must answer 200. Only status and
    headers are read."""
    url = f"{base_url}/v2/" if kind == "oci" else f"{base_url}/index.yaml"
    try:
        req = http.build_request("GET", url, timeout=httpx.Timeout(15.0, connect=10.0))
        resp = await http.send(req, stream=True, auth=auth if kind == "helm" else None)
        await resp.aclose()
    except httpx.ProxyError as e:
        return {"ok": False, "url": url, "detail": f"upstream proxy refused the request: {e}"}
    except httpx.TimeoutException:
        where = "the upstream proxy or " if proxied else ""
        return {"ok": False, "url": url,
                "detail": f"timed out: {where}{httpx.URL(url).host} is unreachable"}
    except httpx.HTTPError as e:
        return {"ok": False, "url": url, "detail": f"{e.__class__.__name__}: {e}"}
    status = resp.status_code
    if kind == "oci":
        ok = status == 200 or (status == 401 and "www-authenticate" in resp.headers)
        detail = ("registry reachable" if ok else
                  f"HTTP {status}: not an OCI registry API at this URL")
    else:
        ok = status == 200
        detail = ("index.yaml reachable" if ok else
                  "HTTP 401/403: credentials missing or wrong" if status in (401, 403) else
                  f"HTTP {status}")
    return {"ok": ok, "url": url, "status": status, "detail": detail}
