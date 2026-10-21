"""Proxy behavior: upstream fetches honor HTTP(S)_PROXY / CONTAINERPROXY_UPSTREAM_PROXY;
internal node-to-node replication traffic never does."""

from __future__ import annotations

import threading
import time
from collections import Counter
from urllib.parse import urlparse

import httpx
import uvicorn


from .conftest import Node, make_config, make_replica, wait_until

HOP_BY_HOP = {
    "connection", "proxy-connection", "keep-alive", "te", "trailer",
    "transfer-encoding", "upgrade",
}

class FakeProxy:
    """Minimal plain-HTTP forward proxy (absolute-URI forwarding).

    httpx sends absolute-form requests (GET http://host/path) when a proxy is
    configured for an http:// target; this forwards them and counts hits per
    target host. HTTPS would use CONNECT, which httpx handles against real
    proxies — the fake upstream here is plain HTTP by design.

    Pure ASGI: the absolute-form target lives in the raw request target
    (scope["raw_path"]) and has no leading "/", so Starlette routing cannot
    match it.
    """

    def __init__(self):
        self.hits: Counter[str] = Counter()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        target = (scope.get("raw_path") or scope["path"].encode("latin-1")).decode("latin-1")
        qs = scope.get("query_string", b"").decode("latin-1")
        if qs:
            target = f"{target}?{qs}"
        self.hits[urlparse(target).netloc] += 1
        headers = {k.decode("latin-1"): v.decode("latin-1")
                   for k, v in scope["headers"] if k.lower() not in HOP_BY_HOP}
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as c:
            r = await c.request(scope["method"], target, content=body, headers=headers)
        resp_headers = [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in r.headers.items()
            if k.lower() not in HOP_BY_HOP | {"content-encoding", "content-length"}
        ]
        await send({"type": "http.response.start", "status": r.status_code,
                   "headers": resp_headers})
        await send({"type": "http.response.body", "body": r.content})

    def start(self) -> None:
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        cfg = uvicorn.Config(self, host="127.0.0.1", port=self.port,
                            log_level="warning", access_log=False)
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 15
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("fake proxy did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
            self._thread.join(timeout=15)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


async def test_primary_fetches_upstream_through_proxy(tmp_path, fake_upstream, upstream_config):
    proxy = FakeProxy()
    proxy.start()
    try:
        img = fake_upstream.install_image()
        primary = Node(make_config(
            tmp_path, "primary", name="px-p",
            upstreams=[upstream_config], upstream_proxy=proxy.url,
        )).start()
        replica = make_replica(tmp_path, primary, "px-r1", [upstream_config])
        try:
            await wait_until(lambda: len(primary.app_obj.store.live_replicas()) == 1,
                           msg="replica registered")
            async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
                r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
                assert r.status_code == 200, r.text
                assert r.headers["docker-content-digest"] == img["manifest"]
                b = await c.get(f"{replica.url}/v2/fake/myimg/blobs/{img['layer1']}")
                assert b.status_code == 200

            upstream_host = f"127.0.0.1:{fake_upstream.port}"
            # every upstream object went through the proxy...
            assert proxy.hits[upstream_host] >= 2  # manifest + blobs
            # ...and actually arrived at the upstream via the proxy
            assert fake_upstream.hits["manifest:myimg:v1"] == 1
            assert fake_upstream.hits[f"blob:{img['layer1']}"] == 1
            # internal replication traffic NEVER touched the proxy
            internal_hosts = {f"127.0.0.1:{primary.config.port}",
                            f"127.0.0.1:{replica.config.port}"}
            assert not (set(proxy.hits) & internal_hosts), dict(proxy.hits)
        finally:
            replica.stop()
            primary.stop()
    finally:
        proxy.stop()


async def test_replica_pulls_through_primary_ignoring_dead_env_proxy(
    primary, fake_upstream, upstream_config, tmp_path, monkeypatch
):
    """A replica with a broken HTTP(S)_PROXY in its env still serves pulls,
    because replica->primary traffic uses the non-proxied internal client."""
    img = fake_upstream.install_image()
    async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
        assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")).status_code == 200

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    r = make_replica(tmp_path, primary, "deadpx-r", [upstream_config])
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            resp = await c.get(f"{r.url}/v2/fake/myimg/manifests/v1")
            assert resp.status_code == 200
            assert resp.headers["docker-content-digest"] == img["manifest"]
            b = await c.get(f"{r.url}/v2/fake/myimg/blobs/{img['layer2']}")
            assert b.status_code == 200
    finally:
        r.stop()

def test_client_split_trust_env(tmp_path):
    plain = Node(make_config(tmp_path, "primary", name="split1"))
    assert plain.app_obj.upstream_http._trust_env is True
    assert plain.app_obj.internal_http._trust_env is False
    assert not plain.app_obj.upstream_http._mounts  # no explicit proxy mounted

    proxied = Node(make_config(tmp_path, "primary", name="split2",
                             upstream_proxy="http://127.0.0.1:3128"))
    assert proxied.app_obj.upstream_http._trust_env is True
    assert proxied.app_obj.internal_http._trust_env is False
    # the explicit proxy is mounted as a catch-all transport on the upstream client
    assert len(proxied.app_obj.upstream_http._mounts) == 1
    # ...and never on the internal client
    assert not proxied.app_obj.internal_http._mounts
