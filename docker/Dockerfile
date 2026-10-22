# syntax=docker/dockerfile:1

# Base images are pinned by multi-arch index digest for reproducible builds.
# To update: skopeo inspect --raw docker://<image:tag> | sha256sum
# Keep the builder's Python minor version equal to the runtime's (3.13).

# Build stage: install ociproxy + deps into an isolated directory.
# Runtime deps are pure Python or manylinux wheels (pyyaml, cryptography)
# built for the same Python minor and glibc, so the tree is portable into
# the distroless python3 image.
FROM python:3.13-slim-trixie@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS builder
WORKDIR /build
# Locked, hash-verified dependencies (regenerate with:
#   uv pip compile pyproject.toml --generate-hashes --python-version 3.13 --universal -o requirements.lock)
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --target /staged -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY docs/THIRD_PARTY_NOTICES.md ./
COPY third_party/ ./third_party/
COPY src ./src
RUN pip install --no-cache-dir --no-deps --target /staged . \
 && mkdir -p /staged-root/var/lib/ociproxy \
 && chown -R 65532:65532 /staged-root

# Runtime: Google distroless python3 (Debian 13, Python 3.13), nonroot user (uid 65532).
# Pinned to the explicit -debian13 name: the bare "python3" alias moves
# between Debian releases (and Python versions) without notice.
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:8ee214843129f43e2ebf5e0ca9f2e4e6d8292143d1b8a6787f169b5898578884

# Version-independent path (pip --target), so a Python bump in the base
# image cannot silently orphan the installed packages.
ENV PYTHONPATH=/app/site-packages \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OCIPROXY_LISTEN=0.0.0.0 \
    OCIPROXY_PORT=5000 \
    OCIPROXY_STORAGE_PATH=/var/lib/ociproxy \
    OCIPROXY_CONFIG=/app/ociproxy.toml

COPY --from=builder /staged /app/site-packages
# Preserve project, dependency and supplemental third-party license notices.
COPY --from=builder /build/LICENSE /build/THIRD_PARTY_NOTICES.md /usr/share/licenses/ociproxy/
COPY --from=builder /build/third_party/ /usr/share/licenses/ociproxy/third_party/
# /var/lib/ociproxy pre-owned by nonroot so named volumes initialize writable.
COPY --from=builder --chown=65532:65532 /staged-root/ /

EXPOSE 5000

ENTRYPOINT ["/usr/bin/python3", "-m", "ociproxy.cli", "serve"]
