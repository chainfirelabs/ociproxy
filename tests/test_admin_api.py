"""Admin surface: overview, image listing, the upstream listing.

/internal/upstreams is read-only on every node. Upstreams added in the web
UI go through /internal/sources (see test_sources.py).
"""

from __future__ import annotations

import json
import time

import httpx

from .conftest import SECRET

H = {"X-ContainerProxy-Secret": SECRET}


async def test_overview_shape(primary):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{primary.url}/internal/overview", headers=H)
        assert r.status_code == 200
        d = r.json()
        assert d["role"] == "primary"
        assert d["node_id"]
        assert [u["prefix"] for u in d["upstreams"]] == ["fake"]
        assert d["blobs"] == {"count": 0, "total_size": 0}
        assert d["manifests"] == 0
        # overview must never carry credentials anywhere
        assert "password" not in json.dumps(d)


async def test_internal_endpoints_require_secret(primary):
    async with httpx.AsyncClient() as c:
        for path in ("/internal/overview", "/internal/images", "/internal/upstreams"):
            assert (await c.get(f"{primary.url}{path}")).status_code == 403
            assert (
                await c.get(f"{primary.url}{path}", headers={"X-ContainerProxy-Secret": "nope"})
            ).status_code == 403


async def test_images_listing_after_pull(primary, fake_upstream):
    img = fake_upstream.install_image(repo="listed", tag="v1")
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{primary.url}/v2/fake/listed/manifests/v1")).status_code == 200
        blob = await c.get(f"{primary.url}/v2/fake/listed/blobs/{img['config']}")
        assert blob.status_code == 200

        r = await c.get(f"{primary.url}/internal/images", headers=H)
        assert r.status_code == 200
        d = r.json()
        item = next(i for i in d["items"] if i["repo"] == "listed" and i["reference"] == "v1")
        assert item["upstream"] == "fake"
        assert item["digest"] == img["manifest"]
        assert item["size"] > 10
        assert item["stored_at"] > 0

        ov = (await c.get(f"{primary.url}/internal/overview", headers=H)).json()
        assert ov["manifests"] >= 1
        assert ov["blobs"]["count"] >= 1
        assert ov["blobs"]["total_size"] >= len(blob.content)


async def test_images_pagination(primary, fake_upstream):
    for i in range(4):
        fake_upstream.install_image(repo=f"repo{i}", tag="v1")
    async with httpx.AsyncClient() as c:
        for i in range(4):
            assert (
                await c.get(f"{primary.url}/v2/fake/repo{i}/manifests/v1")
            ).status_code == 200
        r = await c.get(f"{primary.url}/internal/images?limit=2&offset=0", headers=H)
        d = r.json()
        assert d["returned"] == 2
        assert d["total"] == 4
        page2 = (await c.get(f"{primary.url}/internal/images?limit=2&offset=2", headers=H)).json()
        assert {i["repo"] for i in d["items"]}.isdisjoint({i["repo"] for i in page2["items"]})


async def test_upstreams_get_is_sanitized(primary):
    # Seed a credentialed upstream directly in config; the GET must not leak it.
    from containerproxy.config import UpstreamConfig

    registry = primary.app_obj.sources
    registry.static_upstreams.append(
        UpstreamConfig(
            name="cred",
            base_url="https://example.invalid",
            prefix="cred",
            username="u1",
            password="s3cret",
        )
    )
    registry.reload()
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{primary.url}/internal/upstreams", headers=H)
        cred = next(i for i in r.json()["items"] if i["name"] == "cred")
        assert cred["has_credentials"] is True
        assert "password" not in cred and "username" not in cred
        assert "s3cret" not in r.text


async def test_upstreams_are_read_only(primary, replica):
    """No mutation surface on any node: POST is rejected, DELETE unrouted."""
    async with httpx.AsyncClient() as c:
        for node in (primary, replica):
            r = await c.post(
                f"{node.url}/internal/upstreams",
                headers=H,
                json={"name": "x", "base_url": "https://ok.invalid"},
            )
            assert r.status_code == 405, f"{node.url} accepted a POST"
            # the delete route does not exist at all
            r = await c.delete(f"{node.url}/internal/upstreams/fake", headers=H)
            assert r.status_code == 404, f"{node.url} routes DELETE"
        # untouched
        assert [u.prefix for u in primary.app_obj.config.upstreams] == ["fake"]


async def test_no_upstreams_json_written(primary, fake_upstream):
    """Nothing in the pull path may create the old runtime state file."""
    fake_upstream.install_image(repo="plain", tag="v1")
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{primary.url}/v2/fake/plain/manifests/v1")).status_code == 200
    from pathlib import Path

    assert not Path(primary.config.storage_path, "upstreams.json").exists()


async def test_primary_records_replica_pulls(primary, replica, fake_upstream):
    node_id = replica.config.replication_id
    # Registered (live) but has pulled nothing yet.
    async with httpx.AsyncClient() as c:
        ov = (await c.get(f"{primary.url}/internal/overview", headers=H)).json()
        rep = next(r for r in ov["replicas"] if r["node_id"] == node_id)
        assert rep["last_pull_at"] == 0

    # A pull through the replica cascades to the primary's internal blob
    # endpoint, which must attribute it to the replica's node id.
    img = fake_upstream.install_image(repo="tracked", tag="t1")
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{replica.url}/v2/fake/tracked/manifests/t1")).status_code == 200
        assert (
            await c.get(f"{replica.url}/v2/fake/tracked/blobs/{img['config']}")
        ).status_code == 200

    async with httpx.AsyncClient() as c:
        ov = (await c.get(f"{primary.url}/internal/overview", headers=H)).json()
        rep = next(r for r in ov["replicas"] if r["node_id"] == node_id)
        assert rep["last_pull_at"] > 0
        assert time.time() - rep["last_pull_at"] < 30


async def test_pull_attribution_ignores_unknown_node(primary):
    # A pull claiming a node id that never registered must not crash nor
    # fabricate a replica row.
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{primary.url}/internal/pull/blob/sha256:" + "a" * 64,
            headers={**H, "X-ContainerProxy-Node": "ghost-node"},
        )
        assert r.status_code == 404  # blob absent; attribution was a no-op
        ov = (await c.get(f"{primary.url}/internal/overview", headers=H)).json()
        assert all(r["node_id"] != "ghost-node" for r in ov["replicas"])
