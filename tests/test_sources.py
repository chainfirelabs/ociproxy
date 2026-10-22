"""Upstreams and Helm repos managed in the web UI (/internal/sources): pull
through them, replication, credential handling, conflicts, deletion."""

from __future__ import annotations

import json

import httpx
import pytest

from ociproxy.sources import CredentialBox

from .conftest import SECRET, Node, free_port, make_config, make_replica, wait_until

ADMIN = {"X-OCIProxy-Secret": SECRET, "X-OCIProxy-Acting-User": "ci"}


def _fake_url(fake_upstream, path: str = "") -> str:
    return f"http://127.0.0.1:{fake_upstream.port}{path}"


async def _create(c: httpx.AsyncClient, node, **body) -> httpx.Response:
    # Public replication scenarios explicitly opt in; credentialed/private
    # scenarios exercise local sources. Production defaults remain private/local.
    body.setdefault("sharing", "local" if body.get("credentials") or body.get("visibility") == "private" else "public")
    body.setdefault("visibility", "private" if body["sharing"] == "local" else "public")
    return await c.post(f"{node.url}/internal/sources", headers=ADMIN, json=body)


async def _listing(c: httpx.AsyncClient, node) -> list[dict]:
    r = await c.get(f"{node.url}/internal/upstreams", headers=ADMIN)
    assert r.status_code == 200
    return r.json()["items"]


# -- OCI upstreams -------------------------------------------------------------------

async def test_managed_upstream_pulls_on_primary_and_replica(primary, replica, fake_upstream):
    img = fake_upstream.install_image(repo="team/app", tag="v1")
    async with httpx.AsyncClient() as c:
        # Unknown prefix before it exists.
        assert (await c.get(f"{replica.url}/v2/mirror/team/app/manifests/v1")).status_code == 404
        r = await _create(c, primary, kind="oci", name="mirror", base_url=_fake_url(fake_upstream))
        assert r.status_code == 201, r.text
        item = r.json()
        assert item["managed"] is True and item["active"] is True and item["prefix"] == "mirror"

        r = await c.get(f"{primary.url}/v2/mirror/team/app/manifests/v1")
        assert r.status_code == 200
        assert r.headers["docker-content-digest"] == img["manifest"]

        # The replica learns the source from the changelog, then pulls via the primary.
        await wait_until(lambda: replica.app_obj.config.upstream_by_prefix("mirror") is not None,
                         msg="replica to learn the managed upstream")
        r = await c.get(f"{replica.url}/v2/mirror/team/app/manifests/v1")
        assert r.status_code == 200
        assert r.headers["docker-content-digest"] == img["manifest"]
        blob = await c.get(f"{replica.url}/v2/mirror/team/app/blobs/{img['config']}")
        assert blob.status_code == 200

        names = [(i["name"], i["managed"]) for i in await _listing(c, primary)]
        assert names == [("fake", False), ("mirror", True)]


async def test_edit_changes_prefix_and_keeps_name(primary, fake_upstream):
    fake_upstream.install_image(repo="app", tag="v1")
    async with httpx.AsyncClient() as c:
        assert (await _create(c, primary, kind="oci", name="m", prefix="old", sharing="local", visibility="public",
                              base_url=_fake_url(fake_upstream))).status_code == 201
        r = await c.put(f"{primary.url}/internal/sources/oci/m", headers=ADMIN,
                        json={"prefix": "new", "base_url": _fake_url(fake_upstream),
                              "visibility": "private"})
        assert r.status_code == 200, r.text
        assert r.json()["prefix"] == "new" and r.json()["own_visibility"] == "private"
        assert (await c.get(f"{primary.url}/v2/old/app/manifests/v1")).status_code == 404
        # private now: anonymous is refused, the admin gets through
        assert (await c.get(f"{primary.url}/v2/new/app/manifests/v1")).status_code == 401
        r = await c.get(f"{primary.url}/v2/new/app/manifests/v1", auth=("ci", "cipass"))
        assert r.status_code == 200
        r = await c.put(f"{primary.url}/internal/sources/oci/m", headers=ADMIN,
                        json={"name": "renamed", "base_url": _fake_url(fake_upstream)})
        assert r.status_code == 400


async def test_delete_drops_cached_content_everywhere(primary, replica, fake_upstream):
    fake_upstream.install_image(repo="gone", tag="v1")
    async with httpx.AsyncClient() as c:
        assert (await _create(c, primary, kind="oci", name="tmp",
                              base_url=_fake_url(fake_upstream))).status_code == 201
        await wait_until(lambda: replica.app_obj.config.upstream_by_prefix("tmp") is not None,
                         msg="replica to learn the upstream")
        assert (await c.get(f"{replica.url}/v2/tmp/gone/manifests/v1")).status_code == 200
        await wait_until(lambda: replica.app_obj.store.get_manifest("tmp", "gone", "v1"),
                         msg="manifest on the replica")

        r = await c.delete(f"{primary.url}/internal/sources/oci/tmp", headers=ADMIN)
        assert r.status_code == 204
        assert primary.app_obj.store.get_manifest("tmp", "gone", "v1") is None
        await wait_until(lambda: replica.app_obj.config.upstream_by_prefix("tmp") is None,
                         msg="replica to drop the upstream")
        assert replica.app_obj.store.get_manifest("tmp", "gone", "v1") is None
        assert (await c.get(f"{replica.url}/v2/tmp/gone/manifests/v1")).status_code == 404
        assert "tmp" not in replica.app_obj.clients
        assert (await c.delete(f"{primary.url}/internal/sources/oci/tmp",
                               headers=ADMIN)).status_code == 404


# -- validation and conflicts -------------------------------------------------------------

@pytest.mark.parametrize("body, status", [
    ({"name": "fake"}, 409),                       # name of a config upstream
    ({"name": "x", "prefix": "fake"}, 409),       # prefix of a config upstream
    ({"name": "local"}, 400),                      # reserved
    ({"name": "x", "prefix": "helm"}, 400),        # reserved
    ({"name": "bad/name"}, 400),
    ({"name": "x", "base_url": "ftp://h"}, 400),
    ({"name": "x", "base_url": "https://u:p@h"}, 400),  # creds belong in credentials
    ({"name": "x", "visibility": "secret"}, 400),
    ({"name": "x", "credentials": {"username": "u"}}, 400),
])
async def test_create_is_validated(primary, body, status):
    body = {"kind": "oci", "base_url": "https://registry.example", **body}
    async with httpx.AsyncClient() as c:
        r = await _create(c, primary, **body)
        assert r.status_code == status, r.text
        assert "error" in r.json()
        assert [u.name for u in primary.app_obj.config.upstreams] == ["fake"]


async def test_conflicts_between_managed_entries_and_defaults(primary):
    async with httpx.AsyncClient() as c:
        assert (await _create(c, primary, kind="oci", name="a", base_url="https://a.example",
                              default=True)).status_code == 201
        assert primary.app_obj.config.default_upstream().name == "a"
        r = await _create(c, primary, kind="oci", name="b", prefix="a", base_url="https://b.example")
        assert r.status_code == 409
        r = await _create(c, primary, kind="oci", name="b", base_url="https://b.example", default=True)
        assert r.status_code == 409 and "default" in r.json()["error"]
        assert (await _create(c, primary, kind="oci", name="a",
                              base_url="https://a.example")).status_code == 409  # exists


async def test_prefix_over_pushed_images_is_refused(primary):
    store = primary.app_obj.store
    store.put_manifest("local", "myteam/app", "v1", "sha256:" + "0" * 64,
                       "application/vnd.oci.image.manifest.v1+json", b"{}")
    async with httpx.AsyncClient() as c:
        r = await _create(c, primary, kind="oci", name="myteam", base_url="https://r.example")
        assert r.status_code == 409 and "pushed images" in r.json()["error"]
        # "myteam_x" must not match through LIKE-style wildcards
        assert (await _create(c, primary, kind="oci", name="myteam_x",
                              base_url="https://r.example")).status_code == 201


async def test_config_entries_are_read_only(primary):
    async with httpx.AsyncClient() as c:
        r = await c.put(f"{primary.url}/internal/sources/oci/fake", headers=ADMIN,
                        json={"base_url": "https://elsewhere.example"})
        assert r.status_code == 409 and "configuration" in r.json()["error"]
        assert (await c.delete(f"{primary.url}/internal/sources/oci/fake",
                               headers=ADMIN)).status_code == 409


async def test_admin_role_secret_and_primary_are_required(primary, replica):
    from ociproxy.auth import create_user

    create_user(primary.app_obj.store, "pat", "pusher", "pusherpass1")
    body = {"kind": "oci", "name": "x", "base_url": "https://r.example"}
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{primary.url}/internal/sources", json=body,
                         headers={**ADMIN, "X-OCIProxy-Acting-User": "pat"})
        assert r.status_code == 403
        r = await c.post(f"{primary.url}/internal/sources", json=body,
                         headers={"X-OCIProxy-Secret": "wrong",
                                  "X-OCIProxy-Acting-User": "ci"})
        assert r.status_code == 403
        assert (await _create(c, replica, **body)).status_code == 403
        create_user(replica.app_obj.store, "ci", "admin", "replica-admin-password")
        assert (await _create(c, replica, **{**body, "sharing":"local", "visibility":"private"})).status_code == 201


async def test_shadowed_by_a_later_config_entry(primary, fake_upstream):
    async with httpx.AsyncClient() as c:
        assert (await _create(c, primary, kind="oci", name="later",
                              base_url=_fake_url(fake_upstream))).status_code == 201
    # The operator then adds the same name to the configuration.
    from ociproxy.config import UpstreamConfig

    registry = primary.app_obj.sources
    registry.static_upstreams.append(
        UpstreamConfig(name="later", base_url="https://config.example", prefix="later"))
    registry.reload()
    assert [u.base_url for u in primary.app_obj.config.upstreams if u.name == "later"] == \
        ["https://config.example"]
    async with httpx.AsyncClient() as c:
        managed = [i for i in await _listing(c, primary) if i["name"] == "later" and i["managed"]]
    assert managed[0]["active"] is False and managed[0]["shadowed_by"] == "later"


async def test_feature_switch_off(tmp_path, upstream_config, fake_upstream):
    cfg = make_config(tmp_path, "primary", name="off", upstreams=[upstream_config],
                      push_username="ci", push_password="cipass")
    node = Node(cfg)
    # A source stored while the feature was on is ignored when it's off.
    node.app_obj.store.put_source("oci", "kept", {"base_url": _fake_url(fake_upstream)}, None)
    node.app_obj.config.managed_sources = False
    node.app_obj.sources.managed_enabled = False
    node.app_obj.sources.reload()
    node.start()
    try:
        assert node.app_obj.config.upstream_by_prefix("kept") is None
        async with httpx.AsyncClient() as c:
            r = await _create(c, node, kind="oci", name="x", base_url="https://r.example")
            assert r.status_code == 403
            kept = next(i for i in await _listing(c, node) if i["name"] == "kept")
            assert kept["active"] is False
            # cleaning up stays possible
            assert (await c.delete(f"{node.url}/internal/sources/oci/kept",
                                   headers=ADMIN)).status_code == 204
    finally:
        node.stop()


# -- credentials ----------------------------------------------------------------------------

async def test_helm_repo_credentials_stay_on_the_primary(tmp_path, primary, replica, fake_upstream):
    fake_upstream.helm_auth = ("robot", "hunter2-pass")
    fake_upstream.add_helm_chart("mychart", "1.0.0")
    async with httpx.AsyncClient() as c:
        r = await _create(c, primary, kind="helm", name="private",
                          base_url=_fake_url(fake_upstream, "/charts"),
                          credentials={"username": "robot", "password": "hunter2-pass"})
        assert r.status_code == 201, r.text
        assert r.json()["has_credentials"] is True
        assert "hunter2" not in r.text

        assert replica.app_obj.config.helm_repo("private") is None
        assert (await c.get(f"{replica.url}/helm/private/index.yaml")).status_code == 404
        r = await c.get(f"{primary.url}/helm/private/index.yaml", auth=("ci","cipass"))
        assert r.status_code == 200 and b"mychart" in r.content
        r = await c.get(f"{primary.url}/helm/private/charts/mychart/1.0.0/mychart-1.0.0.tgz", auth=("ci","cipass"))
        assert r.status_code == 200

        listing = await c.get(f"{primary.url}/internal/upstreams", headers=ADMIN)
        assert "hunter2" not in listing.text and "robot" not in listing.text

    # At rest: encrypted on the primary, absent on the replica and in the changelog.
    p_row = primary.app_obj.store.get_source("helm", "private")
    assert p_row["credentials"].startswith("v1:") and "hunter2" not in p_row["credentials"]
    assert replica.app_obj.store.get_source("helm", "private") is None
    assert replica.app_obj.config.helm_repo("private") is None
    for ev in primary.app_obj.store.changelog_since(0, limit=10000):
        assert b"hunter2" not in (ev.bytes or b"")
    for f in (tmp_path / "storage-p1").glob("index.db*"):  # WAL included
        assert b"hunter2" not in f.read_bytes(), f

    async with httpx.AsyncClient() as c:
        # Editing without "credentials" keeps them; null clears them.
        r = await c.put(f"{primary.url}/internal/sources/helm/private", headers=ADMIN,
                        json={"base_url": _fake_url(fake_upstream, "/charts"), "visibility": "public"})
        assert r.json()["has_credentials"] is True
        r = await c.put(f"{primary.url}/internal/sources/helm/private", headers=ADMIN,
                        json={"base_url": _fake_url(fake_upstream, "/charts"), "credentials": None})
        assert r.json()["has_credentials"] is False
        assert primary.app_obj.config.helm_repo("private").username is None


async def test_stored_credentials_never_follow_a_new_host(primary, fake_upstream):
    fake_upstream.helm_auth = ("robot", "hunter2-pass")
    async with httpx.AsyncClient() as c:
        assert (await _create(c, primary, kind="helm", name="h",
                              base_url=_fake_url(fake_upstream, "/charts"),
                              credentials={"username": "robot", "password": "hunter2-pass"})
                ).status_code == 201
        # Same origin, other path: kept.
        r = await c.put(f"{primary.url}/internal/sources/helm/h", headers=ADMIN,
                        json={"base_url": _fake_url(fake_upstream, "/charts/")})
        assert r.status_code == 200 and r.json()["has_credentials"] is True
        # Another host without re-entering them: refused.
        r = await c.put(f"{primary.url}/internal/sources/helm/h", headers=ADMIN,
                        json={"base_url": "https://elsewhere.example/charts"})
        assert r.status_code == 400 and "re-enter" in r.json()["error"]
        # The connectivity test only uses stored credentials on their own origin.
        r = await c.post(f"{primary.url}/internal/sources/test", headers=ADMIN,
                         json={"kind": "helm", "name": "h"})
        assert r.json()["ok"] is True, r.text
        # Same server under another origin: no credentials, so the source says 401.
        r = await c.post(f"{primary.url}/internal/sources/test", headers=ADMIN, json={
            "kind": "helm", "name": "h",
            "base_url": f"http://localhost:{fake_upstream.port}/charts"})
        assert r.json()["ok"] is False and r.json()["status"] == 401, r.text


def test_credential_box():
    box = CredentialBox("secret-one")
    sealed = box.seal("oci", "a", "user", "pass")
    assert box.open("oci", "a", sealed) == ("user", "pass")
    assert box.open("oci", "b", sealed) is None          # bound to its entry
    assert CredentialBox("secret-two").open("oci", "a", sealed) is None  # rotated secret
    assert box.open("oci", "a", "v1:" + "A" * 40) is None
    assert box.open("oci", "a", "v9:abc") is None


async def test_unreadable_credentials_are_reported(primary, fake_upstream):
    store = primary.app_obj.store
    sealed = CredentialBox("an-old-secret").seal("oci", "old", "u", "p")
    store.put_source("oci", "old", {"base_url": _fake_url(fake_upstream)}, sealed)
    item = next(i for i in primary.app_obj.sources.describe(primary.app_obj.policy)
                if i["name"] == "old")
    assert item["credentials_unreadable"] is True and item["has_credentials"] is False
    assert item["active"] is True  # still usable anonymously
    assert primary.app_obj.config.upstream_by_prefix("old").username is None


# -- connectivity test ------------------------------------------------------------------------

async def test_probe(primary, fake_upstream):
    url = f"{primary.url}/internal/sources/test"
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=ADMIN, json={"kind": "oci", "base_url": _fake_url(fake_upstream)})
        assert r.json()["ok"] is True, r.text  # 401 + Www-Authenticate: a registry
        r = await c.post(url, headers=ADMIN, json={"kind": "helm", "base_url": _fake_url(fake_upstream, "/nope")})
        assert r.json()["ok"] is False and r.json()["status"] == 404
        r = await c.post(url, headers=ADMIN, json={"kind": "oci", "base_url": "http://127.0.0.1:1"})
        assert r.json()["ok"] is False
        # A saved entry is tested with its stored URL (and credentials).
        r = await c.post(url, headers=ADMIN, json={"kind": "oci", "name": "fake"})
        assert r.json()["ok"] is True


# -- changelog --------------------------------------------------------------------------------

def test_compaction_keeps_newest_source_event(tmp_path):
    from ociproxy.store import Store
    store = Store(str(tmp_path / "s"))
    for i in range(3):
        store.publish_config_sources({"protocol":"public-v2","upstreams":[{"name":"a", "base_url":f"https://{i}.example", "sharing":"public", "visibility":"public"}], "helm":[]})
    store.put_source("oci","private",{"base_url":"https://private.example"},None)
    store.compact_changelog()
    events = [e for e in store.changelog_since(0) if e.kind == "source"]
    assert len(events) == 1
    assert json.loads(events[0].bytes)["upstreams"][0]["base_url"] == "https://2.example"
    assert b"private.example" not in events[0].bytes
    store.close()


# -- the primary's config-file list ------------------------------------------------

async def test_replica_uses_the_primarys_configured_sources(tmp_path, fake_upstream):
    from ociproxy.config import HelmRepoConfig, UpstreamConfig

    img = fake_upstream.install_image(repo="team/app", tag="v1")
    fake_upstream.add_helm_chart("mychart", "1.0.0")
    primary = Node(make_config(
        tmp_path, "primary", name="p1",
        upstreams=[UpstreamConfig(name="fake", base_url=_fake_url(fake_upstream), prefix="fake",
                                  visibility="public", sharing="public")],
        helm_repos=[HelmRepoConfig(name="charts", base_url=_fake_url(fake_upstream, "/charts"), visibility="public", sharing="public")],
        push_username="ci", push_password="cipass",
    )).start()
    # The replica's own list is empty except for an entry the primary lacks.
    port = free_port()
    replica = Node(make_config(
        tmp_path, "replica", name="r1",
        upstreams=[UpstreamConfig(name="stale", base_url="https://stale.example", prefix="stale")],
        primary_url=primary.url, advertised_url=f"http://127.0.0.1:{port}", port=port,
        push_username="ci", push_password="cipass",
    )).start()
    try:
        cfg = replica.app_obj.config
        await wait_until(lambda: cfg.upstream_by_prefix("fake") is not None,
                         msg="replica to receive the primary's upstreams")
        assert cfg.upstream_by_prefix("stale") is not None
        assert cfg.helm_repo("charts") is not None
        # Routing and visibility only: credentials never leave the primary.
        assert cfg.upstream_by_prefix("fake").username is None
        assert cfg.upstream_by_prefix("fake").visibility == "public"
        for ev in primary.app_obj.store.changelog_since(0, limit=10000):
            assert b"hunter2" not in (ev.bytes or b"") and b"robot" not in (ev.bytes or b"")

        async with httpx.AsyncClient() as c:
            url = f"{replica.url}/v2/fake/team/app/manifests/v1"
            assert (await c.get(url)).status_code == 200
            r = await c.get(url, auth=("ci", "cipass"))
            assert r.status_code == 200
            assert r.headers["docker-content-digest"] == img["manifest"]
            r = await c.get(f"{replica.url}/helm/charts/index.yaml")
            assert r.status_code == 200 and b"mychart" in r.content
    finally:
        replica.stop()
        primary.stop()

    # Kept across a restart, even before the primary is reachable again.
    again = Node(replica.config)
    try:
        assert again.app_obj.config.upstream_by_prefix("fake") is not None
        assert again.app_obj.config.upstream_by_prefix("stale") is not None
    finally:
        again.app_obj.store.close()


def test_config_sources_are_published_only_when_changed(tmp_path):
    from ociproxy.config import UpstreamConfig

    def boot(upstreams):
        node = Node(make_config(tmp_path, "primary", name="p", upstreams=upstreams))
        events = [e for e in node.app_obj.store.changelog_since(0) if e.reference == "config"]
        node.app_obj.store.close()
        return events

    a = UpstreamConfig(name="a", base_url="https://a.example", prefix="a", visibility="public", sharing="public")
    assert len(boot([a])) == 1
    assert len(boot([a])) == 1  # restart, same config: nothing new
    b = UpstreamConfig(name="b", base_url="https://b.example", prefix="b", visibility="public", sharing="public")
    events = boot([a, b])
    assert len(events) == 2
    assert [u["name"] for u in json.loads(events[-1].bytes)["upstreams"]] == ["a", "b"]


async def test_replica_managed_switch_is_independent(tmp_path, primary, fake_upstream):
    primary.config.managed_sources = False
    primary.app_obj.sources.reload()
    replica = make_replica(tmp_path, primary, "r1")
    try:
        await wait_until(lambda: replica.app_obj.config.upstream_by_prefix("fake") is not None)
        assert replica.app_obj.sources.managed_enabled is True
        replica.app_obj.store.put_source("oci", "m", {"base_url":_fake_url(fake_upstream)}, None)
        assert replica.app_obj.config.upstream_by_prefix("m") is not None
        assert primary.app_obj.config.upstream_by_prefix("m") is None
    finally:
        replica.stop()


def test_v1_encrypted_credentials_survive_product_rename():
    # A fixture produced before the rename, with synthetic credentials only.
    sealed = 'v1:++FWTpA/7JcEwloDklE41ybz/C1YNciEVz2IP7OOnC1FUzOOqlvUW1Hcg2pVK1LVJFHeCekEmvSbJwyj2u0CYgUfRE0dNW/Hiv0X2eecgtD0aflAbxBWGQ=='
    box = CredentialBox("rename-compatibility-test-secret")
    assert box.open("oci", "compat-source", sealed) == ("fixture-user", "fixture-password")
    assert box.open("oci", "different-source", sealed) is None
