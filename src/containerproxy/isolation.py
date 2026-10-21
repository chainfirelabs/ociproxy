"""Public replication policy. No machine credential authorizes local administration."""
from __future__ import annotations

from .auth import AccessPolicy
from .store import HELM_NS

PROTOCOL = "public-v2"
SCHEMA_KEY = "node_isolation_schema"


def shared_repo(config, upstream: str, repo: str) -> bool:
    policy = AccessPolicy(config)
    if upstream.startswith(HELM_NS):
        name = upstream[len(HELM_NS):]
        source = config.helm_repo(name)
        return bool(source and source.sharing == "public" and source.visibility == "public"
                    and policy.is_public(f"helm/{name}"))
    source = next((u for u in config.upstreams if u.storage_name == upstream), None)
    return bool(source and source.sharing == "public" and source.visibility == "public"
                and policy.is_public(f"{source.prefix}/{repo}"))


def direct_source(config, upstream: str) -> bool:
    return config.is_primary or upstream.startswith("private:")
