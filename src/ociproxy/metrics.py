"""Minimal Prometheus text-format metrics (no client library dependency)."""

from __future__ import annotations

import threading
from collections import defaultdict

COUNTERS = {
    "ociproxy_manifest_requests_total":
        "Manifest resolutions by result (hit, miss, revalidated, refreshed, stale).",
    "ociproxy_blob_requests_total": "Blob requests by result (hit, miss).",
    "ociproxy_helm_requests_total":
        "Helm repo resolutions by kind (index, chart) and result (hit, miss, revalidated, refreshed, stale).",
    "ociproxy_source_fetches_total":
        "Fetches from the source (upstream registry on a primary, the primary on a replica).",
    "ociproxy_source_errors_total": "Failed fetches from the source.",
    "ociproxy_source_bytes_total": "Blob bytes downloaded from the source.",
    "ociproxy_uploads_expired_total": "Push upload sessions dropped after going idle.",
    "ociproxy_gc_runs_total": "Garbage collection runs.",
    "ociproxy_gc_deleted_manifests_total": "Idle mirrored manifests expired by GC.",
    "ociproxy_gc_deleted_blobs_total": "Unreferenced blobs deleted by GC.",
    "ociproxy_gc_freed_bytes_total": "Bytes freed by GC.",
}


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels) + "}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))

    def inc(self, name: str, value: float = 1, **labels: str) -> None:
        assert name in COUNTERS, name
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._counters[name][key] += value

    def value(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._counters[name].get(tuple(sorted(labels.items())), 0.0)

    def render(self, gauges: list[tuple[str, str, dict, float]]) -> str:
        """Counters plus point-in-time gauges given as (name, help, labels, value)."""
        lines: list[str] = []
        with self._lock:
            snapshot = {n: dict(v) for n, v in self._counters.items()}
        for name, help_text in COUNTERS.items():
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
            for labels, v in sorted(snapshot.get(name, {}).items()):
                lines.append(f"{name}{_fmt_labels(labels)} {v:g}")
        seen: set[str] = set()
        for name, help_text, labels, v in gauges:
            if name not in seen:
                lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
                seen.add(name)
            lines.append(f"{name}{_fmt_labels(tuple(sorted(labels.items())))} {v:g}")
        return "\n".join(lines) + "\n"
