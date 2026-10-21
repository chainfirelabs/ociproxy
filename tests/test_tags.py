"""Tag revalidation, Accept handling, and moved-tag propagation."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from containerproxy.api import App
from containerproxy.upstream import DEFAULT_ACCEPT

from .conftest import Node, free_port, make_config
from .fake_upstream import MANIFEST_LIST, MANIFEST_V2
from .test_push import AUTH, push_blob


def _nodes(tmp_path, upstream_config, tag_ttl: float):
    primary = Node(make_config(
        tmp_path, "primary", name="tp", upstreams=[upstream_config], tag_ttl=tag_ttl,
        push_username="ci", push_password="cipass",
    )).start()
    port = free_port()
    replica = Node(make_config(
        tmp_path, "replica", name="tr", upstreams=[upstream_config], tag_ttl=tag_ttl,
        primary_url=primary.url, advertised_url=f"http://127.0.0.1:{port}", port=port,
    )).start()
    return primary, replica


async def test_client_accept_is_not_forwarded_upstream(primary, fake_upstream):
    """A narrow client Accept must not decide what gets cached for everyone."""
    fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{primary.url}/v2/fake/myimg/manifests/multi",
                        headers={"Accept": MANIFEST_V2})
        assert r.status_code == 200
        assert r.headers["content-type"] == MANIFEST_LIST
    assert fake_upstream.last_accept == DEFAULT_ACCEPT


async def test_fresh_tag_is_not_revalidated(primary, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        for _ in range(3):
            assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")).status_code == 200
    assert fake_upstream.hits["manifest:myimg:v1"] == 1
    assert fake_upstream.hits["manifest-head:myimg:v1"] == 0


async def test_stale_unchanged_tag_revalidates_with_head_only(tmp_path, fake_upstream, upstream_config):
    fake_upstream.install_image()
    primary, replica = _nodes(tmp_path, upstream_config, tag_ttl=0)
    try:
        async with httpx.AsyncClient() as c:
            for _ in range(3):
                assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")).status_code == 200
        assert fake_upstream.hits["manifest:myimg:v1"] == 1
        assert fake_upstream.hits["manifest-head:myimg:v1"] == 2
    finally:
        replica.stop()
        primary.stop()


async def test_moved_upstream_tag_reaches_primary_and_replica(tmp_path, fake_upstream, upstream_config):
    old = fake_upstream.install_image()
    primary, replica = _nodes(tmp_path, upstream_config, tag_ttl=0)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
            assert r.headers["docker-content-digest"] == old["manifest"]

            new = fake_upstream.install_image()  # re-points myimg:v1 upstream
            assert new["manifest"] != old["manifest"]

            r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
            assert r.headers["docker-content-digest"] == new["manifest"]
            r = await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")
            assert r.headers["docker-content-digest"] == new["manifest"]
            # the new layers are pullable through the replica
            b = await c.get(f"{replica.url}/v2/fake/myimg/blobs/{new['layer1']}")
            assert "sha256:" + hashlib.sha256(b.content).hexdigest() == new["layer1"]
    finally:
        replica.stop()
        primary.stop()


async def test_revalidation_failure_serves_cached(tmp_path, fake_upstream, upstream_config):
    img = fake_upstream.install_image()
    primary, replica = _nodes(tmp_path, upstream_config, tag_ttl=0)
    try:
        async with httpx.AsyncClient() as c:
            assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")).status_code == 200
            fake_upstream.manifests.clear()  # upstream now 404s everything
            r = await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")
            assert r.status_code == 200
            assert r.headers["docker-content-digest"] == img["manifest"]
    finally:
        replica.stop()
        primary.stop()


async def test_digest_references_are_never_revalidated(tmp_path, fake_upstream, upstream_config):
    img = fake_upstream.install_image()
    primary, replica = _nodes(tmp_path, upstream_config, tag_ttl=0)
    try:
        async with httpx.AsyncClient() as c:
            url = f"{primary.url}/v2/fake/myimg/manifests/{img['manifest']}"
            for _ in range(3):
                assert (await c.get(url)).status_code == 200
        assert fake_upstream.hits[f"manifest-head:myimg:{img['manifest']}"] == 0
    finally:
        replica.stop()
        primary.stop()


async def test_repushed_local_tags_are_independent(primary, replica):
    from containerproxy.auth import create_user
    create_user(replica.app_obj.store, "replica-pusher", "pusher", "replica-password")

    async def push_manifest(c, payload: bytes, auth=AUTH) -> str:
        blob = await push_blob(c, "app", payload, auth=auth)
        assert blob.status_code == 201
        bd = "sha256:" + hashlib.sha256(payload).hexdigest()
        mb = json.dumps({"schemaVersion":2, "mediaType":MANIFEST_V2,
                         "config":{"digest":bd,"size":len(payload)}, "layers":[]}).encode()
        r = await c.put("/v2/app/manifests/latest", content=mb,
                        headers={"content-type":MANIFEST_V2}, auth=auth)
        assert r.status_code == 201
        return r.headers["docker-content-digest"]

    async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
        first = await push_manifest(c, b'{"v":1}')
    async with httpx.AsyncClient(base_url=replica.url, timeout=30) as c:
        local = await push_manifest(c, b'{"v":"replica"}', ("replica-pusher","replica-password"))
    async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
        second = await push_manifest(c, b'{"v":2}')
        assert second != first
        assert (await c.get("/v2/app/manifests/latest")).headers["docker-content-digest"] == second
    async with httpx.AsyncClient(base_url=replica.url, timeout=30) as c:
        assert (await c.get("/v2/app/manifests/latest")).headers["docker-content-digest"] == local


def test_node_refuses_to_start_without_secret(tmp_path):
    cfg = make_config(tmp_path, "primary", name="nosecret")
    cfg.secret = ""
    with pytest.raises(ValueError, match="secret"):
        App(cfg)
