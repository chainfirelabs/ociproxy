"""Pull-through cascade: replica -> primary -> upstream, with caching + dedup."""

from __future__ import annotations

import asyncio

import httpx

from .conftest import wait_until


async def test_ping_and_headers(primary):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{primary.url}/v2/")
        assert r.status_code == 200
        assert r.headers["docker-distribution-api-version"] == "registry/2.0"


async def test_replica_pull_cascades_to_upstream(primary, replica, fake_upstream):
    img = fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
        assert r.status_code == 200, r.text
        assert r.headers["docker-content-digest"] == img["manifest"]
        assert "manifest.v2+json" in r.headers["content-type"]

        # blobs served through the replica too
        for d in (img["config"], img["layer1"], img["layer2"]):
            b = await c.get(f"{replica.url}/v2/fake/myimg/blobs/{d}")
            assert b.status_code == 200
            assert "sha256:" + __import__("hashlib").sha256(b.content).hexdigest() == d

    # upstream was hit exactly once per object
    assert fake_upstream.hits["manifest:myimg:v1"] == 1
    assert fake_upstream.hits[f"blob:{img['config']}"] == 1
    assert fake_upstream.hits[f"blob:{img['layer1']}"] == 1
    assert fake_upstream.hits[f"blob:{img['layer2']}"] == 1
    # token challenge was exercised
    assert fake_upstream.hits["token"] >= 1


async def test_second_pull_is_local(primary, replica, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
        await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
    assert fake_upstream.hits["manifest:myimg:v1"] == 1


async def test_concurrent_pulls_dedup_upstream(primary, replica, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient(timeout=30) as c:
        results = await asyncio.gather(*[
            c.get(f"{replica.url}/v2/fake/myimg/manifests/v1") for _ in range(5)
        ])
    assert all(r.status_code == 200 for r in results)
    assert fake_upstream.hits["manifest:myimg:v1"] == 1


async def test_manifest_list_recursion(primary, replica, fake_upstream):
    img = fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/multi")
        assert r.status_code == 200
        assert r.headers["docker-content-digest"] == img["list"]
        # child manifest and all blobs now resolvable from the replica directly
        child = await c.get(f"{replica.url}/v2/fake/myimg/manifests/{img['manifest']}")
        assert child.status_code == 200
        assert child.headers["docker-content-digest"] == img["manifest"]
    # child manifest fetched from upstream once during list resolution
    assert fake_upstream.hits[f"manifest:myimg:{img['manifest']}"] == 1


async def test_unknown_upstream_404(replica):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{replica.url}/v2/nosuchrepo/img/manifests/v1")
        assert r.status_code == 404


async def test_missing_manifest_404(primary, replica, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/nope")
        assert r.status_code == 404


async def test_bad_digest_400(replica):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{replica.url}/v2/fake/myimg/blobs/notadigest")
        assert r.status_code == 400


async def test_head_manifest(primary, replica, fake_upstream):
    img = fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        r = await c.head(f"{replica.url}/v2/fake/myimg/manifests/v1")
        assert r.status_code == 200
        assert r.headers["docker-content-digest"] == img["manifest"]


async def test_head_missing_blob_404_not_500(primary):
    """docker push starts with a HEAD blob existence check; a miss must be 404."""
    async with httpx.AsyncClient() as c:
        r = await c.head(f"{primary.url}/v2/somerepo/blobs/sha256:{'a' * 64}")
        assert r.status_code == 404


async def test_blob_get_streams_before_full_download(primary, fake_upstream):
    """Regression: a blob GET must start flowing immediately (tee'd into
    the store) instead of after the whole upstream download completes. The
    old eager path made client daemons time out on multi-GB images."""
    import time

    body = b"x" * 4096  # drip-fed 4 x 1024 @ 0.25s = ~1s total
    digest = fake_upstream.add_blob(body)
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=30) as c:
        async with c.stream("GET", f"{primary.url}/v2/fake/slowimg/blobs/{digest}") as r:
            assert r.status_code == 200
            it = r.aiter_bytes(1024)
            first = await anext(it)
            ttfb = time.monotonic() - t0
            assert first == body[:1024]
            async for _ in it:
                pass
    total = time.monotonic() - t0
    assert total >= 0.9, "slow upstream stream should still take ~1s"
    assert ttfb < 0.5, f"first byte took {ttfb:.2f}s; response must stream, not buffer"
    await wait_until(lambda: primary.app_obj.store.has_blob(digest),
                    msg="streamed blob committed to store")


async def test_referrers_tag_probe_is_404_while_primary_down(primary, replica, fake_upstream):
    """Docker probes the referrers fallback tag ("sha256-<hex>") on every pull
    and fails on anything but 404. A replica whose primary is down must
    still answer 404 so pulls of cached images keep working."""
    img = fake_upstream.install_image()
    probe = "sha256-" + img["manifest"].split(":", 1)[1]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
        assert r.status_code == 200
        primary.stop()

        r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/{probe}")
        assert r.status_code == 404, r.text
        # An ordinary tag that is not cached still reports the outage.
        r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v2")
        assert r.status_code == 502, r.text
        # The cached image is still served.
        r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/{img['manifest']}")
        assert r.status_code == 200
