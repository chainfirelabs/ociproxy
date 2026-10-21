"""Client for upstream registry v2 (OCI distribution) endpoints.

Handles the standard bearer-token challenge flow (realm/service/scope), token
caching with expiry, optional basic credentials, and CDN redirect following.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

import httpx

from .config import UpstreamConfig
DEFAULT_ACCEPT = (
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.oci.image.index.v1+json"
)

_CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')

# tags/list paging: page size asked of the registry, and a bound on how many
# pages one listing may follow.
TAGS_PAGE_SIZE = 1000
TAGS_MAX_PAGES = 50


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"upstream returned {status}: {detail}")


@dataclass
class FetchedManifest:
    media_type: str
    bytes: bytes
    digest: str


class UpstreamClient:
    def __init__(self, upstream: UpstreamConfig, http: httpx.AsyncClient):
        self.upstream = upstream
        self.http = http
        # (realm, service, scope) -> (token, expires_at)
        self._tokens: dict[tuple[str, str, str], tuple[str, float]] = {}

    def _url(self, path: str) -> str:
        base = httpx.URL(self.upstream.base_url)
        try:
            target = (httpx.URL(f"{self.upstream.base_url}{path}")
                      if path.startswith("/") and not path.startswith("//") else base.join(path))
        except httpx.InvalidURL:
            raise UpstreamError(502, "invalid upstream pagination URL") from None
        if (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port):
            raise UpstreamError(502, "upstream pagination URL must stay on the registry origin")
        if target.username or target.password:
            raise UpstreamError(502, "upstream pagination URL must not contain credentials")
        return str(target)

    async def _authorized(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
        **kw,
    ) -> httpx.Response:
        """Issue a request, transparently satisfying one bearer challenge.

        With stream=True the returned response body is unread; the caller
        must iterate and close it. httpx's client.request() always buffers
        the whole body, which is exactly what must be avoided for blobs.
        """
        headers = dict(headers or {})

        async def go(extra_auth=None) -> httpx.Response:
            req = self.http.build_request(method, self._url(path), headers=headers, **kw)
            return await self.http.send(req, stream=stream, auth=extra_auth)

        resp = await go()
        if resp.status_code != 401:
            return resp

        challenge = resp.headers.get("www-authenticate", "")
        if not challenge.lower().startswith("bearer"):
            # Basic challenge or unknown: retry once with basic creds if we have them.
            if self.upstream.username:
                await resp.aclose()
                return await go((self.upstream.username, self.upstream.password or ""))
            raise UpstreamError(401, f"unauthorized by {self.upstream.name}: {challenge!r}")

        params = dict(_CHALLENGE_RE.findall(challenge))
        token = await self._get_token(
            params.get("realm", ""), params.get("service", ""), params.get("scope", "")
        )
        headers["Authorization"] = f"Bearer {token}"
        await resp.aclose()
        return await go()

    async def _get_token(self, realm: str, service: str, scope: str) -> str:
        if not realm:
            raise UpstreamError(401, "bearer challenge without realm")
        try:
            token_url = httpx.URL(realm)
        except httpx.InvalidURL:
            raise UpstreamError(502, "unsafe upstream token endpoint") from None
        if (token_url.scheme not in ("http", "https") or not token_url.host
                or token_url.username or token_url.password
                or (httpx.URL(self.upstream.base_url).scheme == "https"
                    and token_url.scheme != "https")):
            raise UpstreamError(502, "unsafe upstream token endpoint")
        key = (realm, service, scope)
        cached = self._tokens.get(key)
        if cached and cached[1] > time.time():
            return cached[0]

        params = {}
        if service:
            params["service"] = service
        if scope:
            params["scope"] = scope
        auth = None
        if self.upstream.username:
            auth = (self.upstream.username, self.upstream.password or "")
        resp = await self.http.get(realm, params=params, auth=auth)
        if resp.status_code != 200:
            raise UpstreamError(resp.status_code, f"token request failed: {resp.text[:200]}")
        data = resp.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise UpstreamError(502, "token response missing token")
        expires_in = int(data.get("expires_in", 300))
        # Refresh 30s early to avoid boundary failures.
        self._tokens[key] = (token, time.time() + max(expires_in - 30, 10))
        return token

    async def fetch_manifest(self, repo: str, reference: str) -> FetchedManifest:
        # Always ask for every manifest type, never the pulling client's
        # Accept: the result is cached for ALL clients, and a narrow Accept
        # (e.g. schema2 only) makes the registry return a single-platform
        # manifest instead of the multi-arch index.
        resp = await self._authorized(
            "GET",
            f"/v2/{repo}/manifests/{reference}",
            headers={"Accept": DEFAULT_ACCEPT},
        )
        if resp.status_code != 200:
            raise UpstreamError(resp.status_code, f"manifest {repo}@{reference}: {resp.text[:200]}")
        body = resp.content
        digest = resp.headers.get("docker-content-digest") or "sha256:" + hashlib.sha256(body).hexdigest()
        return FetchedManifest(
            media_type=resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip(),
            bytes=body,
            digest=digest,
        )

    async def head_manifest(self, repo: str, reference: str) -> str | None:
        """Current digest of a manifest, via HEAD (not counted against Docker
        Hub pull rate limits). None if the registry omits the digest header."""
        resp = await self._authorized(
            "HEAD",
            f"/v2/{repo}/manifests/{reference}",
            headers={"Accept": DEFAULT_ACCEPT},
        )
        if resp.status_code != 200:
            raise UpstreamError(resp.status_code, f"manifest {repo}@{reference}: HEAD failed")
        return resp.headers.get("docker-content-digest")

    async def open_blob(self, repo: str, digest: str) -> httpx.Response:
        """Start a streaming blob download. Caller must close/aiter the response."""
        # identity: the digest covers the blob's own bytes, and Content-Length
        # is passed through to clients, so no transfer compression.
        resp = await self._authorized(
            "GET", f"/v2/{repo}/blobs/{digest}", stream=True,
            headers={"Accept-Encoding": "identity"},
        )
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            raise UpstreamError(resp.status_code, f"blob {digest}: {body[:200]!r}")
        return resp

    async def list_tags(self, repo: str) -> list[str]:
        """Every tag of a repo, following ``Link: rel="next"`` pagination."""
        tags: list[str] = []
        path = f"/v2/{repo}/tags/list?n={TAGS_PAGE_SIZE}"
        for _ in range(TAGS_MAX_PAGES):
            resp = await self._authorized("GET", path)
            if resp.status_code != 200:
                raise UpstreamError(resp.status_code, f"tags {repo}: {resp.text[:200]}")
            try:
                page = resp.json().get("tags") or []
            except (ValueError, AttributeError):
                raise UpstreamError(502, f"tags {repo}: malformed response")
            tags.extend(t for t in page if isinstance(t, str))
            m = _LINK_NEXT_RE.search(resp.headers.get("link", ""))
            if not m:
                break
            path = m.group(1)
        return tags
