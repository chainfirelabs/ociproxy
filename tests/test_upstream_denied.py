"""Anonymous sources: an upstream that still refuses access after the
anonymous token dance (Docker Hub does this for repositories that do not
exist) is reported as 404, not 401. A 401 would tell the client to log in
to this node, and registry-mirror clients only fall back on 404."""

from __future__ import annotations

from dataclasses import replace

import httpx

from ociproxy.config import UpstreamConfig
from .conftest import Node, make_config


def _hub(fake_upstream, **kw) -> UpstreamConfig:
    return UpstreamConfig(name="hub", base_url=f"http://127.0.0.1:{fake_upstream.port}",
                          prefix="hub", official_namespace="library",
                          visibility="public", sharing="public", **kw)


async def test_missing_repo_on_docker_hub_style_upstream_is_404(tmp_path, fake_upstream):
    fake_upstream.deny_unknown_repos = True
    node = Node(make_config(tmp_path, "primary", name="deny-404", upstreams=[_hub(fake_upstream)])).start()
    ns = {"ns": f"127.0.0.1:{fake_upstream.port}"}
    try:
        img = fake_upstream.install_image(repo="library/nginx", tag="latest")
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            # registry-mirror request (docker pull ansible) and prefixed pull
            for r in (await c.head("/v2/library/ansible/manifests/latest", params=ns),
                      await c.get("/v2/library/ansible/manifests/latest", params=ns),
                      await c.get("/v2/hub/ansible/manifests/latest")):
                assert r.status_code == 404, (r.request.url, r.status_code, r.text)
                assert "www-authenticate" not in r.headers
            r = await c.get("/v2/hub/ansible/manifests/latest")
            assert r.json()["errors"][0]["code"] == "MANIFEST_UNKNOWN"
            assert "not found on hub" in r.json()["errors"][0]["message"]
            assert (await c.get("/v2/library/ansible/tags/list", params=ns)).status_code == 404
            # existing images are unaffected
            r = await c.get("/v2/library/nginx/manifests/latest", params=ns)
            assert r.status_code == 200 and r.headers["docker-content-digest"] == img["manifest"]
    finally:
        node.stop()


async def test_refused_anonymous_token_is_404(tmp_path, fake_upstream):
    fake_upstream.token_status = 401
    node = Node(make_config(tmp_path, "primary", name="deny-token", upstreams=[_hub(fake_upstream)])).start()
    try:
        fake_upstream.install_image(repo="team/app", tag="v1")
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            assert (await c.get("/v2/hub/team/app/manifests/v1")).status_code == 404
    finally:
        node.stop()


async def test_source_with_credentials_keeps_upstream_status(tmp_path, fake_upstream):
    """With credentials a 401 may mean wrong credentials: not masked as 404."""
    fake_upstream.deny_unknown_repos = True
    hub = replace(_hub(fake_upstream), username="robot", password="pw", sharing="local")
    node = Node(make_config(tmp_path, "primary", name="deny-creds", upstreams=[hub])).start()
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            assert (await c.get("/v2/hub/ansible/manifests/latest")).status_code == 401
    finally:
        node.stop()
