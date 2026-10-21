"""Push flow: docker-push-style upload sessions on the primary."""

from __future__ import annotations

import hashlib
import json

import httpx

from .conftest import make_replica, wait_until

AUTH = ("ci", "cipass")
MANIFEST_V2 = "application/vnd.docker.distribution.manifest.v2+json"


async def push_blob(c: httpx.AsyncClient, repo: str, data: bytes, auth=AUTH) -> httpx.Response:
    r = await c.post(f"/v2/{repo}/blobs/uploads/", auth=auth)
    assert r.status_code == 202, r.text
    loc = r.headers["location"]
    r = await c.patch(loc, content=data, auth=auth)
    assert r.status_code == 202, r.text
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    r = await c.put(f"{loc}?digest={digest}", auth=auth)
    return r


async def test_push_blob_and_manifest_then_pull(primary, replica):
    layer = b"pushed-layer-" * 1000
    config = json.dumps({"architecture": "amd64", "os": "linux"}).encode()
    async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
        r = await push_blob(c, "myapp", layer)
        assert r.status_code == 201
        ld = "sha256:" + hashlib.sha256(layer).hexdigest()
        r = await push_blob(c, "myapp", config)
        assert r.status_code == 201
        cd = "sha256:" + hashlib.sha256(config).hexdigest()

        manifest = {
            "schemaVersion": 2, "mediaType": MANIFEST_V2,
            "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                       "digest": cd, "size": len(config)},
            "layers": [{"mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                        "digest": ld, "size": len(layer)}],
        }
        mb = json.dumps(manifest).encode()
        md = "sha256:" + hashlib.sha256(mb).hexdigest()
        r = await c.put("/v2/myapp/manifests/latest", content=mb,
                        headers={"content-type": MANIFEST_V2}, auth=AUTH)
        assert r.status_code == 201
        assert r.headers["docker-content-digest"] == md

        # pull it back from the primary
        r = await c.get("/v2/myapp/manifests/latest")
        assert r.status_code == 200
        assert r.content == mb

    # Local pushes are not implicitly published to the shared source catalog.
    async with httpx.AsyncClient(base_url=replica.url, timeout=30) as c:
        assert (await c.get("/v2/myapp/manifests/latest")).status_code == 404
        assert not replica.app_obj.store.has_blob(ld)


async def test_push_requires_auth(primary):
    async with httpx.AsyncClient(base_url=primary.url) as c:
        r = await c.post("/v2/myapp/blobs/uploads/")
        assert r.status_code == 401
        r = await c.put("/v2/myapp/manifests/latest", content=b"{}", auth=("ci", "wrong"))
        assert r.status_code == 401


async def test_push_reserved_prefix_denied(primary):
    async with httpx.AsyncClient(base_url=primary.url) as c:
        r = await c.post("/v2/fake/myapp/blobs/uploads/", auth=AUTH)
        assert r.status_code == 400


async def test_push_digest_mismatch_rejected(primary):
    async with httpx.AsyncClient(base_url=primary.url) as c:
        r = await c.post("/v2/myapp/blobs/uploads/", auth=AUTH)
        loc = r.headers["location"]
        await c.patch(loc, content=b"real-bytes", auth=AUTH)
        r = await c.put(f"{loc}?digest=sha256:{'b' * 64}", auth=AUTH)
        assert r.status_code == 400


async def test_push_invalid_manifest_json_rejected(primary):
    async with httpx.AsyncClient(base_url=primary.url) as c:
        r = await c.put("/v2/myapp/manifests/latest", content=b"not json", auth=AUTH)
        assert r.status_code == 400


async def test_catalog_lists_pushed_repos(primary):
    async with httpx.AsyncClient(base_url=primary.url) as c:
        await push_blob(c, "catalogapp", b"x" * 10)
        mb = json.dumps({"schemaVersion": 2, "mediaType": MANIFEST_V2,
                        "config": {"digest": f"sha256:{'c' * 64}", "size": 1},
                        "layers": []}).encode()
        await c.put("/v2/catalogapp/manifests/latest", content=mb, auth=AUTH)
        r = await c.get("/v2/_catalog")
        assert r.status_code == 200
        assert "catalogapp" in r.json()["repositories"]


async def test_pushed_image_remains_local(primary, fake_upstream, upstream_config, tmp_path):
    r2 = make_replica(tmp_path, primary, "push-r2", [upstream_config])
    try:
        await wait_until(lambda: len(primary.app_obj.store.live_replicas()) == 1,
                        msg="replica registered")
        layer = b"fanout-pushed-layer" * 500
        async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
            await push_blob(c, "fanapp", layer)
            ld = "sha256:" + hashlib.sha256(layer).hexdigest()
            mb = json.dumps({
                "schemaVersion": 2, "mediaType": MANIFEST_V2,
                "config": {"digest": ld, "size": len(layer)}, "layers": [],
            }).encode()
            await c.put("/v2/fanapp/manifests/latest", content=mb, auth=AUTH)
        await wait_until(lambda: r2.app_obj.tail.serial >= primary.app_obj.store.current_serial())
        assert not r2.app_obj.store.has_blob(ld)
        assert not r2.app_obj.store.manifest_exists("local", "fanapp", "latest")
    finally:
        r2.stop()
