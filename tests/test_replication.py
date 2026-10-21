"""Replication: fan-out push to all replicas + changelog catch-up."""

from __future__ import annotations

import httpx

from .conftest import make_replica, wait_until
from .fake_upstream import MANIFEST_LIST, MANIFEST_V2


async def test_fanout_distributes_to_all_replicas(primary, fake_upstream, upstream_config, tmp_path):
    img = fake_upstream.install_image()
    r1 = make_replica(tmp_path, primary, "fo-r1", [upstream_config])
    r2 = make_replica(tmp_path, primary, "fo-r2", [upstream_config])
    try:
        # wait for both to register with the primary
        await wait_until(lambda: len(primary.app_obj.store.live_replicas()) == 2,
                        msg="both replicas registered")

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{r1.url}/v2/fake/myimg/manifests/v1")
            assert r.status_code == 200
            # A real `docker pull` fetches every blob after the manifest;
            # content spreads lazily as blobs are actually requested.
            for d in (img["config"], img["layer1"], img["layer2"]):
                assert (await c.get(f"{r1.url}/v2/fake/myimg/blobs/{d}")).status_code == 200

        # r2 must receive everything WITHOUT ever being asked directly
        await wait_until(
            lambda: (
                r2.app_obj.store.manifest_exists("fake", "myimg", "v1")
                and r2.app_obj.store.has_blob(img["config"])
                and r2.app_obj.store.has_blob(img["layer1"])
                and r2.app_obj.store.has_blob(img["layer2"])
            ),
            msg="fan-out delivered image to r2",
        )
        # upstream still saw exactly one fetch per object
        assert fake_upstream.hits["manifest:myimg:v1"] == 1
        for d in (img["config"], img["layer1"], img["layer2"]):
            assert fake_upstream.hits[f"blob:{d}"] == 1
    finally:
        r1.stop()
        r2.stop()


async def test_late_replica_catches_up_via_changelog(primary, replica, fake_upstream, upstream_config, tmp_path):
    img = fake_upstream.install_image()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
        assert r.status_code == 200
        assert (await c.get(f"{replica.url}/v2/fake/myimg/blobs/{img['layer1']}")).status_code == 200

    # join AFTER the content existed; tail must deliver history
    r3 = make_replica(tmp_path, primary, "late-r3", [upstream_config])
    try:
        await wait_until(
            lambda: (
                r3.app_obj.store.manifest_exists("fake", "myimg", "v1")
                and r3.app_obj.store.has_blob(img["layer1"])
            ),
            msg="late replica caught up",
        )
        async with httpx.AsyncClient() as c:
            b = await c.get(f"{r3.url}/v2/fake/myimg/blobs/{img['layer2']}")
            assert b.status_code == 200
    finally:
        r3.stop()


async def test_replica_rejects_push(replica):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{replica.url}/v2/myapp/blobs/uploads/")
        assert r.status_code == 401


async def test_internal_requires_secret(primary):
    primary.config.replication_peers["test"] = {"token":"peer-test-token"}
    async with httpx.AsyncClient() as c:
        path = f"{primary.url}/replication/v2/changelog?since=0&wait=0"
        assert (await c.get(path)).status_code == 403
        assert (await c.get(path, headers={"X-ContainerProxy-Secret":"test-secret"})).status_code == 403
        assert (await c.get(path, headers={"Authorization":"Bearer peer-test-token", "X-ContainerProxy-Peer":"test"})).status_code == 200


async def test_replica_does_not_serve_internal_pull(replica):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{replica.url}/internal/pull/manifest?upstream=fake&repo=myimg&reference=v1",
            headers={"X-ContainerProxy-Secret": "test-secret"},
        )
        assert r.status_code == 404


async def test_primary_rejects_ingest(primary):
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{primary.url}/internal/ingest/blob/sha256:{'a' * 64}",
            headers={"X-ContainerProxy-Secret": "test-secret"},
        )
        assert r.status_code == 404


async def test_tail_preserves_manifest_media_type(primary, replica, fake_upstream, upstream_config, tmp_path):
    """A late replica catching up via the changelog must store real media types,
    not the octet-stream fallback — docker rejects wrong types on unpack."""
    img = fake_upstream.install_image()
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(f"{replica.url}/v2/fake/myimg/manifests/multi")).status_code == 200
    r4 = make_replica(tmp_path, primary, "mt-r4", [upstream_config])
    try:
        await wait_until(lambda: r4.app_obj.store.manifest_exists("fake", "myimg", "multi"),
                        msg="tail delivered index")
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{r4.url}/v2/fake/myimg/manifests/multi")
            assert r.headers["content-type"] == MANIFEST_LIST
            child = await c.get(f"{r4.url}/v2/fake/myimg/manifests/{img['manifest']}")
            assert child.headers["content-type"] == MANIFEST_V2
    finally:
        r4.stop()


async def test_manifest_digest_via_blob_endpoint_serves_manifest_media_type(primary, replica, fake_upstream):
    """Docker fetches manifest digests through the blob path; serving those as
    octet-stream breaks containerd unpack."""
    img = fake_upstream.install_image()
    async with httpx.AsyncClient(timeout=30) as c:
        assert (await c.get(f"{replica.url}/v2/fake/myimg/manifests/multi")).status_code == 200
        r = await c.get(f"{replica.url}/v2/fake/myimg/blobs/{img['list']}")
        assert r.status_code == 200
        assert r.headers["content-type"] == MANIFEST_LIST
        assert r.content == fake_upstream.manifests[("myimg", "multi")][1]


async def test_replica_ingest_digest_mismatch_is_400_and_cleans_up(replica):
    """Regression: the mismatch path used to `raise` a JSONResponse, which
    escaped as a TypeError (500) and leaked the temp file."""
    tmp_dir = replica.app_obj.store.tmp_dir
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{replica.url}/internal/ingest/blob/sha256:{'a' * 64}",
            content=b"not the right bytes",
            headers={"X-ContainerProxy-Secret": "test-secret"},
        )
    assert r.status_code == 404, r.text
    assert list(tmp_dir.iterdir()) == []
