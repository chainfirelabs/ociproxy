"""OCIPROXY_CA_PATH: extra CAs for outbound HTTPS (replication, upstreams).

The primary here serves HTTPS with a certificate from a throwaway internal
CA. A replica only replicates from it when told to trust that CA."""

from __future__ import annotations

import ssl
from dataclasses import replace

import httpx
import pytest

from ociproxy.config import load_config
from ociproxy.tls import verify_setting
from .conftest import Node, free_port, make_config


def _https_primary(tmp_path, internal_ca, upstream_config, name):
    return Node(make_config(tmp_path, "primary", name=name, upstreams=[upstream_config]),
                ssl_certfile=str(internal_ca / "server.crt"),
                ssl_keyfile=str(internal_ca / "server.key")).start()


def _replica(tmp_path, primary, name, ca_path=None):
    port = free_port()
    cfg = make_config(tmp_path, "replica", name=name, primary_url=primary.url,
                      advertised_url=f"http://127.0.0.1:{port}", port=port)
    return Node(replace(cfg, ca_path=ca_path)).start()


async def test_replica_trusts_primary_signed_by_internal_ca(tmp_path, internal_ca,
                                                            fake_upstream, upstream_config):
    primary = _https_primary(tmp_path, internal_ca, upstream_config, "tls-p")
    # a directory works as well as a single file
    trust = tmp_path / "trust"
    trust.mkdir()
    (trust / "internal-ca.crt").write_bytes((internal_ca / "ca.crt").read_bytes())
    replica = _replica(tmp_path, primary, "tls-r", ca_path=str(trust))
    try:
        img = fake_upstream.install_image()
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
            assert r.status_code == 200, r.text
            assert r.headers["docker-content-digest"] == img["manifest"]
            r = await c.get(f"{replica.url}/v2/fake/myimg/blobs/{img['layer1']}")
            assert r.status_code == 200
    finally:
        replica.stop()
        primary.stop()


async def test_replica_without_the_ca_cannot_reach_primary(tmp_path, internal_ca,
                                                           fake_upstream, upstream_config):
    primary = _https_primary(tmp_path, internal_ca, upstream_config, "tls-p2")
    replica = _replica(tmp_path, primary, "tls-r2")
    try:
        fake_upstream.install_image()
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.get(f"{replica.url}/v2/fake/myimg/manifests/v1")
            assert r.status_code != 200
    finally:
        replica.stop()
        primary.stop()


def test_verify_setting(tmp_path, internal_ca):
    assert verify_setting(None) is True
    empty = tmp_path / "empty"
    empty.mkdir()
    assert verify_setting(str(empty)) is True  # nothing to add
    ctx = verify_setting(str(internal_ca / "ca.crt"))
    assert isinstance(ctx, ssl.SSLContext)
    # the public bundle is still trusted alongside the extra CA
    subjects = {dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()}
    assert "test internal CA" in subjects and len(subjects) > 50
    # internal CAs often fail Python's strict RFC 5280 profile (this fixture's
    # leaf has no authorityKeyIdentifier); chain/hostname checks stay on
    assert not ctx.verify_flags & ssl.VERIFY_X509_STRICT
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate")
    with pytest.raises(ValueError, match="bad.pem"):
        verify_setting(str(bad))


def test_load_config_ca_path(tmp_path, internal_ca, monkeypatch):
    toml = tmp_path / "c.toml"
    toml.write_text(f'[storage]\npath = "{tmp_path}/s"\n[network]\nca_path = "{internal_ca / "ca.crt"}"\n')
    assert load_config(str(toml)).ca_path == str(internal_ca / "ca.crt")
    monkeypatch.setenv("OCIPROXY_CA_PATH", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="does not exist"):
        load_config(str(toml))


def test_unreadable_ca_dir_is_a_clear_error(tmp_path, internal_ca):
    import os
    from ociproxy.tls import ca_files
    from ociproxy_ui.tls import ca_files as ui_ca_files
    d = tmp_path / "locked"
    d.mkdir()
    (d / "ca.crt").write_bytes((internal_ca / "ca.crt").read_bytes())
    d.chmod(0o000)
    try:
        if os.access(d, os.R_OK):
            pytest.skip("running as root: permissions are not enforced")
        for fn in (ca_files, ui_ca_files):
            with pytest.raises(ValueError, match="not readable"):
                fn(str(d))
    finally:
        d.chmod(0o755)
