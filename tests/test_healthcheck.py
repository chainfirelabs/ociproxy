"""Container health check modules (run in distroless images via -S -m)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ociproxy import healthcheck as reg_hc
from ociproxy_ui import healthcheck as ui_hc
from .conftest import free_port

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mod", [reg_hc, ui_hc])
def test_probe_host(mod):
    assert mod.probe_host("0.0.0.0") == "127.0.0.1"
    assert mod.probe_host("") == "127.0.0.1"
    assert mod.probe_host("::") == "::1"
    assert mod.probe_host("[::]") == "::1"
    assert mod.probe_host("10.1.2.3") == "10.1.2.3"
    assert mod.probe_host("[fd00::1]") == "fd00::1"


@pytest.mark.parametrize("mod", [reg_hc, ui_hc])
def test_check_status_and_failures(mod, primary):
    port = int(primary.url.rsplit(":", 1)[1])
    assert mod.check("127.0.0.1", port, "/v2/")
    assert not mod.check("127.0.0.1", port, "/no-such-path")  # 404
    assert not mod.check("127.0.0.1", free_port(), "/v2/", timeout=1)  # refused


def _run(module: str, env: dict) -> int:
    pythonpath = os.pathsep.join([str(ROOT / "src"), str(ROOT / "webapp" / "src")])
    full = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": pythonpath,
            # must be ignored: probes always go direct
            "HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", **env}
    return subprocess.run([sys.executable, "-S", "-m", module], env=full, timeout=30).returncode


def test_registry_module_cli_follows_listen_and_port(primary):
    port = primary.url.rsplit(":", 1)[1]
    assert _run("ociproxy.healthcheck", {"OCIPROXY_LISTEN": "127.0.0.1", "OCIPROXY_PORT": port}) == 0
    assert _run("ociproxy.healthcheck", {"OCIPROXY_PORT": str(free_port())}) == 1


def test_ui_module_cli_probes_its_endpoint(primary):
    # The node has no /api/auth/config: a live listener with the wrong
    # answer must fail, not pass on the TCP connect alone.
    port = primary.url.rsplit(":", 1)[1]
    assert _run("ociproxy_ui.healthcheck", {"UI_LISTEN": "127.0.0.1", "UI_PORT": port}) == 1
