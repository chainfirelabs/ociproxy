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


OCI_INDEX = "application/vnd.oci.image.index.v1+json"


def _referrers_index(subject: str, *artifact_types: str) -> bytes:
    return json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json",
         "digest": "sha256:" + f"{i:x}" * 64, "size": 100, "artifactType": t}
        for i, t in enumerate(artifact_types, start=1)
    ]}).encode()


async def test_mirror_referrers_is_200_empty_index_not_404(tmp_path, fake_upstream):
    """Docker's mirror pull asks for referrers after the manifest. A 404 makes
    the daemon retry on registry-1.docker.io, which stalls offline hosts for
    the connect timeout (30s) on every pull. With no referrers, the answer is
    an empty index."""
    node = _private_node(tmp_path, "mirror-ref", [_hub(fake_upstream)])
    ns = {"ns": f"127.0.0.1:{fake_upstream.port}"}
    try:
        img = fake_upstream.install_image(repo="library/alpine", tag="latest")
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            for repo in ("library/alpine", "library/never-heard-of-it"):
                r = await c.get(f"/v2/{repo}/referrers/{img['manifest']}", params=ns)
                assert r.status_code == 200, r.text
                assert r.headers["content-type"] == OCI_INDEX
                assert r.json() == {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": []}
            r = await c.head(f"/v2/library/alpine/referrers/{img['manifest']}", params=ns)
            assert r.status_code == 200 and r.headers["content-type"] == OCI_INDEX
            # the prefixed path behaves the same
            r = await c.get(f"/v2/hub/library/alpine/referrers/{img['manifest']}")
            assert r.status_code == 200 and r.json()["manifests"] == []
            r = await c.get("/v2/library/alpine/referrers/sha256:nothex", params=ns)
            assert r.status_code == 400
            r = await c.delete(f"/v2/library/alpine/referrers/{img['manifest']}", params=ns, auth=AUTH)
            assert r.status_code == 405
    finally:
        node.stop()


async def test_mirror_referrers_come_from_the_tag_schema(tmp_path, fake_upstream):
    node = _private_node(tmp_path, "mirror-ref-tag", [_hub(fake_upstream)])
    ns = {"ns": f"127.0.0.1:{fake_upstream.port}"}
    try:
        img = fake_upstream.install_image(repo="library/alpine", tag="latest")
        tag = "sha256-" + img["manifest"].split(":", 1)[1]
        fake_upstream.add_manifest("library/alpine", tag, OCI_INDEX, _referrers_index(
            img["manifest"], "application/vnd.dev.sigstore.bundle+json", "application/spdx+json"))
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            r = await c.get(f"/v2/library/alpine/referrers/{img['manifest']}", params=ns)
            assert r.status_code == 200, r.text
            assert [m["artifactType"] for m in r.json()["manifests"]] == [
                "application/vnd.dev.sigstore.bundle+json", "application/spdx+json"]
            assert "oci-filters-applied" not in r.headers
            r = await c.get(f"/v2/library/alpine/referrers/{img['manifest']}",
                            params={**ns, "artifactType": "application/spdx+json"})
            assert [m["artifactType"] for m in r.json()["manifests"]] == ["application/spdx+json"]
            assert r.headers["oci-filters-applied"] == "artifactType"
    finally:
        node.stop()


async def test_referrers_respect_visibility_and_local_pushes(tmp_path, fake_upstream):
    hub = replace(_hub(fake_upstream), sharing="local", visibility="private")
    node = _private_node(tmp_path, "ref-priv", [hub])
    ns = {"ns": f"127.0.0.1:{fake_upstream.port}"}
    subject = "sha256:" + "a" * 64
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30, base_url=node.url) as c:
            assert (await c.get(f"/v2/grafana/grafana/referrers/{subject}", params=ns)).status_code == 401
            r = await c.get(f"/v2/grafana/grafana/referrers/{subject}", params=ns, auth=AUTH)
            assert r.status_code == 200 and r.json()["manifests"] == []
            # a pushed referrers tag (written by clients such as oras/cosign)
            body = _referrers_index(subject, "application/vnd.example+json")
            r = await c.put(f"/v2/team/app/manifests/sha256-{'a' * 64}", content=body,
                            headers={"content-type": OCI_INDEX}, auth=AUTH)
            assert r.status_code == 201, r.text
            assert (await c.get(f"/v2/team/app/referrers/{subject}")).status_code == 401
            r = await c.get(f"/v2/team/app/referrers/{subject}", auth=AUTH)
            assert [m["artifactType"] for m in r.json()["manifests"]] == ["application/vnd.example+json"]
            # never served to mirror requests for the upstream's team/app
            r = await c.get(f"/v2/team/app/referrers/{subject}", params=ns, auth=AUTH)
            assert r.status_code == 200 and r.json()["manifests"] == []
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
