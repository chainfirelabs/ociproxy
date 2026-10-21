"""Environment-driven configuration (container deployment path)."""

from __future__ import annotations

import json

import pytest

from containerproxy.config import load_config


def test_env_overrides_file(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[node]\nrole = "primary"\nsecret = "filesecret"\n'
        '[storage]\npath = "./var"\n'
    )
    monkeypatch.setenv("CONTAINERPROXY_ROLE", "replica")
    monkeypatch.setenv("CONTAINERPROXY_SECRET", "envsecret")
    monkeypatch.setenv("CONTAINERPROXY_PRIMARY_URL", "http://primary:5000")
    monkeypatch.setenv("CONTAINERPROXY_STORAGE_PATH", str(tmp_path / "store"))
    monkeypatch.setenv("CONTAINERPROXY_PORT", "6000")
    c = load_config(str(cfg))
    assert c.role == "replica"
    assert c.secret == "envsecret"
    assert c.primary_url == "http://primary:5000"
    assert c.storage_path == str(tmp_path / "store")
    assert c.port == 6000
    # replica without advertised_url defaults to a resolvable http URL
    assert c.advertised_url and c.advertised_url.startswith("http://")
    assert c.advertised_url.endswith(":6000")


def test_env_only_no_file(monkeypatch):
    monkeypatch.setenv("CONTAINERPROXY_ROLE", "primary")
    monkeypatch.setenv("CONTAINERPROXY_SECRET", "s3cret")
    monkeypatch.setenv("CONTAINERPROXY_STORAGE_PATH", "/tmp/dpx-env-store")
    monkeypatch.setenv("CONTAINERPROXY_UPSTREAMS", json.dumps([
        {"name": "dockerhub", "base_url": "https://registry-1.docker.io", "prefix": "dh"},
        {"name": "ghcr", "base_url": "https://ghcr.io", "prefix": "ghcr"},
    ]))
    c = load_config(None)
    assert c.role == "primary"
    assert c.secret == "s3cret"
    assert [u.prefix for u in c.upstreams] == ["dh", "ghcr"]


def test_replica_requires_primary(monkeypatch):
    monkeypatch.setenv("CONTAINERPROXY_ROLE", "replica")
    monkeypatch.delenv("CONTAINERPROXY_PRIMARY_URL", raising=False)
    monkeypatch.delenv("CONTAINERPROXY_ADVERTISED_URL", raising=False)
    with pytest.raises(ValueError, match="primary"):
        load_config(None)


def test_primary_rejects_primary_url(monkeypatch):
    monkeypatch.setenv("CONTAINERPROXY_ROLE", "primary")
    monkeypatch.setenv("CONTAINERPROXY_PRIMARY_URL", "http://elsewhere:5000")
    with pytest.raises(ValueError, match="must not"):
        load_config(None)


def test_bad_upstreams_json(monkeypatch):
    monkeypatch.setenv("CONTAINERPROXY_ROLE", "primary")
    monkeypatch.setenv("CONTAINERPROXY_UPSTREAMS", "{not json")
    with pytest.raises(ValueError, match="JSON"):
        load_config(None)


def test_file_only_config_still_works(tmp_path, monkeypatch):
    for var in list(__import__("os").environ):
        if var.startswith("CONTAINERPROXY_"):
            monkeypatch.delenv(var)
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[node]\nrole = "replica"\nsecret = "s"\nprimary_url = "http://p:1"\n'
        f'[storage]\npath = "{tmp_path}/v"\n'
        '[[upstream]]\nname = "hub"\nbase_url = "https://registry-1.docker.io"\nprefix = "hub"\n'
    )
    c = load_config(str(cfg))
    assert c.role == "replica"
    assert c.primary_url == "http://p:1"
    assert c.upstreams[0].name == "hub"


def test_managed_sources_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTAINERPROXY_SECRET", "s")
    assert load_config().managed_sources is True
    cfg = tmp_path / "c.toml"
    cfg.write_text("[sources]\nmanaged = false\n")
    assert load_config(str(cfg)).managed_sources is False
    monkeypatch.setenv("CONTAINERPROXY_MANAGED_SOURCES", "true")  # env wins
    assert load_config(str(cfg)).managed_sources is True
    monkeypatch.setenv("CONTAINERPROXY_MANAGED_SOURCES", "maybe")
    with pytest.raises(ValueError, match="MANAGED_SOURCES"):
        load_config()
