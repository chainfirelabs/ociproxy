"""Web UI: sign-in (local + OIDC), sessions, CSRF, roles, tokens, passthrough."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest
from containerproxy_ui.app import create_app
from containerproxy_ui.oidc import OIDCSettings

from containerproxy.auth import create_user

from .conftest import SECRET
from .fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeOIDC

CSRF = {"X-Requested-With": "containerproxy"}


def ui_client(proxy_url: str, secret: str = SECRET, **kw) -> httpx.AsyncClient:
    app = create_app(proxy_url=proxy_url, secret=secret, **kw)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ui", timeout=15
    )


async def login(c: httpx.AsyncClient, user: str = "ci", password: str = "cipass") -> httpx.Response:
    return await c.post("/api/auth/login", json={"username": user, "password": password}, headers=CSRF)


# -- pages and passthrough ------------------------------------------------------------

async def test_ui_serves_dashboard(primary):
    async with ui_client(primary.url) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "containerproxy" in r.text.lower()
        assert "Stored images" in r.text and "Sign in" in r.text
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"


async def test_ui_api_requires_sign_in(primary):
    async with ui_client(primary.url) as c:
        for path in ("/api/overview", "/api/images", "/api/upstreams", "/api/me", "/api/users"):
            assert (await c.get(path)).status_code == 401, path


async def test_ui_overview_passthrough(primary):
    async with ui_client(primary.url) as c:
        assert (await login(c)).status_code == 200
        r = await c.get("/api/overview")
        assert r.status_code == 200
        d = r.json()
        assert d["role"] == "primary"
        assert [u["prefix"] for u in d["upstreams"]] == ["fake"]
        assert "password" not in r.text


async def test_ui_images_passthrough_with_query(primary, fake_upstream):
    fake_upstream.install_image(repo="uishow", tag="t1")
    async with httpx.AsyncClient() as docker:
        assert (await docker.get(f"{primary.url}/v2/fake/uishow/manifests/t1")).status_code == 200
    async with ui_client(primary.url) as c:
        await login(c)
        r = await c.get("/api/images?limit=1&offset=0")
        assert r.status_code == 200
        assert r.json()["returned"] == 1


async def test_ui_upstreams_are_read_only(primary):
    async with ui_client(primary.url) as c:
        await login(c)
        r = await c.get("/api/upstreams")
        assert [(u["prefix"], u["visibility"]) for u in r.json()["items"]] == [("fake", "public")]
        r = await c.post("/api/upstreams", json={"name": "x"}, headers=CSRF)
        assert r.status_code == 405
        assert (await c.delete("/api/upstreams/fake", headers=CSRF)).status_code == 404


async def test_ui_wrong_secret_is_surfaced(primary):
    async with ui_client(primary.url, secret="wrong-secret") as c:
        assert (await login(c)).status_code == 403


async def test_ui_overview_shows_its_own_replica(primary, replica):
    create_user(replica.app_obj.store, "ci", "admin", "replica-password")
    async with ui_client(primary.url, node_url=replica.url) as c:
        assert (await login(c)).status_code == 401
        assert (await login(c, "ci", "replica-password")).status_code == 200
        d = (await c.get("/api/overview")).json()
        assert d["role"] == "replica"
        assert d["node_id"] == replica.app_obj.node_id
        assert "primary_node_id" not in d
        assert [u["prefix"] for u in d["upstreams"]] == ["fake"]
        assert d["replicas"] == []
        assert d["primary"]["url"] == primary.url
        # Status comes from this replica's public feed, without a primary admin call.
        assert d["primary"]["serial"] == d["primary_serial"]
        assert d["primary"]["registered"] == d["primary_connected"]


async def test_ui_local_node_down_does_not_fall_back_to_primary(primary):
    async with ui_client(primary.url, node_url="http://127.0.0.1:9") as c:
        assert (await login(c)).status_code == 502


async def test_ui_proxy_down_maps_to_502():
    async with ui_client("http://127.0.0.1:9") as c:  # nothing listening
        r = await login(c)
        assert r.status_code == 502
        assert "error" in r.json()


# -- local sign-in, sessions, CSRF ------------------------------------------------------

async def test_ui_local_login_and_logout(primary):
    create_user(primary.app_obj.store, "rita", "reader", "reader-password")
    async with ui_client(primary.url) as c:
        r = await login(c, "rita", "wrong-password")
        assert r.status_code == 401
        r = await login(c, "rita", "reader-password")
        assert r.json()["role"] == "reader"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        me = (await c.get("/api/me")).json()
        assert me["username"] == "rita" and me["tokens"] == []
        assert (await c.post("/api/auth/logout", headers=CSRF)).status_code == 200
        assert (await c.get("/api/me")).status_code == 401


async def test_ui_requires_csrf_header_on_state_changes(primary):
    async with ui_client(primary.url) as c:
        r = await c.post("/api/auth/login", json={"username": "ci", "password": "cipass"})
        assert r.status_code == 403  # login CSRF
        await login(c)
        r = await c.post("/api/users", json={"username": "x", "role": "reader", "password": "long-enough-1"})
        assert r.status_code == 403
        assert (await c.post("/api/auth/logout")).status_code == 403


async def test_ui_session_cookie_cannot_be_forged(primary):
    async with ui_client(primary.url) as c:
        await login(c)
        good = c.cookies["cp_session"]
        body, sig = good.rsplit(".", 1)
        c.cookies.clear()
        c.cookies.set("cp_session", body + "." + sig[::-1])
        assert (await c.get("/api/me")).status_code == 401
        # a valid signature over a different user: rejected as well
        c.cookies.clear()
        c.cookies.set("cp_session", "eyJ1IjoiYWRtaW4iLCJleHAiOjk5OTk5OTk5OTl9." + sig)
        assert (await c.get("/api/me")).status_code == 401
    # a cookie from a UI keyed with another secret is worthless here
    async with ui_client(primary.url, secret="other-secret") as other:
        other.cookies.set("cp_session", good)
        assert (await other.get("/api/me")).status_code == 401


async def test_ui_session_ends_when_user_is_disabled(primary):
    store = primary.app_obj.store
    create_user(store, "rita", "reader", "reader-password")
    async with ui_client(primary.url) as c:
        await login(c, "rita", "reader-password")
        assert (await c.get("/api/overview")).status_code == 200
        user = store.get_user("rita")
        user["disabled"] = True
        store.put_user(user)
        assert (await c.get("/api/overview")).status_code == 401


async def test_ui_token_login_is_refused(primary):
    """A leaked CLI token must not open a UI session."""
    store = primary.app_obj.store
    create_user(store, "rita", "reader", "reader-password")
    async with ui_client(primary.url) as c:
        await login(c, "rita", "reader-password")
        token = (await c.post("/api/me/tokens", json={"name": "cli"}, headers=CSRF)).json()["token"]
    async with ui_client(primary.url) as c:
        assert (await login(c, "rita", token)).status_code == 401


# -- tokens and admin -----------------------------------------------------------------

async def test_ui_token_created_in_ui_works_for_docker(tmp_path, fake_upstream, upstream_config):
    from .conftest import Node, make_config

    upstream_config.visibility = "private"
    upstream_config.sharing = "local"
    node = Node(make_config(tmp_path, "primary", name="uit", upstreams=[upstream_config])).start()
    try:
        fake_upstream.install_image()
        create_user(node.app_obj.store, "pete", "pusher", "pusher-password")
        async with ui_client(node.url) as c:
            await login(c, "pete", "pusher-password")
            r = await c.post("/api/me/tokens", json={"name": "laptop", "expires_days": 7}, headers=CSRF)
            assert r.status_code == 201
            token, token_id = r.json()["token"], r.json()["id"]
        async with httpx.AsyncClient() as docker:
            url = f"{node.url}/v2/fake/myimg/manifests/v1"
            assert (await docker.get(url)).status_code == 401
            assert (await docker.get(url, auth=("pete", token))).status_code == 200
            async with ui_client(node.url) as c:
                await login(c, "pete", "pusher-password")
                assert (await c.delete(f"/api/me/tokens/{token_id}", headers=CSRF)).status_code == 204
            assert (await docker.get(url, auth=("pete", token))).status_code == 401
    finally:
        node.stop()


async def test_ui_admin_pages_are_admin_only(primary):
    store = primary.app_obj.store
    create_user(store, "rita", "reader", "reader-password")
    async with ui_client(primary.url) as c:
        await login(c, "rita", "reader-password")
        assert (await c.get("/api/users")).status_code == 403
        r = await c.post("/api/users", json={"username": "x", "role": "admin", "password": "long-enough-1"},
                         headers=CSRF)
        assert r.status_code == 403
    async with ui_client(primary.url) as c:
        await login(c)  # bootstrap admin
        r = await c.post("/api/users", json={"username": "pete", "role": "pusher",
                                             "password": "pusher-password"}, headers=CSRF)
        assert r.status_code == 201
        r = await c.patch("/api/users/pete", json={"role": "admin"}, headers=CSRF)
        assert r.json()["role"] == "admin"
        names = [u["username"] for u in (await c.get("/api/users")).json()["items"]]
        assert names == ["pete", "rita"]
        assert (await c.delete("/api/users/rita", headers=CSRF)).status_code == 204
        # the bootstrap admin has no stored account, so no tokens
        r = await c.post("/api/me/tokens", json={"name": "x"}, headers=CSRF)
        assert r.status_code == 400


async def test_ui_change_password(primary):
    create_user(primary.app_obj.store, "rita", "reader", "reader-password")
    async with ui_client(primary.url) as c:
        await login(c, "rita", "reader-password")
        r = await c.post("/api/me/password", json={"current": "reader-password", "new": "brand-new-pass"},
                         headers=CSRF)
        assert r.status_code == 200
    async with ui_client(primary.url) as c:
        assert (await login(c, "rita", "reader-password")).status_code == 401
        assert (await login(c, "rita", "brand-new-pass")).status_code == 200


# -- OIDC ------------------------------------------------------------------------------

@pytest.fixture
def idp():
    p = FakeOIDC()
    p.start()
    yield p
    p.stop()


def oidc_settings(idp: FakeOIDC, **kw) -> OIDCSettings:
    return OIDCSettings(issuer=idp.issuer, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                        redirect_uri="http://ui/auth/oidc/callback", provider_name="Authentik",
                        admin_group="registry-admins", pusher_group="registry-pushers", **kw)


async def oidc_sign_in(c: httpx.AsyncClient) -> httpx.Response:
    """Drive the browser side of the flow; returns the callback response."""
    r = await c.get("/auth/oidc/start")
    assert r.status_code == 302, r.text
    async with httpx.AsyncClient() as browser:
        r = await browser.get(r.headers["location"])  # the provider "signs in" and redirects
    assert r.status_code in (302, 307)
    back = urlsplit(r.headers["location"])
    assert back.path == "/auth/oidc/callback"
    return await c.get(f"{back.path}?{back.query}")


async def test_oidc_sign_in_provisions_user_with_group_role(primary, idp):
    idp.user["groups"] = ["registry-users", "registry-pushers"]
    async with ui_client(primary.url, oidc=oidc_settings(idp)) as c:
        cfg = (await c.get("/api/auth/config")).json()
        assert cfg == {"oidc": True, "provider": "Authentik"}
        r = await oidc_sign_in(c)
        assert r.status_code == 302 and r.headers["location"] == "/"
        me = (await c.get("/api/me")).json()
        assert (me["username"], me["role"], me["source"]) == ("olga", "pusher", "oidc")
        # OIDC users get tokens for docker/helm
        r = await c.post("/api/me/tokens", json={"name": "cli"}, headers=CSRF)
        token = r.json()["token"]
    assert await primary.app_obj.auth.check("olga", token) is not None

    # role follows the provider's groups on the next sign-in
    idp.user["groups"] = ["registry-admins"]
    async with ui_client(primary.url, oidc=oidc_settings(idp)) as c:
        await oidc_sign_in(c)
        assert (await c.get("/api/me")).json()["role"] == "admin"


async def test_oidc_groups_from_userinfo(primary, idp):
    idp.user["groups"] = ["registry-admins"]
    idp.groups_in_userinfo_only = True
    async with ui_client(primary.url, oidc=oidc_settings(idp)) as c:
        await oidc_sign_in(c)
        assert (await c.get("/api/me")).json()["role"] == "admin"


async def test_oidc_reader_group_gate(primary, idp):
    idp.user["groups"] = ["someone-else"]
    async with ui_client(primary.url, oidc=oidc_settings(idp, reader_group="registry-users")) as c:
        r = await oidc_sign_in(c)
        assert r.status_code == 403 and "not allowed" in r.text
        assert (await c.get("/api/me")).status_code == 401
    assert primary.app_obj.store.get_user("olga") is None


@pytest.mark.parametrize("override, message", [
    ({"nonce": "replayed"}, "nonce"),
    ({"aud": "some-other-client"}, "audience"),
    ({"iss": "https://evil.example/"}, "issuer"),
    ({"exp": 1}, "expired"),
])
async def test_oidc_rejects_bad_id_tokens(primary, idp, override, message):
    idp.claim_overrides = override
    async with ui_client(primary.url, oidc=oidc_settings(idp)) as c:
        r = await oidc_sign_in(c)
        assert r.status_code == 403
        assert message in r.text
        assert (await c.get("/api/me")).status_code == 401


async def test_oidc_state_mismatch_and_missing_flow(primary, idp):
    async with ui_client(primary.url, oidc=oidc_settings(idp)) as c:
        r = await c.get("/auth/oidc/callback?code=x&state=y")  # no flow cookie
        assert r.status_code == 400 and "expired" in r.text
        await c.get("/auth/oidc/start")
        r = await c.get("/auth/oidc/callback?code=x&state=forged")
        assert r.status_code == 400 and "state" in r.text


async def test_oidc_never_takes_over_local_account(primary, idp):
    create_user(primary.app_obj.store, "olga", "admin", "olga-local-pass")
    async with ui_client(primary.url, oidc=oidc_settings(idp)) as c:
        r = await oidc_sign_in(c)
        assert r.status_code == 403 and "local user" in r.text
        assert (await c.get("/api/me")).status_code == 401


def test_oidc_requires_https_issuer():
    from containerproxy_ui.oidc import OIDCClient

    s = OIDCSettings(issuer="http://auth.example/application/o/x", client_id="c",
                     client_secret="s", redirect_uri="https://ui/auth/oidc/callback")
    with pytest.raises(ValueError, match="https"):
        OIDCClient(s, httpx.AsyncClient())
    s.allow_http = True
    OIDCClient(s, httpx.AsyncClient())


def test_oidc_env_config_is_validated(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example/application/o/registry/")
    with pytest.raises(ValueError, match="OIDC_CLIENT_ID"):
        OIDCSettings.from_env("https://ui.example")
    monkeypatch.setenv("OIDC_CLIENT_ID", "id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError, match="UI_PUBLIC_URL"):
        OIDCSettings.from_env(None)
    s = OIDCSettings.from_env("https://ui.example/")
    assert s.redirect_uri == "https://ui.example/auth/oidc/callback"
    assert s.issuer == "https://auth.example/application/o/registry"


# -- managed sources ------------------------------------------------------------------

async def test_ui_sources_admin_only_and_csrf(primary, fake_upstream):
    create_user(primary.app_obj.store, "reader1", "reader", "readerpass1")
    body = {"kind": "oci", "name": "viaui", "base_url": f"http://127.0.0.1:{fake_upstream.port}"}
    async with ui_client(primary.url) as c:
        assert (await login(c, "reader1", "readerpass1")).status_code == 200
        assert (await c.post("/api/sources", json=body, headers=CSRF)).status_code == 403
        # everyone signed in may read the list
        assert (await c.get("/api/upstreams")).status_code == 200
    async with ui_client(primary.url) as c:
        assert (await login(c)).status_code == 200
        assert (await c.post("/api/sources", json=body)).status_code == 403  # no CSRF header
        r = await c.post("/api/sources", json=body, headers=CSRF)
        assert r.status_code == 201, r.text
        r = await c.post("/api/sources/test", json={"kind": "oci", "name": "viaui"}, headers=CSRF)
        assert r.status_code == 200 and r.json()["ok"] is True
        r = await c.put("/api/sources/oci/viaui", headers=CSRF,
                        json={"base_url": body["base_url"], "prefix": "renamed"})
        assert r.status_code == 200 and r.json()["prefix"] == "renamed"
        items = (await c.get("/api/upstreams")).json()["items"]
        assert [(i["name"], i["managed"]) for i in items] == [("fake", False), ("viaui", True)]
        # config entries can't be changed through the UI
        r = await c.put("/api/sources/oci/fake", headers=CSRF, json={"base_url": "https://x.example"})
        assert r.status_code == 409
        assert (await c.delete("/api/sources/oci/viaui", headers=CSRF)).status_code == 204
        assert primary.app_obj.config.upstream_by_prefix("renamed") is None
