"""Admin endpoints for node-local upstreams and Helm repos (both roles,
guarded by node.secret, acting user must be an admin). See sources.py.

  POST   /internal/sources                {kind, name, ...spec, credentials?}
  PUT    /internal/sources/<kind>/<name>  {...spec, credentials?}
  DELETE /internal/sources/<kind>/<name>  also drops what was cached from it
  POST   /internal/sources/test           {kind, base_url, credentials?, name?}

``credentials`` is ``{"username", "password"}`` to set, ``null`` to clear,
or absent to keep what is stored (refused if base_url moves to another
host). Credentials are write-only: no endpoint returns them, and a test
only uses stored ones against their own origin. Config-file entries can't be changed here. The read side is
GET /internal/upstreams, which lists both kinds.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .accounts_api import _error, _json
from .auth import AccountError
from .sources import SourceError, probe, spec_from_request

if TYPE_CHECKING:
    from .api import App

_ABSENT = object()


def _origin(url: str) -> tuple:
    u = httpx.URL(url)
    return u.scheme, u.host, u.port


def _credentials(data: dict):
    """_ABSENT (keep), None (clear) or (username, password)."""
    if "credentials" not in data:
        return _ABSENT
    c = data["credentials"]
    if c is None:
        return None
    if not isinstance(c, dict):
        raise SourceError("credentials must be an object or null")
    username, password = c.get("username"), c.get("password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise SourceError("credentials need a username and a password")
    return username, password


class SourcesAPI:
    def __init__(self, app: App):
        self.app = app
        self.store = app.store
        self.config = app.config

    @property
    def registry(self):
        return self.app.sources

    def routes(self) -> list[Route]:
        r = [
            ("/internal/sources", self.create, ["POST"]),
            ("/internal/sources/test", self.test, ["POST"]),
            ("/internal/sources/{kind}/{name}", self.change, ["PUT", "DELETE"]),
        ]
        return [Route(path, self._guard(fn), methods=methods) for path, fn, methods in r]

    def _guard(self, handler):
        async def endpoint(request: Request) -> Response:
            if not self.app.check_internal(request):
                return _error(403, "bad secret")
            try:
                self.app.accounts._acting(request, "admin")
                return await handler(request)
            except (AccountError, SourceError) as e:
                return _error(e.status, str(e))
        return endpoint

    def _require_enabled(self) -> None:
        if not self.config.managed_sources:
            raise SourceError("adding sources in the UI is disabled by configuration "
                              "(OCIPROXY_MANAGED_SOURCES=false)", 403)

    def _item(self, kind: str, name: str) -> dict:
        return next(i for i in self.registry.describe(self.app.policy)
                    if i["kind"] == kind and i["name"] == name and i["managed"])

    async def _save(self, kind: str, name: str, data: dict, *, existing) -> None:
        spec = spec_from_request(kind, name, data)
        self.registry.check_conflicts(kind, name, spec)
        creds = _credentials(data)
        if creds is _ABSENT:
            row = self.store.get_source(kind, name) if existing else None
            sealed = row["credentials"] if row else None
            if sealed and _origin(row["spec"]["base_url"]) != _origin(spec["base_url"]):
                # Stored credentials never follow an entry to another host.
                raise SourceError("the host changed: re-enter the credentials (or remove them)")
        elif creds is None:
            sealed = None
        else:
            sealed = self.registry.box.seal(kind, name, *creds)
        if spec["sharing"] == "public" and sealed:
            raise SourceError("shared public sources cannot contain upstream credentials")
        old = self.store.get_source(kind, name)
        if old and old["spec"].get("sharing", "local") != spec["sharing"]:
            raise SourceError("delete and recreate a source to change sharing scope", 409)
        if old and old["spec"].get("owner"):
            spec["owner"] = old["spec"]["owner"]
        if spec["sharing"] == "public" and not self.config.is_primary:
            await self._publish(kind, name, spec)
            return
        if old and old["spec"].get("base_url") != spec["base_url"]:
            namespace = name if spec["sharing"] == "public" else f"private:{name}"
            await asyncio.to_thread(self.store.drop_namespace, namespace if kind == "oci" else "helm/"+namespace)
        await asyncio.to_thread(self.store.put_source, kind, name, spec, sealed)

    async def _publish(self, kind, name, spec, *, delete=False):
        try:
            await self.app.primary_client.publish(kind, name, spec, delete=delete)
            catalog = await self.app.primary_client.catalog()
        except httpx.HTTPStatusError as exc:
            raise SourceError("primary refused public catalog change", exc.response.status_code) from None
        except httpx.HTTPError:
            raise SourceError("primary unavailable; public catalog was not changed locally", 502) from None
        import json
        await asyncio.to_thread(self.store.apply_source_event, "config", json.dumps(catalog).encode())

    async def create(self, request: Request) -> Response:
        self._require_enabled()
        data = await _json(request)
        kind, name = data.get("kind"), data.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            raise SourceError("kind and name are required")
        if self.registry.get_managed(kind, name) is not None:
            raise SourceError(f"{kind} source {name!r} already exists", 409)
        await self._save(kind, name, data, existing=False)
        return JSONResponse(self._item(kind, name), status_code=201)

    async def change(self, request: Request) -> Response:
        kind, name = request.path_params["kind"], request.path_params["name"]
        current = self.registry.get_managed(kind, name)
        if current is None and not self.config.is_primary:
            entry = self.registry.resolve(kind, name)
            item = next((i for i in self.registry.describe(self.app.policy)
                         if i["kind"] == kind and i["name"] == name), None)
            if entry and entry.sharing == "public" and item and item.get("editable"):
                data = {"base_url": entry.base_url, "sharing": "public", "visibility": "public"}
                if kind == "oci":
                    data.update(prefix=entry.prefix, default=entry.default, official_namespace=entry.official_namespace)
                if request.method != "DELETE":
                    data = await _json(request)
                    data = spec_from_request(kind, name, data)
                    if data["sharing"] != "public":
                        raise SourceError("delete and recreate to make a source local", 409)
                await self._publish(kind, name, data, delete=request.method == "DELETE")
                return Response(status_code=204) if request.method == "DELETE" else JSONResponse(self._item(kind, name))
        if current is None:
            if self.registry.is_static(kind, name):
                raise SourceError(f"{name!r} is defined in the configuration file; "
                                  "change it there", 409)
            raise SourceError(f"no {kind} source {name!r}", 404)
        if request.method == "DELETE":
            # Always allowed, even with the feature off: cleaning up is safe.
            await asyncio.to_thread(self.store.delete_source, kind, name)
            return Response(status_code=204)
        self._require_enabled()
        data = await _json(request)
        if data.get("name", name) != name or data.get("kind", kind) != kind:
            raise SourceError("name and kind can't change; delete and re-add instead")
        await self._save(kind, name, data, existing=True)
        return JSONResponse(self._item(kind, name))

    async def test(self, request: Request) -> Response:
        data = await _json(request)
        kind, name = data.get("kind"), data.get("name")
        base_url = data.get("base_url")
        stored = self.registry.resolve(kind, name) if isinstance(name, str) else None
        if not isinstance(base_url, str) or not base_url:
            if stored is None:
                raise SourceError("base_url is required")
            base_url = stored.base_url
        spec = spec_from_request(kind, name if isinstance(name, str) and name else "test",
                                 {"base_url": base_url})
        creds = _credentials(data)
        if creds is _ABSENT:
            # Stored credentials are only ever sent to their own origin.
            same = stored is not None and _origin(stored.base_url) == _origin(spec["base_url"])
            creds = (stored.username, stored.password or "") if same and stored.username else None
        result = await probe(self.app.upstream_http, kind, spec["base_url"], creds,
                             proxied=bool(self.config.upstream_proxy))
        return JSONResponse(result)
