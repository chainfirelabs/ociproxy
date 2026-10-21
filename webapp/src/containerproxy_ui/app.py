"""Node-local admin UI for either primary or replica.

CONTAINERPROXY_URL selects this UI's own backend; CONTAINERPROXY_SECRET is that
node's local administration/session secret. UI_NODE_URL is a legacy alias that
selects the local backend for every operation. Authentication, OIDC provisioning,
users, tokens and private source management never consult the primary's admin
API. UI_PUBLIC_URL enables Secure cookies with HTTPS and supplies OIDC callback
URLs. UI_SESSION_HOURS defaults to 12; UI_LISTEN/UI_PORT control the listener.
"""

from __future__ import annotations

import html
import logging
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib import resources
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .index_html import INDEX_HTML
from .oidc import OIDCClient, OIDCError, OIDCSettings, new_flow
from .session import Signer

log = logging.getLogger("containerproxy.ui")

SESSION_COOKIE = "cp_session"
FLOW_COOKIE = "cp_oidc"
FLOW_MAX_AGE = 600
# Cross-site forms can't set custom headers, and without CORS headers a
# cross-origin fetch with one is refused at preflight: this header on every
# state-changing call is the CSRF defence (with SameSite=Lax cookies).
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "containerproxy"

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "no-referrer",  # the OIDC callback URL carries a code
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


@lru_cache(maxsize=1)
def _logo_bytes() -> bytes:
    return (resources.files(__package__) / "logo.png").read_bytes()


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class UI:
    def __init__(self, proxy_url: str, secret: str, public_url: str | None = None,
                 oidc: OIDCSettings | None = None, session_hours: float = 12.0,
                 node_url: str | None = None):
        # UI_NODE_URL is a legacy alias for the local backend, never a second
        # node whose account authority or private inventory is consulted.
        self.proxy_url = (node_url or proxy_url).rstrip("/")
        self.node_url = None
        self.secret = secret
        self.public_url = public_url.rstrip("/") if public_url else None
        self.session_max_age = session_hours * 3600
        self.sessions = Signer(secret, f"session:{self.proxy_url}")
        self.flows = Signer(secret, f"oidc-flow:{self.proxy_url}")
        # Internal traffic: never honor HTTP_PROXY/HTTPS_PROXY. The proxy
        # lives on the container network; a corporate proxy cannot resolve it.
        self.http = httpx.AsyncClient(
            base_url=self.proxy_url, timeout=httpx.Timeout(15.0, connect=5.0), trust_env=False,
        )
        self.node_http = httpx.AsyncClient(
            base_url=self.node_url, timeout=httpx.Timeout(10.0, connect=5.0), trust_env=False,
        ) if self.node_url else None
        # The identity provider is external: environment proxies apply.
        self.idp_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self.oidc = OIDCClient(oidc, self.idp_http) if oidc else None

    async def aclose(self) -> None:
        await self.http.aclose()
        if self.node_http is not None:
            await self.node_http.aclose()
        await self.idp_http.aclose()

    # -- plumbing ---------------------------------------------------------------

    @property
    def _secure_cookies(self) -> bool:
        return bool(self.public_url and self.public_url.startswith("https://"))

    def _set_cookie(self, resp: Response, name: str, value: str, max_age: float) -> None:
        resp.set_cookie(name, value, max_age=int(max_age), httponly=True, samesite="lax",
                        secure=self._secure_cookies, path="/")

    async def _proxy(self, method: str, path: str, *, user: str | None = None,
                     headers: dict | None = None, **kw) -> httpx.Response | JSONResponse:
        headers = {**(headers or {}), "X-ContainerProxy-Secret": self.secret}
        if user:
            headers["X-ContainerProxy-Acting-User"] = user
        try:
            return await self.http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as e:
            return _error(502, f"proxy unreachable at {self.proxy_url}: {e.__class__.__name__}")

    @staticmethod
    def _passthrough(resp: httpx.Response | JSONResponse) -> Response:
        """Preserve the proxy's status and body so real errors surface."""
        if isinstance(resp, Response):
            return resp
        headers = {}
        if ct := resp.headers.get("content-type"):
            headers["content-type"] = ct
        return Response(content=resp.content, status_code=resp.status_code, headers=headers)

    async def _current_user(self, request: Request) -> dict | Response:
        """The signed-in user as the primary sees them now, or an error."""
        session = self.sessions.loads(request.cookies.get(SESSION_COOKIE))
        if session is None:
            return _error(401, "not signed in")
        resp = await self._proxy("GET", "/internal/me", user=session.get("u"))
        if isinstance(resp, Response):
            return resp
        if resp.status_code == 403:  # deleted or disabled since sign-in
            return _error(401, "session is no longer valid")
        if resp.status_code != 200:
            return self._passthrough(resp)
        return resp.json()

    def _csrf_ok(self, request: Request) -> bool:
        return request.headers.get(CSRF_HEADER) == CSRF_VALUE

    def route(self, handler, *, admin: bool = False):
        """Wrap an API handler: CSRF check, sign-in, optional admin role."""
        async def endpoint(request: Request) -> Response:
            if request.method not in ("GET", "HEAD") and not self._csrf_ok(request):
                return _error(403, "missing CSRF header")
            user = await self._current_user(request)
            if isinstance(user, Response):
                return user
            if admin and user["role"] != "admin":
                return _error(403, "admins only")
            return await handler(request, user)
        return endpoint

    # -- pages ------------------------------------------------------------------

    async def index(self, request: Request) -> Response:
        return HTMLResponse(INDEX_HTML, headers=SECURITY_HEADERS)

    async def logo(self, request: Request) -> Response:
        return Response(content=_logo_bytes(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    def _auth_error_page(self, message: str, status: int = 400) -> HTMLResponse:
        body = (f"<!DOCTYPE html><meta charset=utf-8><title>Sign-in failed</title>"
                f"<body style='font:15px sans-serif;background:#0b0b0d;color:#ede6df;padding:40px'>"
                f"<h2>Sign-in failed</h2><p>{html.escape(message)}</p><p><a style='color:#c84e23' "
                f"href='/'>Back to ContainerProxy</a></p></body>")
        resp = HTMLResponse(body, status_code=status, headers=SECURITY_HEADERS)
        resp.delete_cookie(FLOW_COOKIE, path="/")
        return resp

    # -- sign-in ----------------------------------------------------------------

    async def auth_config(self, request: Request) -> Response:
        return JSONResponse({
            "oidc": self.oidc is not None,
            "provider": self.oidc.s.provider_name if self.oidc else None,
        })

    async def login(self, request: Request) -> Response:
        if not self._csrf_ok(request):
            return _error(403, "missing CSRF header")
        try:
            data = await request.json()
        except ValueError:
            return _error(400, "body is not valid JSON")
        if not isinstance(data, dict):
            return _error(400, "body must be a JSON object")
        resp = await self._proxy("POST", "/internal/auth/login", json={
            "username": data.get("username"), "password": data.get("password")})
        if isinstance(resp, Response):
            return resp
        if resp.status_code != 200:
            return self._passthrough(resp)
        user = resp.json()
        out = JSONResponse(user)
        self._set_cookie(out, SESSION_COOKIE,
                         self.sessions.dumps({"u": user["username"]}, self.session_max_age),
                         self.session_max_age)
        return out

    async def logout(self, request: Request) -> Response:
        if not self._csrf_ok(request):
            return _error(403, "missing CSRF header")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    async def oidc_start(self, request: Request) -> Response:
        if self.oidc is None:
            return self._auth_error_page("single sign-on is not configured", 404)
        flow = new_flow()
        try:
            url = await self.oidc.authorize_url(flow)
        except OIDCError as e:
            return self._auth_error_page(str(e), 502)
        resp = RedirectResponse(url, status_code=302)
        self._set_cookie(resp, FLOW_COOKIE, self.flows.dumps(flow, FLOW_MAX_AGE), FLOW_MAX_AGE)
        return resp

    async def oidc_callback(self, request: Request) -> Response:
        if self.oidc is None:
            return self._auth_error_page("single sign-on is not configured", 404)
        q = request.query_params
        flow = self.flows.loads(request.cookies.get(FLOW_COOKIE))
        if flow is None:
            return self._auth_error_page("the sign-in attempt expired; please try again")
        if q.get("error"):
            return self._auth_error_page(f"the provider refused the sign-in: {q.get('error')}")
        if not q.get("code") or q.get("state") != flow.get("state"):
            return self._auth_error_page("invalid sign-in response (state mismatch)")
        try:
            claims = await self.oidc.exchange(q["code"], flow)
            username, role = self.oidc.username(claims), self.oidc.role(claims)
        except OIDCError as e:
            return self._auth_error_page(str(e), 403)
        resp = await self._proxy("POST", "/internal/auth/oidc",
                                 json={"username": username, "role": role})
        if isinstance(resp, Response) or resp.status_code != 200:
            detail = "the registry is unreachable" if isinstance(resp, Response) else \
                resp.json().get("error", f"status {resp.status_code}")
            return self._auth_error_page(f"can't sign in as {username!r}: {detail}", 403)
        out = RedirectResponse("/", status_code=302)
        out.delete_cookie(FLOW_COOKIE, path="/")
        self._set_cookie(out, SESSION_COOKIE,
                         self.sessions.dumps({"u": username}, self.session_max_age),
                         self.session_max_age)
        log.info("OIDC sign-in: %s (%s)", username, role)
        return out

    # -- API ----------------------------------------------------------------------

    async def me(self, request: Request, user: dict) -> Response:
        return JSONResponse(user)

    def _as_user(self, method: str, path: str, *, body: bool = False):
        """Handler forwarding to an /internal account endpoint as the user."""
        async def handler(request: Request, user: dict) -> Response:
            kw = {}
            if body:
                kw["content"] = await request.body()
                kw["headers"] = {"content-type": "application/json"}
            target = path.format(**{k: quote(v, safe="") for k, v in request.path_params.items()})
            resp = await self._proxy(method, target, user=user["username"], **kw)
            return self._passthrough(resp)
        return handler

    @staticmethod
    def _by_method(handlers: dict):
        async def handler(request: Request, user: dict) -> Response:
            return await handlers[request.method](request, user)
        return handler

    async def overview(self, request: Request, user: dict) -> Response:
        resp = await self._proxy("GET", "/internal/overview")
        if isinstance(resp, Response) or resp.status_code != 200:
            return self._passthrough(resp)
        data = resp.json()
        if data.get("role") == "replica":
            data["primary"] = {"url": data.get("primary_url"),
                               "registered": data.get("primary_connected", False),
                               "serial": data.get("primary_serial"),
                               "last_contact_at": data.get("primary_last_contact_at"),
                               "synced_serial": data.get("synced_serial")}
        return JSONResponse(data)

    def _read_only(self, path: str, *, query: bool = False):
        async def handler(request: Request, user: dict) -> Response:
            kw = {"params": dict(request.query_params)} if query else {}
            return self._passthrough(await self._proxy("GET", path, **kw))
        return handler


def create_app(proxy_url: str | None = None, secret: str | None = None, *,
               public_url: str | None = None, oidc: OIDCSettings | None = None,
               node_url: str | None = None) -> Starlette:
    public_url = public_url or os.environ.get("UI_PUBLIC_URL") or None
    if oidc is None:
        oidc = OIDCSettings.from_env(public_url)
    secret = secret if secret is not None else os.environ.get("CONTAINERPROXY_SECRET", "")
    if not secret:
        raise ValueError("CONTAINERPROXY_SECRET is required (it also keys UI sessions)")
    ui = UI(
        proxy_url or os.environ.get("CONTAINERPROXY_URL", "http://primary:5000"),
        secret,
        public_url=public_url,
        oidc=oidc,
        session_hours=float(os.environ.get("UI_SESSION_HOURS", "12")),
        node_url=node_url or os.environ.get("UI_NODE_URL") or None,
    )
    r = ui.route
    routes = [
        Route("/", ui.index),
        Route("/logo.png", ui.logo, methods=["GET"]),
        Route("/api/auth/config", ui.auth_config, methods=["GET"]),
        Route("/api/auth/login", ui.login, methods=["POST"]),
        Route("/api/auth/logout", ui.logout, methods=["POST"]),
        Route("/auth/oidc/start", ui.oidc_start, methods=["GET"]),
        Route("/auth/oidc/callback", ui.oidc_callback, methods=["GET"]),
        Route("/api/me", r(ui.me), methods=["GET"]),
        Route("/api/me/password", r(ui._as_user("POST", "/internal/me/password", body=True)),
              methods=["POST"]),
        Route("/api/me/tokens", r(ui._as_user("POST", "/internal/me/tokens", body=True)),
              methods=["POST"]),
        Route("/api/me/tokens/{token_id}", r(ui._as_user("DELETE", "/internal/me/tokens/{token_id}")),
              methods=["DELETE"]),
        Route("/api/overview", r(ui.overview), methods=["GET"]),
        Route("/api/sync", r(ui._as_user("POST", "/internal/sync"), admin=True), methods=["POST"]),
        Route("/api/images", r(ui._read_only("/internal/images", query=True)), methods=["GET"]),
        Route("/api/upstreams", r(ui._read_only("/internal/upstreams")), methods=["GET"]),
        Route("/api/users", r(ui._by_method({
            "GET": ui._as_user("GET", "/internal/users"),
            "POST": ui._as_user("POST", "/internal/users", body=True),
        }), admin=True), methods=["GET", "POST"]),
        Route("/api/users/{username}", r(ui._by_method({
            "PATCH": ui._as_user("PATCH", "/internal/users/{username}", body=True),
            "DELETE": ui._as_user("DELETE", "/internal/users/{username}"),
        }), admin=True), methods=["PATCH", "DELETE"]),
        Route("/api/users/{username}/tokens",
              r(ui._as_user("GET", "/internal/users/{username}/tokens"), admin=True), methods=["GET"]),
        Route("/api/tokens/{token_id}",
              r(ui._as_user("DELETE", "/internal/tokens/{token_id}"), admin=True), methods=["DELETE"]),
        # Upstreams / Helm repos added in the UI (config-file ones are read-only).
        Route("/api/sources", r(ui._as_user("POST", "/internal/sources", body=True), admin=True),
              methods=["POST"]),
        Route("/api/sources/test",
              r(ui._as_user("POST", "/internal/sources/test", body=True), admin=True), methods=["POST"]),
        Route("/api/sources/{kind}/{name}", r(ui._by_method({
            "PUT": ui._as_user("PUT", "/internal/sources/{kind}/{name}", body=True),
            "DELETE": ui._as_user("DELETE", "/internal/sources/{kind}/{name}"),
        }), admin=True), methods=["PUT", "DELETE"]),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        await ui.aclose()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.ui = ui
    return app
