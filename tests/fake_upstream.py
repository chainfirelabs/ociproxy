"""Fake authenticated Docker registry for tests.

Serves a synthetic image (config + 2 layers + manifest v2) and a manifest
list wrapping it, behind a standard bearer-token challenge. Counts served
(200) hits so tests can assert on caching behavior; the challenge 401s are
not counted.

Also serves Helm charts both ways: as OCI artifacts in the registry, and as
a classic HTTP chart repository (index.yaml + .tgz) under /charts/.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import tarfile
import threading
import time
from collections import Counter

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

MANIFEST_V2 = "application/vnd.docker.distribution.manifest.v2+json"
MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
CONFIG_TYPE = "application/vnd.docker.container.image.v1+json"
LAYER_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
HELM_CONFIG_TYPE = "application/vnd.cncf.helm.config.v1+json"
HELM_CHART_LAYER = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"

FAKE_TOKEN = "fake-bearer-token"


def sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def make_chart_tgz(name: str, version: str, app_version: str = "1.10") -> bytes:
    """A minimal but valid Helm chart archive."""
    files = {
        f"{name}/Chart.yaml": (
            f"apiVersion: v2\nname: {name}\nversion: {version}\n"
            f"appVersion: \"{app_version}\"\ndescription: test chart\n"
        ).encode(),
        f"{name}/values.yaml": b"replicas: 1\n",
        f"{name}/templates/cm.yaml": (
            b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}\n"
        ),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeUpstream:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[str, bytes, str]] = {}
        self.hits: Counter[str] = Counter()
        self.require_auth = True
        self.last_accept: str | None = None
        self.last_blob_accept_encoding: str | None = None
        # tags/list page size cap (forces Link pagination in tests).
        self.tags_page_size = 2
        # Classic Helm repo: index entries (name -> list of dicts) and files.
        self.helm_entries: dict[str, list[dict]] = {}
        self.helm_files: dict[str, bytes] = {}
        self.helm_auth: tuple[str, str] | None = None
        self.helm_etag_support = True
        self.helm_raw_index: bytes | None = None  # overrides the generated index
        self.helm_auth_seen: dict[str, str | None] = {}  # path -> Authorization
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self.app = self._build()

    def _build(self) -> Starlette:
        return Starlette(routes=[
            Route("/v2/", self.ping),
            Route("/token", self.token),
            Route("/charts/index.yaml", self.helm_index),
            Route("/charts/{file:path}", self.helm_file),
            Route("/ext/{file:path}", self.helm_file),
            Route("/v2/{repo:path}/tags/list", self.tags),
            Route("/v2/{repo:path}/manifests/{ref}", self.manifest),
            Route("/v2/{repo:path}/blobs/{digest}", self.blob),
        ])

    # -- content registration -------------------------------------------------

    def add_blob(self, data: bytes) -> str:
        d = sha(data)
        self.blobs[d] = data
        return d

    def add_manifest(self, repo: str, ref: str, media: str, body: bytes) -> str:
        d = sha(body)
        self.manifests[(repo, ref)] = (media, body, d)
        return d

    def install_image(self, repo: str = "myimg", tag: str = "v1") -> dict:
        """Synthetic single-arch image + a manifest list wrapping it."""
        config = json.dumps({
            "architecture": "amd64", "os": "linux",
            "config": {"Cmd": ["/bin/sh"]},
            "rootfs": {"type": "layers", "diff_ids": []},
        }).encode()
        layer1 = os.urandom(4096)
        layer2 = b"second-layer-" * 512
        cd = self.add_blob(config)
        l1 = self.add_blob(layer1)
        l2 = self.add_blob(layer2)
        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST_V2,
            "config": {"mediaType": CONFIG_TYPE, "digest": cd, "size": len(config)},
            "layers": [
                {"mediaType": LAYER_TYPE, "digest": l1, "size": len(layer1)},
                {"mediaType": LAYER_TYPE, "digest": l2, "size": len(layer2)},
            ],
        }
        mb = json.dumps(manifest).encode()
        md = self.add_manifest(repo, tag, MANIFEST_V2, mb)
        self.add_manifest(repo, md, MANIFEST_V2, mb)  # tag manifests served by digest too
        list_doc = {
            "schemaVersion": 2,
            "mediaType": MANIFEST_LIST,
            "manifests": [
                {"mediaType": MANIFEST_V2, "digest": md, "size": len(mb),
                 "platform": {"architecture": "amd64", "os": "linux"}},
            ],
        }
        lb = json.dumps(list_doc).encode()
        ld = self.add_manifest(repo, "multi", MANIFEST_LIST, lb)
        self.add_manifest(repo, ld, MANIFEST_LIST, lb)
        return {"config": cd, "layer1": l1, "layer2": l2, "manifest": md, "list": ld}

    def install_oci_chart(self, repo: str = "charts/mychart", version: str = "1.0.0") -> dict:
        """A Helm chart pushed to the registry as an OCI artifact."""
        name = repo.rsplit("/", 1)[-1]
        config = json.dumps({"name": name, "version": version, "apiVersion": "v2"}).encode()
        tgz = make_chart_tgz(name, version)
        cd = self.add_blob(config)
        ld = self.add_blob(tgz)
        manifest = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {"mediaType": HELM_CONFIG_TYPE, "digest": cd, "size": len(config)},
            "layers": [{"mediaType": HELM_CHART_LAYER, "digest": ld, "size": len(tgz)}],
        }
        mb = json.dumps(manifest).encode()
        md = self.add_manifest(repo, version, OCI_MANIFEST, mb)
        self.add_manifest(repo, md, OCI_MANIFEST, mb)
        return {"config": cd, "chart": ld, "manifest": md, "tgz": tgz}

    def add_helm_chart(
        self, name: str, version: str, *, url: str | None = None,
        with_digest: bool = True, app_version: str = "1.10",
    ) -> bytes:
        """Publish a chart in the classic repo. ``url`` defaults to a
        relative ``<name>-<version>.tgz`` (resolved against /charts/)."""
        tgz = make_chart_tgz(name, version, app_version)
        url = url or f"{name}-{version}.tgz"
        path = url.split(f"127.0.0.1:{self.port}", 1)[-1].split(f"localhost:{self.port}", 1)[-1]
        if not path.startswith("/"):
            path = f"/charts/{path}"
        self.helm_files[path] = tgz
        entry = {"version": version, "appVersion": app_version, "urls": [url],
                 "created": "2026-01-02T03:04:05.123456789Z"}
        if with_digest:
            entry["digest"] = hashlib.sha256(tgz).hexdigest()
        self.helm_entries.setdefault(name, []).insert(0, entry)
        return tgz

    def helm_index_bytes(self) -> bytes:
        if self.helm_raw_index is not None:
            return self.helm_raw_index
        # Hand-rolled YAML (shaped like `helm repo index` output) so the
        # proxy is tested on text it did not produce: the quoted appVersion
        # "1.10" (1.1 after a naive YAML round-trip) and the RFC 3339
        # timestamp must survive byte-for-byte.
        lines = ["apiVersion: v1", "entries:"]
        for name, versions in sorted(self.helm_entries.items()):
            lines.append(f"  {name}:")
            for e in versions:
                lines.append("  - apiVersion: v2")
                lines.append(f"    appVersion: \"{e['appVersion']}\"")
                lines.append(f"    created: {e['created']}")
                if "digest" in e:
                    lines.append(f"    digest: {e['digest']}")
                lines.append(f"    name: {name}")
                lines.append("    urls:")
                for u in e["urls"]:
                    lines.append(f"    - {u}")
                lines.append(f"    version: {e['version']}")
        lines.append("generated: 2026-01-02T03:04:05Z")
        return ("\n".join(lines) + "\n").encode()

    # -- handlers ---------------------------------------------------------------

    def _helm_denied(self, request: Request) -> Response | None:
        if self.helm_auth is None:
            return None
        expected = "Basic " + base64.b64encode(":".join(self.helm_auth).encode()).decode()
        if request.headers.get("authorization") != expected:
            return Response(status_code=401, headers={"Www-Authenticate": 'Basic realm="charts"'})
        return None

    async def helm_index(self, request: Request) -> Response:
        if (denied := self._helm_denied(request)) is not None:
            return denied
        body = self.helm_index_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        if self.helm_etag_support and request.headers.get("if-none-match") == etag:
            self.hits["helm-index-304"] += 1
            return Response(status_code=304, headers={"ETag": etag})
        self.hits["helm-index"] += 1
        headers = {"ETag": etag} if self.helm_etag_support else {}
        return Response(body, media_type="application/x-yaml", headers=headers)

    async def helm_file(self, request: Request) -> Response:
        path = request.url.path
        self.helm_auth_seen[path] = request.headers.get("authorization")
        if path.startswith("/charts/") and (denied := self._helm_denied(request)) is not None:
            return denied
        data = self.helm_files.get(path)
        if data is None:
            return Response(status_code=404)
        self.hits[f"helm-file:{path}"] += 1
        return Response(data, media_type="application/gzip")

    def _authed(self, request: Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {FAKE_TOKEN}"

    def _challenge(self, repo: str) -> Response:
        realm = f"http://127.0.0.1:{self.port}/token"
        return Response(
            status_code=401,
            headers={
                "Www-Authenticate": f'Bearer realm="{realm}",service="fake",scope="repository:{repo}:pull"'
            },
        )

    async def ping(self, request: Request) -> Response:
        self.hits["ping"] += 1
        if self.require_auth:
            return self._challenge("_")
        return JSONResponse({})

    async def token(self, request: Request) -> Response:
        self.hits["token"] += 1
        return JSONResponse({"token": FAKE_TOKEN, "expires_in": 300})

    async def tags(self, request: Request) -> Response:
        repo = request.path_params["repo"]
        if self.require_auth and not self._authed(request):
            return self._challenge(repo)
        self.hits[f"tags:{repo}"] += 1
        tags = sorted(ref for (r, ref) in self.manifests if r == repo and ":" not in ref)
        if not tags:
            return JSONResponse({"errors": [{"code": "NAME_UNKNOWN"}]}, status_code=404)
        n = min(int(request.query_params.get("n", 100)), self.tags_page_size)
        last = request.query_params.get("last")
        if last:
            tags = [t for t in tags if t > last]
        page, more = tags[:n], len(tags) > n
        headers = {}
        if more:
            headers["Link"] = f'</v2/{repo}/tags/list?n={n}&last={page[-1]}>; rel="next"'
        return JSONResponse({"name": repo, "tags": page}, headers=headers)

    async def manifest(self, request: Request) -> Response:
        repo = request.path_params["repo"]
        ref = request.path_params["ref"]
        if self.require_auth and not self._authed(request):
            return self._challenge(repo)
        # HEADs (tag revalidation) are counted separately from full GETs.
        kind = "manifest-head" if request.method == "HEAD" else "manifest"
        self.hits[f"{kind}:{repo}:{ref}"] += 1
        self.last_accept = request.headers.get("accept")
        entry = self.manifests.get((repo, ref))
        if entry is None:
            return Response(status_code=404)
        media, body, digest = entry
        return Response(body, media_type=media, headers={"Docker-Content-Digest": digest})

    async def blob(self, request: Request) -> Response:
        digest = request.path_params["digest"]
        if self.require_auth and not self._authed(request):
            return self._challenge(request.path_params.get("repo", "_"))
        self.hits[f"blob:{digest}"] += 1
        self.last_blob_accept_encoding = request.headers.get("accept-encoding")
        data = self.blobs.get(digest)
        if data is None:
            return Response(status_code=404)
        if request.path_params.get("repo") == "slowimg":
            # Drip-feed so tests can assert time-to-first-byte through the
            # proxy (4 chunks x 0.25s ~= 1s total).
            async def slow():
                for i in range(0, len(data), 1024):
                    await asyncio.sleep(0.25)
                    yield data[i:i + 1024]
            return StreamingResponse(slow(), media_type="application/octet-stream",
                                    headers={"Docker-Content-Digest": digest})
        return Response(data, media_type="application/octet-stream",
                       headers={"Docker-Content-Digest": digest})

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                            log_level="warning", access_log=False)
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 15
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("fake upstream did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
            self._thread.join(timeout=15)
