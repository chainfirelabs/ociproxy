"""Account endpoints for this node's UI, guarded by its local node.secret.

The UI signs users in and then calls these on their behalf, naming them in
``X-OCIProxy-Acting-User``. This node looks that user up and
enforces roles itself, so the UI holds no authority of its own beyond the
node secret.

  POST   /internal/auth/login            {username, password} -> user (passwords only)
  POST   /internal/auth/oidc             {username, role}     -> user (provision/sync)
  GET    /internal/me                    acting user + their tokens
  POST   /internal/me/password           {current, new}
  POST   /internal/me/tokens             {name, expires_days?} -> {token, ...} (shown once)
  DELETE /internal/me/tokens/<id>
  GET    /internal/users                 (admin) users + token counts
  POST   /internal/users                 (admin) {username, role, password}
  PATCH  /internal/users/<name>          (admin) {role?, disabled?, password?}
  DELETE /internal/users/<name>          (admin)
  GET    /internal/users/<name>/tokens   (admin)
  DELETE /internal/tokens/<id>           (admin)
"""

from __future__ import annotations

import asyncio
import hmac
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .auth import (
    AccountError, Principal, create_token, create_user, public_token, public_user,
    role_at_least, update_user, upsert_oidc_user, verify_password, validate_password,
)

if TYPE_CHECKING:
    from .api import App

ACTING_USER_HEADER = "x-ociproxy-acting-user"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise AccountError("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise AccountError("body must be a JSON object")
    return data


class AccountsAPI:
    def __init__(self, app: App):
        self.app = app
        self.store = app.store
        self.config = app.config

    def routes(self) -> list[Route]:
        r = [
            ("/internal/auth/login", self.login, ["POST"]),
            ("/internal/auth/oidc", self.oidc, ["POST"]),
            ("/internal/me", self.me, ["GET"]),
            ("/internal/me/password", self.change_password, ["POST"]),
            ("/internal/me/tokens", self.create_my_token, ["POST"]),
            ("/internal/me/tokens/{token_id}", self.revoke_my_token, ["DELETE"]),
            ("/internal/users", self.users, ["GET", "POST"]),
            ("/internal/users/{username}", self.user, ["PATCH", "DELETE"]),
            ("/internal/users/{username}/tokens", self.user_tokens, ["GET"]),
            ("/internal/tokens/{token_id}", self.revoke_token, ["DELETE"]),
        ]
        return [Route(path, self._guard(fn), methods=methods) for path, fn, methods in r]

    def _guard(self, handler):
        async def endpoint(request: Request) -> Response:
            if not self.app.check_internal(request):
                return _error(403, "bad secret")
            try:
                return await handler(request)
            except AccountError as e:
                return _error(e.status, str(e))
        return endpoint

    @property
    def _bootstrap(self) -> str | None:
        return self.config.push_username if self.app.auth.has_bootstrap else None

    def _acting(self, request: Request, minimum: str = "reader") -> Principal:
        name = request.headers.get(ACTING_USER_HEADER, "")
        if not name:
            raise AccountError("no acting user", 400)
        boot = self._bootstrap
        if boot and hmac.compare_digest(name.encode(), boot.encode()):
            principal = Principal(name, "admin", "config")
        else:
            user = self.store.get_user(name)
            if user is None or user["disabled"]:
                raise AccountError("acting user does not exist or is disabled", 403)
            principal = Principal(name, user["role"], user["source"])
        if not role_at_least(principal.role, minimum):
            raise AccountError(f"requires the {minimum} role", 403)
        return principal

    # -- sign-in ---------------------------------------------------------------

    async def login(self, request: Request) -> Response:
        data = await _json(request)
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise AccountError("username and password are required")
        # Passwords only: a leaked CLI token must not open a UI session.
        principal = await self.app.auth.check(username, password, allow_tokens=False)
        if principal is None:
            return _error(401, "invalid username or password")
        return JSONResponse({"username": principal.username, "role": principal.role,
                             "source": principal.source})

    async def oidc(self, request: Request) -> Response:
        data = await _json(request)
        user = await asyncio.to_thread(upsert_oidc_user, self.store, data.get("username"),
                                       data.get("role"), reserved=self._bootstrap)
        return JSONResponse(public_user(user))

    # -- the acting user ---------------------------------------------------------

    async def me(self, request: Request) -> Response:
        principal = self._acting(request)
        if principal.source == "config":
            return JSONResponse({"username": principal.username, "role": "admin",
                                 "source": "config", "tokens": []})
        user = self.store.get_user(principal.username)
        return JSONResponse({**public_user(user), "tokens": [
            public_token(t) for t in self.store.list_tokens(principal.username)]})

    async def change_password(self, request: Request) -> Response:
        principal = self._acting(request)
        if principal.source != "local":
            raise AccountError("only local users have a password here")
        data = await _json(request)
        current, new = data.get("current"), data.get("new")
        user = self.store.get_user(principal.username)
        if not isinstance(current, str) or not await asyncio.to_thread(
                verify_password, current, user["password_hash"] or ""):
            raise AccountError("current password is wrong", 403)
        validate_password(new)
        await asyncio.to_thread(update_user, self.store, principal.username, password=new)
        return JSONResponse({"ok": True})

    async def create_my_token(self, request: Request) -> Response:
        principal = self._acting(request)
        if principal.source == "config":
            raise AccountError("the bootstrap admin can't hold tokens; create a user for that")
        data = await _json(request)
        full, row = await asyncio.to_thread(create_token, self.store, principal.username,
                                            data.get("name"), data.get("expires_days"))
        return JSONResponse({**public_token(row), "token": full}, status_code=201)

    async def revoke_my_token(self, request: Request) -> Response:
        principal = self._acting(request)
        token = self.store.get_token(request.path_params["token_id"])
        if token is None or token["username"] != principal.username:
            raise AccountError("no such token", 404)
        await asyncio.to_thread(self.store.delete_token, token["id"])
        return Response(status_code=204)

    # -- admin -------------------------------------------------------------------

    async def users(self, request: Request) -> Response:
        self._acting(request, "admin")
        if request.method == "GET":
            counts: dict[str, int] = {}
            for t in self.store.list_tokens():
                counts[t["username"]] = counts.get(t["username"], 0) + 1
            return JSONResponse({"items": [
                {**public_user(u), "tokens": counts.get(u["username"], 0)}
                for u in self.store.list_users()]})
        data = await _json(request)
        user = await asyncio.to_thread(
            create_user, self.store, data.get("username"), data.get("role"),
            data.get("password"), reserved=self._bootstrap,
        )
        return JSONResponse(public_user(user), status_code=201)

    async def user(self, request: Request) -> Response:
        principal = self._acting(request, "admin")
        username = request.path_params["username"]
        if request.method == "DELETE":
            if username == principal.username:
                raise AccountError("you can't delete your own account")
            if not await asyncio.to_thread(self.store.delete_user, username):
                raise AccountError(f"no user {username!r}", 404)
            return Response(status_code=204)
        data = await _json(request)
        role, disabled, password = data.get("role"), data.get("disabled"), data.get("password")
        if username == principal.username and (disabled or (role and role != "admin")):
            raise AccountError("you can't disable or demote your own account")
        if disabled is not None and not isinstance(disabled, bool):
            raise AccountError("disabled must be true or false")
        user = await asyncio.to_thread(update_user, self.store, username, role=role,
                                       disabled=disabled, password=password)
        return JSONResponse(public_user(user))

    async def user_tokens(self, request: Request) -> Response:
        self._acting(request, "admin")
        username = request.path_params["username"]
        if self.store.get_user(username) is None:
            raise AccountError(f"no user {username!r}", 404)
        return JSONResponse({"items": [public_token(t) for t in self.store.list_tokens(username)]})

    async def revoke_token(self, request: Request) -> Response:
        self._acting(request, "admin")
        if not await asyncio.to_thread(self.store.delete_token, request.path_params["token_id"]):
            raise AccountError("no such token", 404)
        return Response(status_code=204)
