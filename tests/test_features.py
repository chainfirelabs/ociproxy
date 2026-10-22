"""Blob fetch sharing, tags/list, pagination, metrics, input validation, CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest

from ociproxy.cli import JsonFormatter, main
from ociproxy.replication import ReplicaTail
from ociproxy.resolver import ResolveError
from ociproxy.store import ChangeEvent, Store

from .conftest import SECRET, Node, make_config, wait_until

INTERNAL = {"X-OCIProxy-Secret": SECRET}


# -- blob fetches -------------------------------------------------------------

async def test_concurrent_blob_gets_share_one_upstream_download(primary, fake_upstream):
    body = b"y" * 4096
    digest = fake_upstream.add_blob(body)
    url = f"{primary.url}/v2/fake/slowimg/blobs/{digest}"
    async with httpx.AsyncClient(timeout=30) as c:
        results = await asyncio.gather(*[c.get(url) for _ in range(4)])
    assert all(r.status_code == 200 and r.content == body for r in results)
    assert fake_upstream.hits[f"blob:{digest}"] == 1


async def test_blob_is_cached_even_if_client_disconnects(primary, fake_upstream):
    body = b"z" * 4096
    digest = fake_upstream.add_blob(body)
    async with httpx.AsyncClient(timeout=30) as c:
        async with c.stream("GET", f"{primary.url}/v2/fake/slowimg/blobs/{digest}") as r:
            await anext(r.aiter_bytes(1024))  # take one chunk, then hang up
    await wait_until(lambda: primary.app_obj.store.has_blob(digest), msg="background completion")
    assert fake_upstream.hits[f"blob:{digest}"] == 1


async def test_blob_fetch_asks_for_identity_encoding(primary, fake_upstream):
    img = fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        await c.get(f"{primary.url}/v2/fake/myimg/blobs/{img['layer1']}")
    assert fake_upstream.last_blob_accept_encoding == "identity"


# -- replication ---------------------------------------------------------------

async def test_changelog_long_poll_wakes_on_new_event(primary):
    primary.config.replication_peers["poll"] = {"token": "poll-token"}
    headers = {"Authorization": "Bearer poll-token", "X-OCIProxy-Peer": "poll"}
    async with httpx.AsyncClient(base_url=primary.url, timeout=30) as c:
        since = (await c.get("/replication/v2/changelog", params={"wait": 0}, headers=headers)).json()["current"]
        t0 = time.monotonic()
        poll = asyncio.create_task(c.get("/replication/v2/changelog", params={"since": since, "wait": 20}, headers=headers))
        await asyncio.sleep(0.3)
        primary.app_obj.store.put_manifest("fake", "wake", "latest", "sha256:"+"a"*64, "application/json", b'{}')
        r = await poll
    assert r.json()["events"], r.text
    assert time.monotonic() - t0 < 3


async def test_replica_resumes_from_persisted_serial(tmp_path, primary, fake_upstream, upstream_config):
    fake_upstream.install_image()
    cfg = make_config(tmp_path, "replica", name="persist", upstreams=[upstream_config],
                      primary_url=primary.url)
    cfg.advertised_url = f"http://127.0.0.1:{cfg.port}"
    r1 = Node(cfg).start()
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            assert (await c.get(f"{r1.url}/v2/fake/myimg/manifests/v1")).status_code == 200
        current = primary.app_obj.store.current_serial()
        await wait_until(lambda: r1.app_obj.tail.serial >= current, msg="tail caught up")
        # The primary's ack comes from the replica's own `since`.
        await wait_until(
            lambda: any(a >= current for n, _u, a, _p in primary.app_obj.store.live_replicas()
                        if n == r1.config.replication_id),
            msg="primary recorded the replica's ack",
        )
    finally:
        r1.stop()
    r2 = Node(cfg).start()  # same storage
    try:
        assert r2.app_obj.tail.serial >= current
    finally:
        r2.stop()


async def test_tail_skips_blobs_gone_from_primary(tmp_path):
    async def gone(_digest):
        raise ResolveError(404, "garbage-collected")

    tail = ReplicaTail(Store(str(tmp_path)), client=None, node_id="n", advertised_url="http://x",
                       fetch_blob=gone)
    await tail.apply_event(ChangeEvent(serial=1, kind="blob", digest="sha256:" + "c" * 64))


async def test_tail_skips_blobs_the_replica_already_has(primary, replica, fake_upstream):
    img = fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
        assert (await c.get(f"{replica.url}/v2/fake/myimg/blobs/{img['layer1']}")).status_code == 200
    hits = fake_upstream.hits[f"blob:{img['layer1']}"]
    await replica.app_obj.tail.apply_event(ChangeEvent(serial=1, kind="blob", digest=img["layer1"]))
    assert fake_upstream.hits[f"blob:{img['layer1']}"] == hits


# -- registry surface ------------------------------------------------------------

async def test_tags_list_with_pagination(primary, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        for ref in ("v1", "multi"):
            assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/{ref}")).status_code == 200
        r = await c.get(f"{primary.url}/v2/fake/myimg/tags/list")
        assert r.json() == {"name": "fake/myimg", "tags": ["multi", "v1"]}
        r = await c.get(f"{primary.url}/v2/fake/myimg/tags/list", params={"n": 1})
        assert r.json()["tags"] == ["multi"]
        assert r.headers["link"] == '</v2/fake/myimg/tags/list?n=1&last=multi>; rel="next"'
        r = await c.get(f"{primary.url}/v2/fake/myimg/tags/list", params={"n": 1, "last": "multi"})
        assert r.json()["tags"] == ["v1"]
        r = await c.get(f"{primary.url}/v2/fake/nope/tags/list")
        assert r.status_code == 404


async def test_catalog_link_pagination(primary, fake_upstream):
    fake_upstream.install_image(repo="a")
    fake_upstream.install_image(repo="b")
    async with httpx.AsyncClient() as c:
        for repo in ("a", "b"):
            await c.get(f"{primary.url}/v2/fake/{repo}/manifests/v1")
        r = await c.get(f"{primary.url}/v2/_catalog", params={"n": 1})
        assert r.json()["repositories"] == ["a"]
        assert r.headers["link"] == '</v2/_catalog?n=1&last=a>; rel="next"'


@pytest.mark.parametrize("path,params", [
    ("/v2/_catalog", {"n": "abc"}),
    ("/v2/_catalog", {"n": "-1"}),
    ("/replication/v2/changelog", {"since": "x"}),
    ("/internal/images", {"limit": "-5"}),
])
async def test_bad_query_params_are_400(primary, path, params):
    primary.config.replication_peers["query"] = {"token":"query-token"}
    headers = {"Authorization":"Bearer query-token", "X-OCIProxy-Peer":"query"} if path.startswith("/replication/") else INTERNAL
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{primary.url}{path}", params=params, headers=headers)
    assert r.status_code == 400, r.text


async def test_bad_internal_bodies_are_400(primary, replica):
    headers = replica.app_obj.primary_client._headers
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{primary.url}/replication/v2/register", content=b"{nope", headers=headers)
        assert r.status_code == 400
        r = await c.post(f"{primary.url}/replication/v2/register", headers=headers,
                         json={"protocol":"public-v2", "node_id":"n", "base_url":"file:///etc"})
        assert r.status_code == 400
        assert (await c.post(f"{replica.url}/internal/ingest/manifest", headers=INTERNAL, json={})).status_code == 404


async def test_sha512_blob_digest_is_400(primary):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{primary.url}/v2/fake/x/blobs/sha512:{'a' * 128}")
    assert r.status_code == 400


async def test_metrics_require_secret_and_report(primary, fake_upstream):
    img = fake_upstream.install_image()
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{primary.url}/metrics")).status_code == 403
        await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")
        await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")
        await c.get(f"{primary.url}/v2/fake/myimg/blobs/{img['layer1']}")
        r = await c.get(f"{primary.url}/metrics", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200
    text = r.text
    assert 'ociproxy_manifest_requests_total{result="hit"} 1' in text
    assert 'ociproxy_manifest_requests_total{result="miss"} 1' in text
    assert 'ociproxy_blob_requests_total{result="miss"} 1' in text
    assert "ociproxy_blobs 1" in text


async def test_maintenance_runs_gc_and_compaction(primary):
    primary.app_obj.store.put_manifest("fake", "r", "t", "sha256:" + "1" * 64, "m", b"{}")
    primary.app_obj.store.put_manifest("fake", "r", "t", "sha256:" + "2" * 64, "m", b"{}")
    fut = asyncio.run_coroutine_threadsafe(
        primary.app_obj.run_maintenance(), primary.server_loop()
    )
    await asyncio.wrap_future(fut)
    assert len([e for e in primary.app_obj.store.changelog_since(0) if e.kind == "manifest"]) == 1
    assert primary.app_obj.metrics.value("ociproxy_gc_runs_total") == 1


# -- CLI / logging ---------------------------------------------------------------

def test_cli_role_override_is_validated(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("OCIPROXY_SECRET", "s")
    monkeypatch.setenv("OCIPROXY_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("OCIPROXY_PRIMARY_URL", "http://primary:5000")
    monkeypatch.setenv("OCIPROXY_ROLE", "replica")
    assert main(["serve", "--role", "primary"]) == 2
    assert "must not set a primary_url" in capsys.readouterr().err


def test_cli_missing_secret_is_a_clean_error(monkeypatch, capsys, tmp_path):
    for var in ("OCIPROXY_SECRET", "OCIPROXY_PRIMARY_URL", "OCIPROXY_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OCIPROXY_ROLE", "primary")
    monkeypatch.setenv("OCIPROXY_STORAGE_PATH", str(tmp_path))
    assert main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "secret is required" in err and "Traceback" not in err


def test_json_log_formatter():
    record = logging.LogRecord("ociproxy.x", logging.INFO, __file__, 1, "hello %s", ("w",), None)
    entry = json.loads(JsonFormatter().format(record))
    assert entry["msg"] == "hello w" and entry["level"] == "info" and entry["logger"] == "ociproxy.x"


async def test_non_ascii_credentials_are_rejected_not_500(primary):
    """compare_digest rejects non-ASCII str; comparisons must be on bytes."""
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{primary.url}/internal/overview",
                        headers={"X-OCIProxy-Secret": "s\u00e9cret".encode()})
        assert r.status_code == 403
        r = await c.put(f"{primary.url}/v2/app/manifests/v1", content=b"{}",
                        headers={"Authorization": "Basic " + __import__("base64").b64encode(
                            "ci:p\u00e4ss".encode()).decode()})
        assert r.status_code == 401
