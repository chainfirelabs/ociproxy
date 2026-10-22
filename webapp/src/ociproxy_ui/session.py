"""Signed, expiring cookie values (HMAC-SHA256), no server-side state.

Keys are derived from the node secret per purpose, so a session cookie can
never be replayed as an OIDC-flow cookie and vice versa, and the UI needs
no secret of its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, secret: str, purpose: str):
        self._key = hmac.new(secret.encode(), f"ociproxy-ui:{purpose}".encode(),
                             hashlib.sha256).digest()

    def _sig(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def dumps(self, payload: dict, max_age: float) -> str:
        body = _b64(json.dumps({**payload, "exp": time.time() + max_age},
                               separators=(",", ":")).encode())
        return f"{body}.{self._sig(body)}"

    def loads(self, value: str | None) -> dict | None:
        if not value or "." not in value:
            return None
        body, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(body)):
            return None
        try:
            payload = json.loads(_unb64(body))
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
            return None
        return payload
