"""Registry-mirror requests (?ns=<registry>): Docker daemons with
registry-mirrors and containerd hosts.toml mirrors send unprefixed repo
paths plus the registry they stand in for. Those resolve to that source,
with its visibility, and never to the node's local (pushed) namespace."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx

from ociproxy.config import Config, UpstreamConfig
from .conftest import Node, make_config
from .test_push import AUTH, MANIFEST_V2, push_blob


def _hub(fake_upstream, **kw) -> UpstreamConfig:
    return UpstreamConfig(name="hub", base_url=f"http://127.0.0.1:{fake_upstream.port}",
                          prefix="hub", official_namespace="library",
                          visibility="public", sharing="public", **kw)


def _private_node(tmp_path, name, upstreams):
    # Private by default, like the production example: anonymous pulls of
    # unprefixed (local) paths are refused.
    return Node(make_config(tmp_path, "primary", name=name, upstreams=upstreams,
                            push_username="ci", push_password="cipass",
                            pull_auth="basic")).start()


async def test_mirror_pull_is_anonymous_for_any_namespace(tmp_path, fake_upstream):
    node = _private_node(tmp_path, "mirror-any", [_hub(fake_upstream)])
    ns = f"127.0.0.1:{fake_upstream.port}"
    try:
        img = fake_upstream.install_image(repo="grafana/grafana", tag="latest")
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            # without ns this is the private local namespace
            assert (await c.get("/v2/grafana/grafana/manifests/latest")).status_code == 401
            r = await c.get("/v2/grafana/grafana/manifests/latest", params={"ns": ns})
            assert r.status_code == 200, r.text
            assert r.headers["docker-content-digest"] == img["manifest"]
            r = await c.head(f"/v2/grafana/grafana/manifests/{img['manifest']}", params={"ns": ns})
            assert r.status_code == 200
            r = await c.get(f"/v2/grafana/grafana/blobs/{img['layer1']}", params={"ns": ns})
            assert r.status_code == 200
            r = await c.get("/v2/grafana/grafana/tags/list", params={"ns": ns})
            assert r.status_code == 200 and "latest" in r.json()["tags"]
        # cached under the source, same as the prefixed path
        assert fake_upstream.hits["manifest:grafana/grafana:latest"] == 1
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            assert (await c.get("/v2/hub/grafana/grafana/manifests/latest")).status_code == 200
    finally:
        node.stop()


async def test_mirror_pull_official_image(tmp_path, fake_upstream):
    node = _private_node(tmp_path, "mirror-lib", [_hub(fake_upstream)])
    try:
        fake_upstream.install_image(repo="library/nginx", tag="alpine")
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            r = await c.get("/v2/library/nginx/manifests/alpine",
                            params={"ns": f"127.0.0.1:{fake_upstream.port}"})
            assert r.status_code == 200, r.text
    finally:
        node.stop()


async def test_mirror_never_serves_local_pushes(tmp_path, fake_upstream):
    """A pushed team/app and the upstream's team/app are different images;
    a mirror request for the upstream must not return (or expose) the push."""
    node = _private_node(tmp_path, "mirror-local", [_hub(fake_upstream)])
    ns = f"127.0.0.1:{fake_upstream.port}"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            config = b'{"local": true}'
            assert (await push_blob(c, "team/app", config)).status_code == 201
            import hashlib
            cd = "sha256:" + hashlib.sha256(config).hexdigest()
            manifest = json.dumps({"schemaVersion": 2, "mediaType": MANIFEST_V2,
                                   "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                                              "digest": cd, "size": len(config)},
                                   "layers": []}).encode()
            r = await c.put("/v2/team/app/manifests/v1", content=manifest,
                            headers={"content-type": MANIFEST_V2}, auth=AUTH)
            assert r.status_code == 201, r.text
            local_digest = r.headers["docker-content-digest"]

            # upstream has no team/app: mirror request is a miss, not the push
            r = await c.get("/v2/team/app/manifests/v1", params={"ns": ns})
            assert r.status_code == 404
            r = await c.get(f"/v2/team/app/blobs/{cd}", params={"ns": ns})
            assert r.status_code in (401, 404)

            # once upstream has it, the mirror serves the upstream image
            img = fake_upstream.install_image(repo="team/app", tag="v1")
            r = await c.get("/v2/team/app/manifests/v1", params={"ns": ns})
            assert r.status_code == 200
            assert r.headers["docker-content-digest"] == img["manifest"] != local_digest
            # and the push is still only readable with credentials
            assert (await c.get("/v2/team/app/manifests/v1")).status_code == 401
            r = await c.get("/v2/team/app/manifests/v1", auth=AUTH)
            assert r.headers["docker-content-digest"] == local_digest
    finally:
        node.stop()


async def test_mirror_respects_source_visibility_and_unknown_registry(tmp_path, fake_upstream):
    hub = replace(_hub(fake_upstream), sharing="local", visibility="private")
    node = _private_node(tmp_path, "mirror-priv", [hub])
    try:
        fake_upstream.install_image(repo="grafana/grafana", tag="latest")
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            ns = {"ns": f"127.0.0.1:{fake_upstream.port}"}
            assert (await c.get("/v2/grafana/grafana/manifests/latest", params=ns)).status_code == 401
            r = await c.get("/v2/grafana/grafana/manifests/latest", params=ns, auth=AUTH)
            assert r.status_code == 200
            # a registry no source mirrors: 404 so the client goes direct
            r = await c.get("/v2/grafana/grafana/manifests/latest", params={"ns": "quay.io"})
            assert r.status_code == 404
            # writes are never redirected by ns
            r = await c.post("/v2/grafana/grafana/blobs/uploads/", params=ns, auth=AUTH)
            assert r.status_code == 202
    finally:
        node.stop()


def test_upstream_for_registry_maps_docker_hub_and_prefers_public():
    hub_private = UpstreamConfig(name="hubauth", base_url="https://registry-1.docker.io",
                                 prefix="hubauth", visibility="private")
    hub = UpstreamConfig(name="dockerhub", base_url="https://registry-1.docker.io",
                         prefix="dockerhub", visibility="public", sharing="public")
    ghcr = UpstreamConfig(name="ghcr", base_url="https://ghcr.io", prefix="ghcr",
                          visibility="public", sharing="public")
    cfg = Config(role="primary", storage_path="unused", upstreams=[hub_private, hub, ghcr])
    assert cfg.upstream_for_registry("docker.io") is hub
    assert cfg.upstream_for_registry("index.docker.io") is hub
    assert cfg.upstream_for_registry("GHCR.io") is ghcr
    assert cfg.upstream_for_registry("quay.io") is None
    assert Config(role="primary", storage_path="unused", upstreams=[hub_private]).upstream_for_registry("docker.io") is hub_private
