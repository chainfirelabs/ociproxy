"""Users, tokens, roles, per-repo visibility, account replication, the
account API, and push upload cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import httpx
import pytest

from ociproxy.auth import (
    AccessPolicy, create_token, create_user, hash_password, parse_token, verify_password,
)
from ociproxy.config import AccessRule, HelmRepoConfig, UpstreamConfig

from .conftest import SECRET, Node, free_port, make_config, wait_until
from .fake_upstream import MANIFEST_V2

INTERNAL = {"X-OCIProxy-Secret": SECRET}


def acting(user: str) -> dict:
    return {**INTERNAL, "X-OCIProxy-Acting-User": user}


async def _push(c: httpx.AsyncClient, base: str, repo: str, auth) -> int:
    """Push a one-blob image; returns the final status."""
    payload = f"cfg-{repo}".encode()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    r = await c.post(f"{base}/v2/{repo}/blobs/uploads/", auth=auth)
    if r.status_code != 202:
        return r.status_code
    r = await c.put(f"{base}{r.headers['location']}", params={"digest": digest},
                    content=payload, auth=auth)
    assert r.status_code == 201, r.text
    manifest = json.dumps({"schemaVersion": 2, "mediaType": MANIFEST_V2,
                           "config": {"mediaType": "x", "digest": digest, "size": len(payload)},
                           "layers": []}).encode()
    r = await c.put(f"{base}/v2/{repo}/manifests/v1", content=manifest,
                    headers={"content-type": MANIFEST_V2}, auth=auth)
    return r.status_code


# -- units ----------------------------------------------------------------------------

def test_password_hash_roundtrip():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse", h)
    assert not verify_password("wrong horse", h)
    assert not verify_password("x", "garbage")
    assert hash_password("same") != hash_password("same")  # salted


def test_token_format():
    assert parse_token("cpx_0123456789ab_secret") == ("0123456789ab", "secret")
    for bad in ("cpx_short_secret", "cpx_0123456789ab", "nope", "cpx_0123456789ab_"):
        assert parse_token(bad) is None


def test_visibility_precedence(tmp_path, fake_upstream):
    ups = [UpstreamConfig(name="ghcr", base_url="https://ghcr.io", prefix="ghcr", visibility="private"),
           UpstreamConfig(name="hub", base_url="https://hub", prefix="hub", visibility=None)]
    helm = [HelmRepoConfig(name="stable", base_url="https://c", visibility="private")]
    cfg = make_config(tmp_path, "primary", name="v", upstreams=ups, helm_repos=helm,
                      access_rules=[AccessRule("ghcr/public-org/*", "public"),
                                    AccessRule("internal/*", "private")])
    p = AccessPolicy(cfg)
    assert p.visibility("ghcr/org/app") == "private"          # upstream setting
    assert p.visibility("ghcr/public-org/app") == "public"    # rule beats upstream
    assert p.visibility("hub/library/nginx") == "public"      # inherits default
    assert p.visibility("internal/team/app") == "private"     # rule on pushed repos
    assert p.visibility("team/app") == "public"               # default
    assert p.visibility("helm/stable") == "private"
    assert p.anything_private

    # an unprefixed path may be served by the default upstream: stricter wins
    ups[0].default = True
    assert p.visibility("nginx") == "private"


# -- registry access -------------------------------------------------------------------

@pytest.fixture
def private_cluster(tmp_path, fake_upstream, upstream_config):
    """Primary + replica, 'fake' upstream private, pushed repos public."""
    upstream_config.visibility = "private"
    upstream_config.sharing = "local"
    primary = Node(make_config(tmp_path, "primary", name="ap", upstreams=[upstream_config],
                               push_username="boot", push_password="boot-password")).start()
    port = free_port()
    replica = Node(make_config(tmp_path, "replica", name="ar", upstreams=[upstream_config],
                               primary_url=primary.url, port=port,
                               advertised_url=f"http://127.0.0.1:{port}",
                               push_username="boot", push_password="boot-password")).start()
    yield primary, replica
    replica.stop()
    primary.stop()


async def test_private_upstream_public_local(private_cluster, fake_upstream):
    primary, replica = private_cluster
    fake_upstream.install_image()
    store = primary.app_obj.store
    create_user(store, "rita", "reader", "reader-password")
    async with httpx.AsyncClient(timeout=30) as c:
        for node in (primary,):
            r = await c.get(f"{node.url}/v2/fake/myimg/manifests/v1")
            assert r.status_code == 401
            assert r.headers["www-authenticate"].startswith("Basic")
            r = await c.get(f"{node.url}/v2/fake/myimg/manifests/v1", auth=("rita", "reader-password"))
            assert r.status_code == 200
            r = await c.get(f"{node.url}/v2/fake/myimg/manifests/v1", auth=("rita", "wrong-password"))
            assert r.status_code == 401
        # pushed repos stay public: anonymous pulls work
        assert await _push(c, primary.url, "team/app", ("boot", "boot-password")) == 201
        assert (await c.get(f"{primary.url}/v2/team/app/manifests/v1")).status_code == 200


async def test_roles_gate_push(private_cluster):
    primary, _ = private_cluster
    store = primary.app_obj.store
    create_user(store, "rita", "reader", "reader-password")
    create_user(store, "pete", "pusher", "pusher-password")
    async with httpx.AsyncClient(timeout=30) as c:
        assert await _push(c, primary.url, "a/app", None) == 401
        assert await _push(c, primary.url, "a/app", ("rita", "reader-password")) == 403
        assert await _push(c, primary.url, "a/app", ("pete", "pusher-password")) == 201


async def test_tokens_authenticate_and_revoke_immediately(private_cluster, fake_upstream):
    primary, _ = private_cluster
    fake_upstream.install_image()
    store = primary.app_obj.store
    create_user(store, "pete", "pusher", "pusher-password")
    full, row = create_token(store, "pete", "ci")
    url = f"{primary.url}/v2/fake/myimg/manifests/v1"
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(url, auth=("pete", full))).status_code == 200
        assert await _push(c, primary.url, "t/app", ("pete", full)) == 201
        # the token is bound to its user
        create_user(store, "other", "admin", "other-password")
        assert (await c.get(url, auth=("other", full))).status_code == 401
        # revoke: the cached success is dropped at once
        store.delete_token(row["id"])
        assert (await c.get(url, auth=("pete", full))).status_code == 401
    assert store.get_token(row["id"]) is None


async def test_expired_token_and_disabled_user(private_cluster, fake_upstream):
    primary, _ = private_cluster
    fake_upstream.install_image()
    store = primary.app_obj.store
    create_user(store, "pete", "pusher", "pusher-password")
    full, row = create_token(store, "pete", "ci", expires_days=1)
    url = f"{primary.url}/v2/fake/myimg/manifests/v1"
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(url, auth=("pete", full))).status_code == 200
        row["expires_at"] = time.time() - 1
        store.put_token(row)
        assert (await c.get(url, auth=("pete", full))).status_code == 401
        user = store.get_user("pete")
        user["disabled"] = True
        store.put_user(user)
        assert (await c.get(url, auth=("pete", "pusher-password"))).status_code == 401


async def test_accounts_stay_local_and_replica_has_its_own_accounts(private_cluster, fake_upstream):
    primary, replica = private_cluster
    fake_upstream.install_image()
    store, rstore = primary.app_obj.store, replica.app_obj.store
    create_user(store, "rita", "reader", "reader-password")
    full, _ = create_token(store, "rita", "laptop")
    assert rstore.get_user("rita") is None
    assert not rstore.list_tokens("rita")
    assert not any(e.kind in ("user", "token") for e in store.changelog_since(0))
    create_user(rstore, "rita", "reader", "replica-password")
    url = f"{replica.url}/v2/fake/myimg/manifests/v1"
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(url, auth=("rita", "reader-password"))).status_code == 401
        assert (await c.get(url, auth=("rita", full))).status_code == 401
        assert (await c.get(url, auth=("rita", "replica-password"))).status_code == 200
        store.delete_user("rita")
        assert rstore.get_user("rita") is not None
        assert (await c.get(url, auth=("rita", "replica-password"))).status_code == 200


async def test_late_replica_does_not_replay_accounts(tmp_path, private_cluster):
    primary, _ = private_cluster
    create_user(primary.app_obj.store, "rita", "admin", "reader-password")
    primary.app_obj.store.compact_changelog()
    late = Node(make_config(tmp_path, "replica", name="late", primary_url=primary.url)).start()
    try:
        await wait_until(lambda: late.app_obj.tail.serial >= primary.app_obj.store.current_serial())
        assert late.app_obj.store.get_user("rita") is None
    finally:
        late.stop()


async def test_bootstrap_admin_still_works(private_cluster, fake_upstream):
    primary, replica = private_cluster
    fake_upstream.install_image()
    async with httpx.AsyncClient(timeout=30) as c:
        for node in (primary, replica):
            r = await c.get(f"{node.url}/v2/fake/myimg/manifests/v1", auth=("boot", "boot-password"))
            assert r.status_code == 200


async def test_catalog_and_index_do_not_leak(private_cluster):
    primary, _ = private_cluster
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(f"{primary.url}/v2/_catalog")).status_code == 401
        assert (await c.get(f"{primary.url}/v2/_catalog", auth=("boot", "boot-password"))).status_code == 200
        r = await c.get(f"{primary.url}/")
        assert r.text == "ociproxy primary\n"


async def test_catalog_open_when_nothing_is_private(primary):
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(f"{primary.url}/v2/_catalog")).status_code == 200


async def test_private_helm_repo(tmp_path, fake_upstream):
    helm = HelmRepoConfig(name="stable", base_url=f"http://127.0.0.1:{fake_upstream.port}/charts",
                          visibility="private")
    node = Node(make_config(tmp_path, "primary", name="hp", helm_repos=[helm])).start()
    try:
        fake_upstream.add_helm_chart("app", "1.0.0")
        create_user(node.app_obj.store, "rita", "reader", "reader-password")
        async with httpx.AsyncClient(timeout=30) as c:
            assert (await c.get(f"{node.url}/helm/stable/index.yaml")).status_code == 401
            r = await c.get(f"{node.url}/helm/stable/index.yaml", auth=("rita", "reader-password"))
            assert r.status_code == 200
    finally:
        node.stop()


# -- account API ------------------------------------------------------------------------

async def test_account_api_login(private_cluster):
    primary, _ = private_cluster
    store = primary.app_obj.store
    create_user(store, "alice", "admin", "alice-password")
    full, _ = create_token(store, "alice", "cli")
    async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
        r = await c.post("/internal/auth/login", json={"username": "alice", "password": "alice-password"},
                         headers=INTERNAL)
        assert r.json() == {"username": "alice", "role": "admin", "source": "local"}
        r = await c.post("/internal/auth/login", json={"username": "alice", "password": "nope"},
                         headers=INTERNAL)
        assert r.status_code == 401
        # a CLI token must not open a UI session
        r = await c.post("/internal/auth/login", json={"username": "alice", "password": full},
                         headers=INTERNAL)
        assert r.status_code == 401
        r = await c.post("/internal/auth/login", json={"username": "boot", "password": "boot-password"},
                         headers=INTERNAL)
        assert r.json()["source"] == "config"
        # the node secret is required
        r = await c.post("/internal/auth/login", json={"username": "alice", "password": "alice-password"})
        assert r.status_code == 403


async def test_account_api_roles_are_enforced_by_primary(private_cluster):
    primary, replica = private_cluster
    store = primary.app_obj.store
    create_user(store, "alice", "admin", "alice-password")
    create_user(store, "rita", "reader", "reader-password")
    async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
        assert (await c.get("/internal/users", headers=acting("rita"))).status_code == 403
        assert (await c.get("/internal/users", headers=acting("ghost"))).status_code == 403
        assert (await c.get("/internal/users", headers=INTERNAL)).status_code == 400

        r = await c.post("/internal/users", headers=acting("alice"),
                         json={"username": "pete", "role": "pusher", "password": "pusher-password"})
        assert r.status_code == 201
        assert "password_hash" not in r.text
        r = await c.post("/internal/users", headers=acting("alice"),
                         json={"username": "pete", "role": "pusher", "password": "pusher-password"})
        assert r.status_code == 409
        r = await c.post("/internal/users", headers=acting("alice"),
                         json={"username": "boot", "role": "reader", "password": "whatever-123"})
        assert r.status_code == 409  # reserved for the bootstrap admin
        r = await c.patch("/internal/users/pete", headers=acting("alice"), json={"role": "admin"})
        assert r.json()["role"] == "admin"
        # no self-lockout
        r = await c.patch("/internal/users/alice", headers=acting("alice"), json={"disabled": True})
        assert r.status_code == 400
        assert (await c.delete("/internal/users/alice", headers=acting("alice"))).status_code == 400

        users = (await c.get("/internal/users", headers=acting("alice"))).json()["items"]
        assert [u["username"] for u in users] == ["alice", "pete", "rita"]
        assert (await c.delete("/internal/users/rita", headers=acting("alice"))).status_code == 204
    async with httpx.AsyncClient(base_url=replica.url, timeout=30) as c:
        assert (await c.get("/internal/users", headers=acting("alice"))).status_code == 403
        create_user(replica.app_obj.store, "alice", "admin", "replica-password")
        assert (await c.get("/internal/users", headers=acting("alice"))).status_code == 200


async def test_account_api_tokens_and_password(private_cluster):
    primary, _ = private_cluster
    store = primary.app_obj.store
    create_user(store, "pete", "pusher", "pusher-password")
    create_user(store, "rita", "reader", "reader-password")
    async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
        r = await c.post("/internal/me/tokens", headers=acting("pete"),
                         json={"name": "laptop", "expires_days": 30})
        assert r.status_code == 201
        token, token_id = r.json()["token"], r.json()["id"]
        assert token.startswith("cpx_")
        me = (await c.get("/internal/me", headers=acting("pete"))).json()
        assert [t["name"] for t in me["tokens"]] == ["laptop"]
        assert "token" not in me["tokens"][0] and "secret_hash" not in json.dumps(me)
        # someone else can't revoke it
        assert (await c.delete(f"/internal/me/tokens/{token_id}", headers=acting("rita"))).status_code == 404
        assert (await c.delete(f"/internal/me/tokens/{token_id}", headers=acting("pete"))).status_code == 204

        r = await c.post("/internal/me/password", headers=acting("pete"),
                         json={"current": "wrong-one", "new": "new-password-1"})
        assert r.status_code == 403
        r = await c.post("/internal/me/password", headers=acting("pete"),
                         json={"current": "pusher-password", "new": "short"})
        assert r.status_code == 400
        r = await c.post("/internal/me/password", headers=acting("pete"),
                         json={"current": "pusher-password", "new": "new-password-1"})
        assert r.status_code == 200
    assert await primary.app_obj.auth.check("pete", "new-password-1") is not None


async def test_account_api_oidc_provisioning(private_cluster):
    primary, _ = private_cluster
    store = primary.app_obj.store
    create_user(store, "alice", "admin", "alice-password")
    async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
        r = await c.post("/internal/auth/oidc", headers=INTERNAL, json={"username": "olga", "role": "reader"})
        assert r.json()["source"] == "oidc"
        r = await c.post("/internal/auth/oidc", headers=INTERNAL, json={"username": "olga", "role": "pusher"})
        assert r.json()["role"] == "pusher"  # re-synced from groups on every sign-in
        # never takes over a local account
        r = await c.post("/internal/auth/oidc", headers=INTERNAL, json={"username": "alice", "role": "admin"})
        assert r.status_code == 409
        # OIDC users have no password: only tokens work for the CLI
        r = await c.post("/internal/auth/login", headers=INTERNAL, json={"username": "olga", "password": ""})
        assert r.status_code == 401
        r = await c.patch("/internal/users/olga", headers=acting("alice"), json={"disabled": True})
        assert r.status_code == 200
        r = await c.post("/internal/auth/oidc", headers=INTERNAL, json={"username": "olga", "role": "reader"})
        assert r.status_code == 403


# -- push upload cleanup --------------------------------------------------------------------

async def test_idle_uploads_are_dropped(tmp_path):
    node = Node(make_config(tmp_path, "primary", name="up", push_username="ci",
                            push_password="cipass", upload_idle_timeout=1)).start()
    try:
        tmp_dir = tmp_path / "storage-up" / "tmp"
        async with httpx.AsyncClient(base_url=node.url, timeout=30) as c:
            for _ in range(5):
                r = await c.post("/v2/app/blobs/uploads/", auth=("ci", "cipass"))
                assert r.status_code == 202
            location = r.headers["location"]
            assert len(node.app_obj.uploads) == 5
            assert len(list(tmp_dir.iterdir())) == 5
            # the background sweeper (every timeout/4) drops them
            await wait_until(lambda: not node.app_obj.uploads, timeout=5, msg="sessions swept")
            assert not list(tmp_dir.iterdir())
            assert node.app_obj.metrics.value("ociproxy_uploads_expired_total") == 5
            r = await c.patch(location, content=b"late", auth=("ci", "cipass"))
            assert r.status_code == 404
    finally:
        node.stop()


async def test_upload_in_progress_is_not_dropped(tmp_path):
    node = Node(make_config(tmp_path, "primary", name="up2", push_username="ci",
                            push_password="cipass", upload_idle_timeout=1)).start()
    try:
        async with httpx.AsyncClient(base_url=node.url, timeout=30) as c:
            r = await c.post("/v2/app/blobs/uploads/", auth=("ci", "cipass"))
            location = r.headers["location"]

            async def slow_body():
                for _ in range(4):
                    yield b"x" * 1024
                    await asyncio.sleep(0.5)  # 2 s total, twice the idle timeout

            async def sweep_during():
                await asyncio.sleep(1.5)
                return node.app_obj.sweep_uploads()

            patch, swept = await asyncio.gather(
                c.patch(location, content=slow_body(), auth=("ci", "cipass")), sweep_during())
            assert swept == 0
            assert patch.status_code == 202
            payload = b"x" * 4096
            r = await c.put(location, params={"digest": "sha256:" + hashlib.sha256(payload).hexdigest()},
                            auth=("ci", "cipass"))
            assert r.status_code == 201
    finally:
        node.stop()


async def test_upload_session_cap_and_ownership(tmp_path):
    node = Node(make_config(tmp_path, "primary", name="up3", push_username="ci",
                            push_password="cipass", upload_max_sessions=2)).start()
    try:
        create_user(node.app_obj.store, "pete", "pusher", "pusher-password")
        async with httpx.AsyncClient(base_url=node.url, timeout=30) as c:
            r = await c.post("/v2/app/blobs/uploads/", auth=("ci", "cipass"))
            mine = r.headers["location"]
            assert (await c.post("/v2/app/blobs/uploads/", auth=("ci", "cipass"))).status_code == 202
            r = await c.post("/v2/app/blobs/uploads/", auth=("ci", "cipass"))
            assert r.status_code == 429
            assert r.headers["retry-after"] == "60"
            # another pusher can't continue or cancel someone else's upload
            assert (await c.patch(mine, content=b"x", auth=("pete", "pusher-password"))).status_code == 404
            assert (await c.delete(mine, auth=("pete", "pusher-password"))).status_code == 404
            assert (await c.delete(mine, auth=("ci", "cipass"))).status_code == 204
            assert (await c.post("/v2/app/blobs/uploads/", auth=("pete", "pusher-password"))).status_code == 202
    finally:
        node.stop()
