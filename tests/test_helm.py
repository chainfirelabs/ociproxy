"""Classic HTTP Helm repositories: index rewrite, chart pull-through,
replication, revalidation, auth, GC."""

from __future__ import annotations

import hashlib

import httpx
import pytest
import yaml

from containerproxy.config import HelmRepoConfig, load_config
from containerproxy.helm import INDEX_REF, INDEX_REPO, pointer_blob, rewrite_index

from .conftest import Node, free_port, make_config, wait_until


@pytest.fixture
def helm_config(fake_upstream):
    return HelmRepoConfig(name="stable", base_url=f"http://127.0.0.1:{fake_upstream.port}/charts", visibility="public", sharing="public")


def _cluster(tmp_path, helm_config, *, replicas: int = 1, tag_ttl: float = 300.0,
             pull_auth: str = "none"):
    auth = {"push_username": "ci", "push_password": "cipass", "pull_auth": pull_auth}
    primary = Node(make_config(tmp_path, "primary", name="hp", helm_repos=[helm_config],
                               tag_ttl=tag_ttl, **auth)).start()
    nodes = [primary]
    for i in range(replicas):
        port = free_port()
        nodes.append(Node(make_config(
            tmp_path, "replica", name=f"hr{i}", helm_repos=[helm_config], tag_ttl=tag_ttl,
            primary_url=primary.url, advertised_url=f"http://127.0.0.1:{port}", port=port, **auth,
        )).start())
    return nodes


def _stop(nodes):
    for n in reversed(nodes):
        n.stop()


@pytest.fixture
def helm_nodes(tmp_path, helm_config):
    nodes = _cluster(tmp_path, helm_config)
    yield nodes
    _stop(nodes)


def _sql(store, query: str, *args) -> None:
    """Write to a live node's index under its lock (the connection is shared)."""
    with store._lock:
        store.db.execute(query, args)
        store.db.commit()


def _expire_index(store) -> None:
    _sql(store, "UPDATE manifests SET checked_at = 0, stored_at = 0 "
                "WHERE upstream = 'helm/stable' AND repo = ?", INDEX_REPO)


def _chart_url(node, chart, version, repo="stable"):
    return f"{node.url}/helm/{repo}/charts/{chart}/{version}/{chart}-{version}.tgz"


# -- index rewrite (unit) ----------------------------------------------------------

def test_rewrite_keeps_everything_but_urls():
    src = b"""apiVersion: v1
entries:
  app:
  - appVersion: 1.10
    created: 2026-01-02T03:04:05.123456789Z
    digest: AABBCCDDEEFF00112233445566778899aabbccddeeff00112233445566778899
    name: app
    urls:
    - https://github.com/org/repo/releases/download/app-1.0.0/app-1.0.0.tgz
    version: 1.0.0
  - name: app
    urls:
    - app-0.9.0+build.1.tgz
    version: 0.9.0+build.1
  broken:
  - name: broken
    version: 1.0.0
generated: 2026-01-02T03:04:05Z
"""
    out, sources = rewrite_index(src, "https://charts.example/sub")
    text = out.decode()
    # scalars keep their exact original text
    assert "appVersion: 1.10\n" in text
    assert "created: 2026-01-02T03:04:05.123456789Z" in text
    assert "generated: 2026-01-02T03:04:05Z" in text
    doc = yaml.load(out, Loader=yaml.BaseLoader)  # all scalars as strings
    assert doc["entries"]["app"][0]["urls"] == ["charts/app/1.0.0/app-1.0.0.tgz"]
    assert doc["entries"]["app"][1]["urls"] == ["charts/app/0.9.0+build.1/app-0.9.0+build.1.tgz"]
    assert doc["entries"]["broken"][0] == {"name": "broken", "version": "1.0.0"}
    assert sources == [
        ("app", "1.0.0", "https://github.com/org/repo/releases/download/app-1.0.0/app-1.0.0.tgz",
         "sha256:aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"),
        ("app", "0.9.0+build.1", "https://charts.example/sub/app-0.9.0+build.1.tgz", None),
    ]


def test_rewrite_rejects_non_index():
    from containerproxy.resolver import ResolveError

    with pytest.raises(ResolveError):
        rewrite_index(b"- just\n- a list\n", "https://x")


# -- end to end ---------------------------------------------------------------------

async def test_index_is_rewritten_to_point_at_proxy(helm_nodes, fake_upstream):
    primary, replica = helm_nodes
    fake_upstream.add_helm_chart("app", "1.0.0")
    async with httpx.AsyncClient(timeout=30) as c:
        for node in (primary, replica):
            r = await c.get(f"{node.url}/helm/stable/index.yaml")
            assert r.status_code == 200, r.text
            doc = yaml.load(r.content, Loader=yaml.BaseLoader)
            entry = doc["entries"]["app"][0]
            assert entry["urls"] == ["charts/app/1.0.0/app-1.0.0.tgz"]
            assert entry["appVersion"] == "1.10"
            assert 'appVersion: "1.10"\n' in r.text  # quoting style kept
    assert fake_upstream.hits["helm-index"] == 1


async def test_chart_pull_through_replica_fetches_upstream_once(helm_nodes, fake_upstream):
    primary, replica = helm_nodes
    tgz = fake_upstream.add_helm_chart("app", "1.0.0")
    async with httpx.AsyncClient(timeout=30) as c:
        for node in (replica, primary, replica):
            r = await c.get(_chart_url(node, "app", "1.0.0"))
            assert r.status_code == 200, r.text
            assert r.content == tgz
    assert fake_upstream.hits["helm-file:/charts/app-1.0.0.tgz"] == 1


async def test_chart_fans_out_to_every_replica(tmp_path, helm_config, fake_upstream):
    nodes = _cluster(tmp_path, helm_config, replicas=2)
    primary, r1, r2 = nodes
    try:
        tgz = fake_upstream.add_helm_chart("app", "2.0.0")
        digest = "sha256:" + hashlib.sha256(tgz).hexdigest()
        async with httpx.AsyncClient(timeout=30) as c:
            assert (await c.get(_chart_url(r1, "app", "2.0.0"))).status_code == 200
        store = r2.app_obj.store
        await wait_until(
            lambda: store.manifest_exists("helm/stable", "app", "2.0.0") and store.has_blob(digest),
            msg="chart pointer + archive replicated to the other replica",
        )
        fake_upstream.stop()  # r2 must serve without any source
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(_chart_url(r2, "app", "2.0.0"))
                assert r.content == tgz
        finally:
            fake_upstream.start()
    finally:
        _stop(nodes)


async def test_absolute_and_digestless_urls(helm_nodes, fake_upstream):
    primary, replica = helm_nodes
    external = f"http://localhost:{fake_upstream.port}/ext/other-1.0.0.tgz"
    tgz = fake_upstream.add_helm_chart("other", "1.0.0", url=external, with_digest=False)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(_chart_url(replica, "other", "1.0.0"))
        assert r.status_code == 200
        assert r.content == tgz
    assert fake_upstream.hits["helm-file:/ext/other-1.0.0.tgz"] == 1


async def test_digest_mismatch_is_rejected(helm_nodes, fake_upstream):
    primary, _ = helm_nodes
    fake_upstream.add_helm_chart("app", "1.0.0")
    fake_upstream.helm_files["/charts/app-1.0.0.tgz"] = b"tampered"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(_chart_url(primary, "app", "1.0.0"))
        assert r.status_code == 502
        assert "digest mismatch" in r.text
    assert not primary.app_obj.store.manifest_exists("helm/stable", "app", "1.0.0")


async def test_unknown_chart_repo_and_file(helm_nodes, fake_upstream):
    primary, replica = helm_nodes
    fake_upstream.add_helm_chart("app", "1.0.0")
    async with httpx.AsyncClient(timeout=30) as c:
        for node in (primary, replica):
            assert (await c.get(_chart_url(node, "app", "9.9.9"))).status_code == 404
            assert (await c.get(_chart_url(node, "nope", "1.0.0"))).status_code == 404
            assert (await c.get(f"{node.url}/helm/other/index.yaml")).status_code == 404
            r = await c.get(f"{node.url}/helm/stable/charts/app/1.0.0/app-1.0.0.tgz.prov")
            assert r.status_code == 404


async def test_index_revalidates_with_etag_and_picks_up_new_versions(
    tmp_path, helm_config, fake_upstream,
):
    nodes = _cluster(tmp_path, helm_config, tag_ttl=0)
    primary, replica = nodes
    try:
        fake_upstream.add_helm_chart("app", "1.0.0")
        async with httpx.AsyncClient(timeout=30) as c:
            assert (await c.get(f"{replica.url}/helm/stable/index.yaml")).status_code == 200
            assert (await c.get(f"{replica.url}/helm/stable/index.yaml")).status_code == 200
            assert fake_upstream.hits["helm-index"] == 1
            assert fake_upstream.hits["helm-index-304"] >= 1

            tgz = fake_upstream.add_helm_chart("app", "1.1.0")
            r = await c.get(f"{replica.url}/helm/stable/index.yaml")
            versions = [e["version"] for e in yaml.safe_load(r.content)["entries"]["app"]]
            assert versions == ["1.1.0", "1.0.0"]
            r = await c.get(_chart_url(replica, "app", "1.1.0"))
            assert r.content == tgz
    finally:
        _stop(nodes)


async def test_chart_newer_than_cached_index_is_found(helm_nodes, fake_upstream):
    """While the cached index is fresh, versions it doesn't list are 404;
    once it is stale, a request for an unlisted version refreshes it."""
    primary, _ = helm_nodes
    fake_upstream.add_helm_chart("app", "1.0.0")
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(f"{primary.url}/helm/stable/index.yaml")).status_code == 200
        fake_upstream.add_helm_chart("app", "1.1.0")
        assert (await c.get(_chart_url(primary, "app", "1.1.0"))).status_code == 404
        # expire the cached index: the chart request refreshes it
        store = primary.app_obj.store
        _expire_index(store)
        assert (await c.get(_chart_url(primary, "app", "1.1.0"))).status_code == 200


async def test_index_source_down_serves_cached(tmp_path, helm_config, fake_upstream):
    nodes = _cluster(tmp_path, helm_config, tag_ttl=0)
    primary, replica = nodes
    try:
        fake_upstream.add_helm_chart("app", "1.0.0")
        async with httpx.AsyncClient(timeout=30) as c:
            first = await c.get(f"{replica.url}/helm/stable/index.yaml")
            fake_upstream.helm_raw_index = b"not: [valid"  # upstream now broken
            again = await c.get(f"{replica.url}/helm/stable/index.yaml")
            assert again.status_code == 200
            assert again.content == first.content
    finally:
        _stop(nodes)


async def test_republished_version_is_refetched_by_primary(helm_nodes, fake_upstream):
    primary, _ = helm_nodes
    fake_upstream.add_helm_chart("app", "1.0.0", app_version="1")
    async with httpx.AsyncClient(timeout=30) as c:
        await c.get(_chart_url(primary, "app", "1.0.0"))
        fake_upstream.helm_entries.clear()
        new = fake_upstream.add_helm_chart("app", "1.0.0", app_version="2")
        store = primary.app_obj.store
        _expire_index(store)
        assert (await c.get(f"{primary.url}/helm/stable/index.yaml")).status_code == 200
        r = await c.get(_chart_url(primary, "app", "1.0.0"))
        assert r.content == new


async def test_upstream_credentials_only_sent_to_repo_origin(tmp_path, fake_upstream):
    fake_upstream.helm_auth = ("reader", "s3cret")
    helm = HelmRepoConfig(name="stable", base_url=f"http://127.0.0.1:{fake_upstream.port}/charts",
                          username="reader", password="s3cret", visibility="public")
    nodes = _cluster(tmp_path, helm, replicas=0)
    try:
        fake_upstream.add_helm_chart("app", "1.0.0")
        external = f"http://localhost:{fake_upstream.port}/ext/ext-1.0.0.tgz"
        fake_upstream.add_helm_chart("ext", "1.0.0", url=external)
        async with httpx.AsyncClient(timeout=30) as c:
            assert (await c.get(_chart_url(nodes[0], "app", "1.0.0"))).status_code == 200
            assert (await c.get(_chart_url(nodes[0], "ext", "1.0.0"))).status_code == 200
        assert fake_upstream.helm_auth_seen["/charts/app-1.0.0.tgz"].startswith("Basic ")
        assert fake_upstream.helm_auth_seen["/ext/ext-1.0.0.tgz"] is None
    finally:
        _stop(nodes)


async def test_upstream_auth_failure_is_not_a_client_401(helm_nodes, fake_upstream):
    primary, _ = helm_nodes
    fake_upstream.helm_auth = ("reader", "s3cret")  # proxy has no creds configured
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{primary.url}/helm/stable/index.yaml")
        assert r.status_code == 502


async def test_private_pulls_gate_helm(tmp_path, helm_config, fake_upstream):
    from dataclasses import replace
    helm_config = replace(helm_config, sharing="local", visibility="private")
    nodes = _cluster(tmp_path, helm_config, pull_auth="basic")
    try:
        fake_upstream.add_helm_chart("app", "1.0.0")
        async with httpx.AsyncClient(timeout=30) as c:
            for node in nodes:
                r = await c.get(f"{node.url}/helm/stable/index.yaml")
                assert r.status_code == 401
                assert r.headers["www-authenticate"].startswith("Basic")
                r = await c.get(f"{node.url}/helm/stable/index.yaml", auth=("ci", "cipass"))
                assert r.status_code == 200
                r = await c.get(_chart_url(node, "app", "1.0.0"), auth=("ci", "cipass"))
                assert r.status_code == 200
    finally:
        _stop(nodes)


async def test_gc_keeps_referenced_charts_and_expires_idle_ones(helm_nodes, fake_upstream):
    primary, _ = helm_nodes
    tgz = fake_upstream.add_helm_chart("app", "1.0.0")
    digest = "sha256:" + hashlib.sha256(tgz).hexdigest()
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(_chart_url(primary, "app", "1.0.0"))).status_code == 200
    store = primary.app_obj.store
    index = store.get_manifest("helm/stable", INDEX_REPO, INDEX_REF)
    _sql(store, "UPDATE blobs SET stored_at = 0")
    store.gc(manifest_max_idle=0, blob_grace=1)
    assert store.has_blob(digest)
    assert store.has_blob(pointer_blob(index))
    # expire everything mirrored: the archive and the index go with it
    _sql(store, "UPDATE manifests SET stored_at = 0, checked_at = 0, pulled_at = 0")
    result = store.gc(manifest_max_idle=1, blob_grace=1)
    assert result.manifests == 2
    assert not store.has_blob(digest)


async def test_helm_records_stay_out_of_registry_catalog(helm_nodes, fake_upstream):
    primary, _ = helm_nodes
    fake_upstream.add_helm_chart("app", "1.0.0")
    async with httpx.AsyncClient(timeout=30) as c:
        await c.get(_chart_url(primary, "app", "1.0.0"))
        r = await c.get(f"{primary.url}/v2/_catalog")
        assert r.json()["repositories"] == []
        # but the admin surface lists them
        r = await c.get(f"{primary.url}/internal/images", headers={"X-ContainerProxy-Secret": "test-secret"})
        assert {m["upstream"] for m in r.json()["items"]} == {"helm/stable"}
        r = await c.get(f"{primary.url}/internal/upstreams", headers={"X-ContainerProxy-Secret": "test-secret"})
        assert [(u["kind"], u["prefix"]) for u in r.json()["items"]] == [("helm", "helm/stable")]


# -- config ---------------------------------------------------------------------------

def test_helm_repos_from_toml_and_env(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[node]\nsecret = "x"\n\n[[helm]]\nname = "bitnami"\n'
                   'base_url = "https://charts.bitnami.com/bitnami/"\n')
    c = load_config(str(cfg))
    assert c.helm_repos == [HelmRepoConfig("bitnami", "https://charts.bitnami.com/bitnami")]
    monkeypatch.setenv("CONTAINERPROXY_HELM_REPOS",
                       '[{"name": "jetstack", "base_url": "https://charts.jetstack.io",'
                       ' "username": "u", "password": "p"}]')
    c = load_config(str(cfg))
    assert [(r.name, r.username) for r in c.helm_repos] == [("jetstack", "u")]


@pytest.mark.parametrize("entry, match", [
    ({"name": "a/b", "base_url": "https://x"}, "invalid name"),
    ({"name": "a", "base_url": "ftp://x"}, "http"),
])
def test_helm_repo_validation(tmp_path, monkeypatch, entry, match):
    import json

    monkeypatch.setenv("CONTAINERPROXY_HELM_REPOS", json.dumps([entry]))
    with pytest.raises(ValueError, match=match):
        load_config(None)


def test_duplicate_helm_repo_rejected(monkeypatch):
    monkeypatch.setenv("CONTAINERPROXY_HELM_REPOS",
                       '[{"name": "a", "base_url": "https://x"}, {"name": "a", "base_url": "https://y"}]')
    with pytest.raises(ValueError, match="duplicate"):
        load_config(None)
