"""Container health check: ``python3 -S -m ociproxy.healthcheck``.

Exit 0 when this container's registry answers ``GET /v2/`` with 2xx, else 1.
The runtime image is distroless (no shell, curl or wget), so this runs on the
image's Python. It speaks HTTP/1.0 over a raw socket instead of importing
urllib/http.client, which keeps each check to a few milliseconds of CPU, and
never honours HTTP(S)_PROXY. It reads the same OCIPROXY_LISTEN/OCIPROXY_PORT
the server does, so a changed port or listen address needs no new probe.

Deliberately imports nothing from ociproxy beyond the (trivial) package
``__init__``; keep it stdlib-only.
"""

from __future__ import annotations

import os
import sys

# The C module directly: `import socket` pulls in enum, selectors, collections
# and re (~35 ms per run), and a str host makes getaddrinfo load the idna codec
# (~16 ms). Together that was most of each check's CPU time.
import _socket

PATH = "/v2/"


def probe_host(listen: str) -> str:
    """Address to probe for a server bound to ``listen``."""
    if listen in ("", "0.0.0.0"):
        return "127.0.0.1"
    if listen in ("::", "[::]"):
        return "::1"
    return listen.strip("[]")


def check(host: str, port: int, path: str = PATH, timeout: float = 5.0) -> bool:
    try:
        family, kind, proto, _, addr = _socket.getaddrinfo(
            host.encode("ascii"), port, 0, _socket.SOCK_STREAM)[0]
        s = _socket.socket(family, kind, proto)
        try:
            s.settimeout(timeout)
            s.connect(addr)
            s.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode("ascii"))
            head = b""
            while b"\r\n" not in head and len(head) < 1024:
                chunk = s.recv(1024)
                if not chunk:
                    break
                head += chunk
        finally:
            s.close()
    except (OSError, UnicodeError):
        return False
    parts = head.split(b"\r\n", 1)[0].split()
    return len(parts) >= 2 and parts[0].startswith(b"HTTP/") and parts[1][:1] == b"2"


def main() -> int:
    host = probe_host(os.environ.get("OCIPROXY_LISTEN", "0.0.0.0"))
    port = int(os.environ.get("OCIPROXY_PORT", "5000"))
    return 0 if check(host, port) else 1


if __name__ == "__main__":
    sys.exit(main())
