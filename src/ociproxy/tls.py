"""Outbound TLS trust: the public CA bundle plus optional extra CAs.

OCIPROXY_CA_PATH (or [network] ca_path) names a PEM file, or a directory whose
*.crt / *.pem files are all loaded. Those CAs are trusted IN ADDITION to the
public bundle (certifi) for every outbound HTTPS connection the registry makes:
replication to the primary and fetches from upstream registries. Typical use:
primary and replicas with certificates from an internal CA.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

import certifi

log = logging.getLogger(__name__)

_SUFFIXES = (".crt", ".pem")


def ca_files(ca_path: str) -> list[Path]:
    """PEM files named by ``ca_path``. Raises ValueError if it does not exist."""
    p = Path(ca_path)
    try:
        if p.is_dir():
            return sorted(f for f in p.iterdir()
                          if f.is_file() and f.suffix.lower() in _SUFFIXES and not f.name.startswith("."))
        if p.is_file():
            return [p]
    except PermissionError:
        raise ValueError(f"CA path {ca_path!r} is not readable by this container's user "
                         "(UID 65532): make the folder 755 and the files 644") from None
    raise ValueError(f"CA path {ca_path!r} does not exist")


def verify_setting(ca_path: str | None) -> ssl.SSLContext | bool:
    """httpx ``verify=`` value: True (public bundle only) or a context that
    also trusts the extra CAs. Invalid PEM fails loudly at startup."""
    if not ca_path:
        return True
    files = ca_files(ca_path)
    if not files:
        log.info("no CA certificates in %s; trusting the public CA bundle only", ca_path)
        return True
    ctx = ssl.create_default_context(cafile=certifi.where())
    # Python 3.13+ adds VERIFY_X509_STRICT, which rejects many internal CAs
    # that Docker, Go and curl accept (e.g. `openssl req -x509` CAs without a
    # keyUsage extension, or leaves without an authorityKeyIdentifier).
    # Chain, signature, expiry and hostname checks all remain.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    for f in files:
        try:
            ctx.load_verify_locations(cafile=str(f))
        except (ssl.SSLError, ValueError) as e:
            raise ValueError(f"cannot load CA certificate {f}: {e}") from None
    log.info("trusting %d extra CA file(s) from %s: %s", len(files), ca_path,
             ", ".join(f.name for f in files))
    return ctx
