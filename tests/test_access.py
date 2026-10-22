"""Node access mode (public/private pulls) and official-namespace normalization."""

from __future__ import annotations

import httpx
from dataclasses import replace
import pytest

from ociproxy.config import UpstreamConfig, load_config
from .conftest import Node, make_config


# -- pull auth: public vs private --------------------------------------------

async def test_public_pull_is_anonymous(primary, replica, fake_upstream):
    fake_upstream.install_image()
    async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
        assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")).status_code == 200
        assert (await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")).status_code == 200


async def test_private_pull_requires_auth(tmp_path, fake_upstream, upstream_config):
    upstream_config = replace(upstream_config, sharing="local", visibility="private")
    primary = Node(make_config(tmp_path, "primary", name="priv-p",
                              upstreams=[upstream_config],
                              push_username="ci", push_password="cipass",
                              pull_auth="basic")).start()
    try:
        img = fake_upstream.install_image()
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            # anonymous manifest pull -> 401 with a Basic challenge
            r = await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")
            assert r.status_code == 401
            assert "Basic" in r.headers.get("www-authenticate", "")
            # ping stays open (healthchecks, `docker login` handshake)
            assert (await c.get(f"{primary.url}/v2/")).status_code == 200
            # with credentials -> 200
            r = await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1",
                           auth=("ci", "cipass"))
            assert r.status_code == 200
            assert r.headers["docker-content-digest"] == img["manifest"]
            # blobs are gated too
            assert (await c.get(f"{primary.url}/v2/fake/myimg/blobs/{img['layer1']}",
                               auth=("ci", "cipass"))).status_code == 200
            assert (await c.get(f"{primary.url}/v2/fake/myimg/blobs/{img['layer1']}")).status_code == 401
            # wrong password -> 401
            assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1",
                               auth=("ci", "nope"))).status_code == 401
    finally:
        primary.stop()


async def test_private_replica_gates_its_own_pulls(tmp_path, fake_upstream, upstream_config):
    """A private replica serves authenticated pulls from content replicated
    from a public primary; anonymous pulls are rejected at the replica."""
    img = fake_upstream.install_image()
    primary = Node(make_config(tmp_path, "primary", name="pa-p",
                              upstreams=[upstream_config])).start()
    async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
        assert (await c.get(f"{primary.url}/v2/fake/myimg/manifests/v1")).status_code == 200
    rep = Node(make_config(tmp_path, "replica", name="pa-r",
                          upstreams=[replace(upstream_config, sharing="local", visibility="private")],
                          primary_url=primary.url,
                          push_username="ci", push_password="cipass",
                          pull_auth="basic")).start()
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            assert (await c.get(f"{rep.url}/v2/fake/myimg/manifests/v1")).status_code == 401
            r = await c.get(f"{rep.url}/v2/fake/myimg/manifests/v1", auth=("ci", "cipass"))
            assert r.status_code == 200
            assert r.headers["docker-content-digest"] == img["manifest"]
    finally:
        rep.stop()
        primary.stop()


def test_default_visibility_and_pull_auth_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("OCIPROXY_ROLE", "primary")
    monkeypatch.setenv("OCIPROXY_STORAGE_PATH", str(tmp_path / "s"))
    assert load_config(None).default_visibility == "public"  # default: public

    monkeypatch.setenv("OCIPROXY_PULL_AUTH", "bogus")
    with pytest.raises(ValueError, match="pull_auth"):
        load_config(None)

    # the older spelling still works; users may live in the database, so
    # a private default no longer needs push credentials
    monkeypatch.setenv("OCIPROXY_PULL_AUTH", "basic")
    assert load_config(None).default_visibility == "private"

    # the new setting wins over the old one
    monkeypatch.setenv("OCIPROXY_DEFAULT_VISIBILITY", "public")
    assert load_config(None).default_visibility == "public"
    monkeypatch.setenv("OCIPROXY_DEFAULT_VISIBILITY", "secret")
    with pytest.raises(ValueError, match="visibility"):
        load_config(None)


# -- official namespace ("library/") normalization ----------------------------

def _hub(fake_upstream, **kw) -> UpstreamConfig:
    return UpstreamConfig(name="hub", base_url=f"http://127.0.0.1:{fake_upstream.port}",
                         prefix="hub", official_namespace="library", visibility="public", sharing="public", **kw)


async def test_single_segment_repo_gets_official_namespace(tmp_path, fake_upstream):
    primary = Node(make_config(tmp_path, "primary", name="ns-p", upstreams=[_hub(fake_upstream)])).start()
    try:
        img = fake_upstream.install_image(repo="library/nginx", tag="latest")
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.get(f"{primary.url}/v2/hub/nginx/manifests/latest")
            assert r.status_code == 200, r.text
            assert r.headers["docker-content-digest"] == img["manifest"]
        # upstream saw the normalized name, not the bare one
        assert fake_upstream.hits["manifest:library/nginx:latest"] == 1
    finally:
        primary.stop()


async def test_short_and_long_form_share_one_cache_entry(tmp_path, fake_upstream):
    primary = Node(make_config(tmp_path, "primary", name="ns2-p", upstreams=[_hub(fake_upstream)])).start()
    try:
        fake_upstream.install_image(repo="library/nginx", tag="latest")
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r1 = await c.get(f"{primary.url}/v2/hub/nginx/manifests/latest")
            r2 = await c.get(f"{primary.url}/v2/hub/library/nginx/manifests/latest")
            assert r1.status_code == r2.status_code == 200
            assert r1.headers["docker-content-digest"] == r2.headers["docker-content-digest"]
        # two pull forms, one upstream fetch
        assert fake_upstream.hits["manifest:library/nginx:latest"] == 1
    finally:
        primary.stop()


async def test_multi_segment_repo_is_not_rewritten(tmp_path, fake_upstream):
    primary = Node(make_config(tmp_path, "primary", name="ns3-p", upstreams=[_hub(fake_upstream)])).start()
    try:
        fake_upstream.install_image(repo="team/app", tag="v2")
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.get(f"{primary.url}/v2/hub/team/app/manifests/v2")
            assert r.status_code == 200, r.text
        assert fake_upstream.hits["manifest:team/app:v2"] == 1
    finally:
        primary.stop()


async def test_default_upstream_fallback_normalizes(tmp_path, fake_upstream):
    """Unprefixed pull -> local miss -> default upstream -> normalized repo."""
    primary = Node(make_config(tmp_path, "primary", name="ns4-p",
                              upstreams=[_hub(fake_upstream, default=True)])).start()
    try:
        fake_upstream.install_image(repo="library/redis", tag="latest")
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.get(f"{primary.url}/v2/redis/manifests/latest")
            assert r.status_code == 200, r.text
        assert fake_upstream.hits["manifest:library/redis:latest"] == 1
    finally:
        primary.stop()


async def test_private_blob_cannot_be_read_via_public_path(tmp_path):
    import hashlib
    import json
    from ociproxy.config import AccessRule
    from .test_push import push_blob, MANIFEST_V2

    node = Node(make_config(tmp_path, 'primary', name='blob-isolation',
                           push_username='ci', push_password='cipass',
                           access_rules=[AccessRule(path='private/*', visibility='private')])).start()
    try:
        payload = b'private-layer-for-isolation-test'
        digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
        manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': digest}, 'layers': []}).encode()
        async with httpx.AsyncClient(base_url=node.url) as c:
            assert (await push_blob(c, 'private/app', payload)).status_code == 201
            assert (await c.put('/v2/private/app/manifests/latest', content=manifest,
                                headers={'content-type': MANIFEST_V2}, auth=('ci', 'cipass'))).status_code == 201
            for method in ('GET', 'HEAD'):
                assert (await c.request(method, f'/v2/public/app/blobs/{digest}')).status_code == 401
            assert (await c.get(f'/v2/private/app/blobs/{digest}', auth=('ci', 'cipass'))).content == payload
            # An explicitly published public manifest makes the shared layer public.
            assert (await c.put('/v2/public/app/manifests/latest', content=manifest,
                                headers={'content-type': MANIFEST_V2}, auth=('ci', 'cipass'))).status_code == 201
            assert (await c.get(f'/v2/public/app/blobs/{digest}')).content == payload
    finally:
        node.stop()
