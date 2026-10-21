"""Shared fixtures: real uvicorn nodes on ephemeral ports + fake upstream."""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

# The webapp lives in its own package tree (own image); expose it to tests.
_UI_SRC = str(Path(__file__).resolve().parent.parent / "webapp" / "src")
if _UI_SRC not in sys.path:
    sys.path.insert(0, _UI_SRC)

import pytest
import uvicorn

from containerproxy.api import App
from containerproxy.config import Config, HelmRepoConfig, UpstreamConfig

from .fake_upstream import FakeUpstream

SECRET = "test-secret"
_RUNNING_NODES = {}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Node:
    """A containerproxy node running in a background thread."""

    def __init__(self, config: Config):
        self.config = config
        if not config.is_primary and config.primary_url in _RUNNING_NODES:
            _RUNNING_NODES[config.primary_url].config.replication_peers[config.replication_id] = {
                "token": config.replication_token, "publish": True}
        self.app_obj = App(config)
        self.server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "Node":
        cfg = uvicorn.Config(
            self.app_obj.app, host="127.0.0.1", port=self.config.port,
            log_level="warning", access_log=False, timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self.server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("node did not start")
            time.sleep(0.05)
        _RUNNING_NODES[self.url] = self
        return self

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
            self._thread.join(timeout=15)

    def server_loop(self):
        """The node's event loop (for running coroutines on it from tests)."""
        return self.app_obj.changes._loop

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.config.port}"


def make_config(
    tmp_path,
    role: str,
    *,
    name: str,
    upstreams: list[UpstreamConfig] | None = None,
    primary_url: str | None = None,
    advertised_url: str | None = None,
    push_username: str | None = None,
    push_password: str | None = None,
    port: int | None = None,
    upstream_proxy: str | None = None,
    pull_auth: str = "none",
    tag_ttl: float = 300.0,
    helm_repos: list[HelmRepoConfig] | None = None,
    access_rules: list | None = None,
    upload_idle_timeout: float = 900.0,
    upload_max_sessions: int = 256,
) -> Config:
    return Config(
        role=role,
        storage_path=str(tmp_path / f"storage-{name}"),
        listen="127.0.0.1",
        port=port or free_port(),
        secret=SECRET,
        upstreams=upstreams or [],
        helm_repos=helm_repos or [],
        upstream_proxy=upstream_proxy,
        primary_url=primary_url,
        replication_id=name if role == "replica" else "",
        replication_token=f"replication-test-token-{name}" if role == "replica" else "",
        # pull_auth="basic" is the older spelling of a private default.
        default_visibility="private" if pull_auth == "basic" else "public",
        access_rules=access_rules or [],
        upload_idle_timeout=upload_idle_timeout,
        upload_max_sessions=upload_max_sessions,
        tag_ttl=tag_ttl,
        advertised_url=advertised_url,
        push_username=push_username,
        push_password=push_password,
    )


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """CLI tests run setup_logging(), which points the root logger at that
    test's captured stderr; later node threads would log into a closed file."""
    import logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def fake_upstream():
    up = FakeUpstream()
    up.start()
    yield up
    up.stop()


@pytest.fixture
def upstream_config(fake_upstream):
    return UpstreamConfig(
        name="fake", base_url=f"http://127.0.0.1:{fake_upstream.port}", prefix="fake",
        visibility="public", sharing="public"
    )


async def wait_until(fn, timeout: float = 15.0, msg: str = "condition") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {msg}")


@pytest.fixture
def primary(tmp_path, upstream_config):
    node = Node(make_config(
        tmp_path, "primary", name="p1",
        upstreams=[upstream_config],
        push_username="ci", push_password="cipass",
    )).start()
    yield node
    node.stop()


@pytest.fixture
def replica(tmp_path, primary, upstream_config):
    port = free_port()
    node = Node(make_config(
        tmp_path, "replica", name="r1",
        upstreams=[upstream_config],
        primary_url=primary.url,
        advertised_url=f"http://127.0.0.1:{port}",
        port=port,
    )).start()
    yield node
    node.stop()


def make_replica(tmp_path, primary, name: str, upstreams: list[UpstreamConfig] | None = None) -> Node:
    port = free_port()
    return Node(make_config(
        tmp_path, "replica", name=name,
        upstreams=upstreams,
        primary_url=primary.url,
        advertised_url=f"http://127.0.0.1:{port}",
        port=port,
    )).start()
