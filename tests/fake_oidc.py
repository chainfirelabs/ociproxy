"""Minimal OpenID Connect provider for tests (Authentik-shaped).

/authorize immediately "signs in" ``self.user`` and redirects back with a
code. /token checks the client credentials and the PKCE verifier, like a
real provider, and returns an unsigned id_token (the UI validates claims,
not signatures, because it talks to the token endpoint directly).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

CLIENT_ID = "ociproxy"
CLIENT_SECRET = "oidc-client-secret"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeOIDC:
    def __init__(self):
        self.port = 0
        self.user = {"sub": "u-1", "preferred_username": "olga", "groups": ["registry-users"]}
        self.claim_overrides: dict = {}  # applied to the id_token (tamper tests)
        self.groups_in_userinfo_only = False
        self._codes: dict[str, dict] = {}
        self._server: uvicorn.Server | None = None
        self.app = Starlette(routes=[
            Route("/application/o/registry/.well-known/openid-configuration", self.discovery),
            Route("/authorize", self.authorize),
            Route("/token", self.token, methods=["POST"]),
            Route("/userinfo", self.userinfo),
        ])

    @property
    def issuer(self) -> str:
        return f"http://127.0.0.1:{self.port}/application/o/registry"

    async def discovery(self, request: Request) -> Response:
        base = f"http://127.0.0.1:{self.port}"
        return JSONResponse({
            "issuer": self.issuer + "/",
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "userinfo_endpoint": f"{base}/userinfo",
        })

    async def authorize(self, request: Request) -> Response:
        q = request.query_params
        assert q["client_id"] == CLIENT_ID and q["response_type"] == "code"
        assert q["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self._codes[code] = {"nonce": q["nonce"], "challenge": q["code_challenge"],
                             "redirect_uri": q["redirect_uri"]}
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}")

    async def token(self, request: Request) -> Response:
        auth = request.headers.get("authorization", "")
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if auth != expected:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        grant = self._codes.pop(form.get("code"), None)
        if grant is None or form.get("redirect_uri") != grant["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        challenge = _b64(hashlib.sha256(form["code_verifier"].encode()).digest())
        if challenge != grant["challenge"]:
            return JSONResponse({"error": "invalid_grant", "detail": "pkce"}, status_code=400)
        now = int(time.time())
        claims = {"iss": self.issuer + "/", "aud": CLIENT_ID, "exp": now + 300, "iat": now,
                  "nonce": grant["nonce"], **self.user}
        if self.groups_in_userinfo_only:
            claims.pop("groups", None)
        claims.update(self.claim_overrides)
        id_token = ".".join([_b64(b'{"alg":"RS256"}'), _b64(json.dumps(claims).encode()), "sig"])
        return JSONResponse({"access_token": "at-" + secrets.token_hex(8), "id_token": id_token,
                             "token_type": "Bearer"})

    async def userinfo(self, request: Request) -> Response:
        if not request.headers.get("authorization", "").startswith("Bearer at-"):
            return Response(status_code=401)
        return JSONResponse(self.user)

    def start(self) -> None:
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                                     log_level="warning"))
        threading.Thread(target=self._server.run, daemon=True).start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("fake OIDC provider did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
