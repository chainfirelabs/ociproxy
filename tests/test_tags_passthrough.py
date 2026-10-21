"""tags/list for mirrored repos: upstream passthrough merged with the cache,
and Helm charts stored as OCI artifacts."""

from __future__ import annotations

import hashlib

import httpx

from .conftest import Node, free_port, make_config
from .fake_upstream import OCI_MANIFEST


async def test_tags_listed_from_upstream_without_pulling(primary, fake_upstream):
    """The full upstream list (followed across Link pages), not just what
    was pulled: Helm resolves "latest" and version ranges from it."""
    for tag in ("v1", "v2", "v3", "v4", "v5"):
        fake_upstream.add_manifest("many", tag, OCI_MANIFEST, f'{{"t":"{tag}"}}'.encode())
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{primary.url}/v2/fake/many/tags/list")
        assert r.status_code == 200
        assert r.json() == {"name": "fake/many", "tags": ["v1", "v2", "v3", "v4", "v5"]}
    # 5 tags at page size 2 -> 3 upstream pages
    assert fake_upstream.hits["tags:many"] == 3


async def test_tags_merge_cached_and_upstream(primary, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")).status_code == 200
        fake_upstream.add_manifest("myimg", "v9", OCI_MANIFEST, b'{"new": 1}')
        # v1's cached copy stays listed even after upstream drops it
        del fake_upstream.manifests[("myimg", "v1")]
        primary.app_obj.resolver._tag_lists.clear()
        r = await c.get(f"{primary.url}/v2/fake/myimg/tags/list")
        assert r.json()["tags"] == ["multi", "v1", "v9"]


async def test_tags_cached_for_tag_ttl(primary, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        for _ in range(3):
            assert (await c.get(f"{primary.url}/v2/fake/myimg/tags/list")).status_code == 200
    assert fake_upstream.hits["tags:myimg"] == 1


async def test_tags_via_replica_come_from_primary(primary, replica, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{replica.url}/v2/fake/myimg/tags/list")
        assert r.status_code == 200
        assert r.json()["tags"] == ["multi", "v1"]
        # the primary cached the upstream list; asking it again is free
        r = await c.get(f"{primary.url}/v2/fake/myimg/tags/list")
        assert r.json()["tags"] == ["multi", "v1"]
    assert fake_upstream.hits["tags:myimg"] == 1


async def test_tags_upstream_failure_serves_cached(tmp_path, fake_upstream, upstream_config):
    fake_upstream.install_image()
    node = Node(make_config(tmp_path, "primary", name="tf", upstreams=[upstream_config],
                            tag_ttl=0)).start()
    try:
        async with httpx.AsyncClient() as c:
            assert (await c.get(f"{node.url}/v2/fake/myimg/manifests/v1")).status_code == 200
            fake_upstream.manifests.clear()  # upstream now 404s the repo
            r = await c.get(f"{node.url}/v2/fake/myimg/tags/list")
            assert r.status_code == 200
            assert r.json()["tags"] == ["v1"]
    finally:
        node.stop()


async def test_tags_unknown_everywhere_is_404(primary, replica):
    async with httpx.AsyncClient(timeout=30) as c:
        for node in (primary, replica):
            r = await c.get(f"{node.url}/v2/fake/nope/tags/list")
            assert r.status_code == 404
            assert r.json()["errors"][0]["code"] == "NAME_UNKNOWN"


async def test_oci_helm_chart_pulls_through_replica(primary, replica, fake_upstream):
    """Helm OCI charts are plain OCI artifacts: helm config + one tgz layer."""
    chart = fake_upstream.install_oci_chart("charts/mychart", "1.2.3")
    fake_upstream.install_oci_chart("charts/mychart", "1.3.0")
    base = f"{replica.url}/v2/fake/charts/mychart"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{base}/tags/list")
        assert r.json()["tags"] == ["1.2.3", "1.3.0"]
        # the Accept header Helm (ORAS) sends
        r = await c.get(f"{base}/manifests/1.2.3", headers={"Accept": OCI_MANIFEST})
        assert r.status_code == 200
        assert r.headers["content-type"] == OCI_MANIFEST
        assert r.headers["docker-content-digest"] == chart["manifest"]
        for digest in (chart["config"], chart["chart"]):
            b = await c.get(f"{base}/blobs/{digest}")
            assert b.status_code == 200
            assert "sha256:" + hashlib.sha256(b.content).hexdigest() == digest
    # both blobs are referenced by the stored manifest, so GC keeps them
    store = primary.app_obj.store
    assert {chart["config"], chart["chart"]} <= store._referenced_blobs()


async def test_default_upstream_tags_for_unprefixed_repo(tmp_path, fake_upstream, upstream_config):
    upstream_config.default = True
    fake_upstream.install_image()
    port = free_port()
    node = Node(make_config(tmp_path, "primary", name="td", upstreams=[upstream_config],
                            port=port)).start()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{node.url}/v2/myimg/tags/list")
            assert r.status_code == 200
            assert r.json()["tags"] == ["multi", "v1"]
    finally:
        node.stop()
