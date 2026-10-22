"""OpenID Connect sign-in (authorization code flow + PKCE), e.g. Authentik.

The UI is a confidential client: the code is exchanged server-side with the
client secret, directly at the provider's token endpoint over TLS. Per
OIDC Core 3.1.3.7 the TLS server validation then stands in for checking the
id_token signature, so no JWT/crypto library is needed; the claims are
still validated (iss, aud/azp, exp, nonce). That is only sound over HTTPS,
so a plain-http issuer is refused unless it is a loopback address or
OIDC_ALLOW_HTTP is set.

Roles come from the provider's groups claim on every sign-in:
  OIDC_ADMIN_GROUP  -> admin
  OIDC_PUSHER_GROUP -> pusher
  otherwise         -> reader, if OIDC_READER_GROUP is unset or the user is in it
                       (else the sign-in is refused)

Authentik: create an OAuth2/OpenID provider (confidential client, redirect
URI <UI_PUBLIC_URL>/auth/oidc/callback, scopes openid/profile/email) and
use its "OpenID Configuration Issuer" URL as OIDC_ISSUER. Its default
profile scope includes the ``groups`` claim.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

LEEWAY = 60.0
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class OIDCError(Exception):
    """Sign-in failed; the message is safe to show the user."""


@dataclass
class OIDCSettings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    provider_name: str = "single sign-on"
    scopes: str = "openid profile email"
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    admin_group: str | None = None
    pusher_group: str | None = None
    reader_group: str | None = None
    allow_http: bool = False

    @classmethod
    def from_env(cls, public_url: str | None) -> OIDCSettings | None:
        env = os.environ.get
        issuer = env("OIDC_ISSUER")
        if not issuer:
            return None
        missing = [k for k in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET") if not env(k)]
        if not public_url:
            missing.append("UI_PUBLIC_URL")
        if missing:
            raise ValueError(f"OIDC_ISSUER is set but {', '.join(missing)} is missing")
        return cls(
            issuer=issuer.rstrip("/"),
            client_id=env("OIDC_CLIENT_ID"),
            client_secret=env("OIDC_CLIENT_SECRET"),
            redirect_uri=f"{public_url.rstrip('/')}/auth/oidc/callback",
            provider_name=env("OIDC_PROVIDER_NAME") or "single sign-on",
            scopes=env("OIDC_SCOPES") or "openid profile email",
            username_claim=env("OIDC_USERNAME_CLAIM") or "preferred_username",
            groups_claim=env("OIDC_GROUPS_CLAIM") or "groups",
            admin_group=env("OIDC_ADMIN_GROUP") or None,
            pusher_group=env("OIDC_PUSHER_GROUP") or None,
            reader_group=env("OIDC_READER_GROUP") or None,
            allow_http=(env("OIDC_ALLOW_HTTP") or "").lower() in ("1", "true", "yes"),
        )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_flow() -> dict:
    """state, nonce and PKCE verifier for one sign-in attempt."""
    return {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24),
            "verifier": secrets.token_urlsafe(48)}


def _challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise OIDCError("the provider returned a malformed id_token") from None
    if not isinstance(claims, dict):
        raise OIDCError("the provider returned a malformed id_token")
    return claims


class OIDCClient:
    def __init__(self, settings: OIDCSettings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self._meta: dict | None = None
        self._meta_at = 0.0
        url = httpx.URL(settings.issuer)
        if url.scheme != "https" and not settings.allow_http and url.host not in _LOOPBACK:
            raise ValueError("OIDC_ISSUER must be https (set OIDC_ALLOW_HTTP=true to override)")

    async def metadata(self) -> dict:
        if self._meta is None or time.time() - self._meta_at > 3600:
            try:
                r = await self.http.get(f"{self.s.issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                meta = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise OIDCError(f"can't reach the identity provider: {e.__class__.__name__}") from None
            if str(meta.get("issuer", "")).rstrip("/") != self.s.issuer:
                raise OIDCError("the provider's issuer does not match OIDC_ISSUER")
            # Discovery is remote input: an HTTPS issuer must not direct
            # client credentials or access tokens to an insecure endpoint.
            for name in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
                endpoint = meta.get(name)
                if endpoint is None and name == "userinfo_endpoint":
                    continue
                try:
                    url = httpx.URL(endpoint)
                except (TypeError, httpx.InvalidURL):
                    raise OIDCError(f"the provider returned an invalid {name}") from None
                if (url.scheme not in ("http", "https") or not url.host
                        or url.username or url.password
                        or (url.scheme != "https" and not self.s.allow_http
                            and url.host not in _LOOPBACK)):
                    raise OIDCError(f"the provider returned an unsafe {name}")
            self._meta, self._meta_at = meta, time.time()
        return self._meta

    async def authorize_url(self, flow: dict) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self.s.client_id,
            "redirect_uri": self.s.redirect_uri,
            "scope": self.s.scopes,
            "state": flow["state"],
            "nonce": flow["nonce"],
            "code_challenge": _challenge(flow["verifier"]),
            "code_challenge_method": "S256",
        }
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def exchange(self, code: str, flow: dict) -> dict:
        """Trade the code for tokens; return validated claims (+ userinfo)."""
        meta = await self.metadata()
        try:
            r = await self.http.post(meta["token_endpoint"], data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.s.redirect_uri,
                "code_verifier": flow["verifier"],
            }, auth=(self.s.client_id, self.s.client_secret))
        except httpx.HTTPError as e:
            raise OIDCError(f"token exchange failed: {e.__class__.__name__}") from None
        if r.status_code != 200:
            raise OIDCError(f"the provider rejected the sign-in ({r.status_code})")
        tokens = r.json()
        if "id_token" not in tokens:
            raise OIDCError("the provider returned no id_token (is the openid scope enabled?)")
        claims = self._validate(_jwt_claims(tokens["id_token"]), meta, flow["nonce"])
        need = (self.s.username_claim, self.s.groups_claim)
        if any(k not in claims for k in need) and meta.get("userinfo_endpoint") and tokens.get("access_token"):
            try:
                u = await self.http.get(meta["userinfo_endpoint"],
                                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
                info = u.json() if u.status_code == 200 else {}
            except (httpx.HTTPError, ValueError):
                info = {}
            if isinstance(info, dict) and info.get("sub") == claims.get("sub"):
                claims = {**info, **claims}
        return claims

    def _validate(self, claims: dict, meta: dict, nonce: str) -> dict:
        now = time.time()
        if str(claims.get("iss", "")).rstrip("/") != str(meta["issuer"]).rstrip("/"):
            raise OIDCError("id_token issuer mismatch")
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if self.s.client_id not in auds:
            raise OIDCError("id_token audience mismatch")
        if len(auds) > 1 and claims.get("azp") != self.s.client_id:
            raise OIDCError("id_token authorized party mismatch")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] + LEEWAY < now:
            raise OIDCError("id_token expired")
        if claims.get("nonce") != nonce:
            raise OIDCError("id_token nonce mismatch")
        return claims

    def username(self, claims: dict) -> str:
        name = claims.get(self.s.username_claim)
        if not isinstance(name, str) or not name:
            raise OIDCError(f"the provider sent no {self.s.username_claim!r} claim")
        return name

    def role(self, claims: dict) -> str:
        groups = claims.get(self.s.groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        if self.s.admin_group and self.s.admin_group in groups:
            return "admin"
        if self.s.pusher_group and self.s.pusher_group in groups:
            return "pusher"
        if self.s.reader_group and self.s.reader_group not in groups:
            raise OIDCError("your account is not allowed to use this registry")
        return "reader"
