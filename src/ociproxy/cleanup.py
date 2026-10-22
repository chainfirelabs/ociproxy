"""Node-local cache management and durable public-only cleanup requests."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
import uuid

from .store import LOCAL, parse_digest

MAX_TARGETS = 2000
PREVIEW_TTL = 300


def snapshot_locked(store):
    return {tuple(r[:3]): {"digest": r[3], "body": r[4], "last_used": r[5]}
            for r in store.db.execute(
                "SELECT upstream,repo,reference,digest,bytes,MAX(stored_at,checked_at,pulled_at) FROM manifests")}


def expand_keys(rows, keys):
    """Include cached multi-platform child manifests in the same repository."""
    by_digest = {}
    for key, value in rows.items():
        by_digest.setdefault((*key[:2], value["digest"]), set()).add(key)
    result, pending = set(keys), list(keys)
    while pending:
        key = pending.pop()
        if key not in rows:
            continue
        try:
            doc = json.loads(rows[key]["body"])
            children = doc.get("manifests", []) if isinstance(doc, dict) else []
        except (ValueError, TypeError):
            children = []
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            digest = child.get("digest")
            if not isinstance(digest, str):
                continue
            for match in by_digest.get((*key[:2], digest), ()):
                if match not in result:
                    result.add(match)
                    pending.append(match)
    return result


def protected_keys_locked(store, candidates, rows=None):
    rows = snapshot_locked(store) if rows is None else rows
    pins = set(store.db.execute("SELECT upstream,repo,reference FROM cleanup_pins"))
    # Pin all aliases of a pinned digest, and its platform manifests.
    digests = {(*key[:2], rows[key]["digest"]) for key in pins if key in rows}
    pins |= {key for key in rows if (*key[:2], rows[key]["digest"]) in digests}
    protected = expand_keys(rows, pins)
    # A retained index must never lose its cached platform images/layers.
    protected |= expand_keys(rows, set(rows) - candidates) & candidates
    return protected


def validate_targets(targets):
    if not isinstance(targets, list) or len(targets) > MAX_TARGETS:
        raise ValueError(f"targets must be a list of at most {MAX_TARGETS} entries")
    result = []
    for target in targets:
        if (not isinstance(target, list) or len(target) != 4
                or not all(isinstance(v, str) and v and len(v) <= 2048 for v in target)):
            raise ValueError("each target must contain upstream, repository, reference and digest")
        parse_digest(target[3])
        result.append(tuple(target))
    return list(dict.fromkeys(result))


def validate_policy(value):
    fields = {"manifest_max_idle_days", "disk_high_percent", "disk_low_percent", "max_cache_bytes"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("provide all cleanup policy fields")
    for name, number in value.items():
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or number < 0):
            raise ValueError(f"invalid {name}")
    if (value["disk_high_percent"] > 100 or value["disk_low_percent"] >= 100
            or (value["disk_high_percent"] and value["disk_low_percent"] >= value["disk_high_percent"])):
        raise ValueError("disk thresholds must satisfy 0 <= low < high <= 100 (high=0 disables)")
    if int(value["max_cache_bytes"]) != value["max_cache_bytes"]:
        raise ValueError("max_cache_bytes must be an integer")
    return dict(value)


class Cleanup:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def policy(self):
        saved = self.store.get_meta("cleanup_policy")
        return validate_policy(json.loads(saved)) if saved else validate_policy({
            "manifest_max_idle_days": self.config.gc_manifest_max_idle / 86400,
            "disk_high_percent": self.config.gc_disk_high_percent,
            "disk_low_percent": self.config.gc_disk_low_percent,
            "max_cache_bytes": self.config.gc_max_cache_bytes,
        })

    def set_policy(self, value):
        value = validate_policy(value)
        self.store.set_meta("cleanup_policy", json.dumps(value))
        return value

    def pin(self, key, enabled):
        if not isinstance(key, list) or len(key) != 3 or not all(isinstance(v, str) for v in key):
            raise ValueError("target must be [upstream, repository, reference]")
        if not isinstance(enabled, bool):
            raise ValueError("pinned must be true or false")
        with self.store._lock:
            if enabled and tuple(key) not in snapshot_locked(self.store):
                raise ValueError("target is no longer cached")
            if enabled:
                self.store.db.execute("INSERT OR IGNORE INTO cleanup_pins VALUES (?,?,?)", key)
            else:
                self.store.db.execute("DELETE FROM cleanup_pins WHERE upstream=? AND repo=? AND reference=?", key)
            self.store.db.commit()

    def _estimate_locked(self, keys, scoped=False):
        refs = {}
        for up, repo, ref, digest in self.store.db.execute("SELECT * FROM manifest_blob_refs"):
            refs.setdefault(digest, set()).add((up, repo, ref))
        cutoff = time.time() - self.config.gc_blob_grace
        potential = eligible = 0
        for digest, size, stored in self.store.db.execute("SELECT digest,size,stored_at FROM blobs"):
            if scoped and not (refs.get(digest, set()) & keys):
                continue
            if not (refs.get(digest, set()) - keys):
                potential += size
                if stored < cutoff:
                    eligible += size
        return {"potential_bytes": potential, "reclaimable_bytes": eligible}

    def overview(self, offset=0):
        with self.store._lock:
            rows = snapshot_locked(self.store)
            pins = set(self.store.db.execute("SELECT upstream,repo,reference FROM cleanup_pins"))
            protected = protected_keys_locked(self.store, set(rows), rows)
            entries = [{"target": list(key), "digest": row["digest"], "last_used": row["last_used"],
                        "pinned": key in pins, "protected": key in protected,
                        "pushed": key[0] == LOCAL, "public": self.store.shareable_repo(*key[:2]) and key[0] != LOCAL}
                       for key, row in sorted(rows.items())[offset:offset + MAX_TARGETS]]
        disk = shutil.disk_usage(self.store.root)
        return {"role": self.config.role, "entries": entries, "total": len(rows), "offset": offset,
                "policy": self.policy(), "gc_interval": self.config.gc_interval,
                "blob_grace": self.config.gc_blob_grace, "cache": self.store.blob_stats(),
                "disk": {"total": disk.total, "used": disk.used, "free": disk.free,
                         "used_percent": 100 * disk.used / disk.total if disk.total else 0},
                "last_run": json.loads(self.store.get_meta("cleanup_last_run") or "null"),
                "jobs": self.jobs()}

    def preview(self, request):
        scope = request.get("scope", "local")
        if scope not in ("local", "public"):
            raise ValueError("scope must be local or public")
        if scope == "public" and not self.config.is_primary:
            raise ValueError("only the primary can request shared-public cleanup")
        targets = request.get("targets")
        if (not isinstance(targets, list) or len(targets) > MAX_TARGETS
                or any(not isinstance(k, list) or len(k) != 3
                       or not all(isinstance(v, str) for v in k) for k in targets)):
            raise ValueError("select cached targets before previewing cleanup")
        include_pushed = request.get("include_pushed", False)
        if not isinstance(include_pushed, bool):
            raise ValueError("include_pushed must be true or false")
        with self.store._lock:
            rows = snapshot_locked(self.store)
            keys = expand_keys(rows, {tuple(k) for k in targets}) & set(rows)
            if len(keys) > MAX_TARGETS:
                raise ValueError("selection includes too many platform manifests; select fewer images")
            protected = protected_keys_locked(self.store, keys, rows)
            blocked = {k for k in keys if k in protected or (k[0] == LOCAL and not include_pushed)
                       or (scope == "public" and (k[0] == LOCAL or not self.store.shareable_repo(*k[:2])))}
            keys -= blocked
            payload = {"scope": scope, "targets": [[*key, rows[key]["digest"]] for key in sorted(keys)]}
            token = uuid.uuid4().hex
            now = time.time()
            self.store.db.execute("DELETE FROM cleanup_previews WHERE created_at<?", (now - PREVIEW_TTL,))
            self.store.db.execute("INSERT INTO cleanup_previews VALUES (?,?,?)", (token, now, json.dumps(payload)))
            self.store.db.commit()
            return {"preview_id": token, "scope": scope, "count": len(keys), "blocked": len(blocked),
                    "targets": payload["targets"], "expires_in": PREVIEW_TTL,
                    "contains_pushed": any(k[0] == LOCAL for k in keys), **self._estimate_locked(keys, scope == "public"),
                    "note": "Replica estimates differ; pins, changed content and shared layers are rechecked. Blobs obey the grace period."}

    def _remove_locked(self, targets, public):
        rows = snapshot_locked(self.store)
        wanted = {tuple(t[:3]): t[3] for t in targets}
        candidates = {k for k, digest in wanted.items() if k in rows and rows[k]["digest"] == digest}
        protected = protected_keys_locked(self.store, candidates, rows)
        result = {"deleted": 0, "protected": 0, "changed": 0, "missing": 0, "blobs": 0, "bytes": 0}
        for key, digest in wanted.items():
            if public and (key[0] == LOCAL or not self.store.shareable_repo(*key[:2])):
                result["protected"] += 1
            elif key not in rows:
                result["missing"] += 1
            elif rows[key]["digest"] != digest:
                result["changed"] += 1
            elif key in protected:
                result["protected"] += 1
            else:
                self.store.db.execute("DELETE FROM manifests WHERE upstream=? AND repo=? AND reference=?", key)
                self.store.db.execute("DELETE FROM changelog WHERE kind='manifest' AND upstream=? AND repo=? AND reference=?", key)
                result["deleted"] += 1
        return result

    def _affected_locked(self, targets):
        keys = {tuple(t[:3]) for t in targets if t[0] != LOCAL and self.store.shareable_repo(t[0], t[1])}
        return {digest for up, repo, ref, digest in self.store.db.execute("SELECT * FROM manifest_blob_refs")
                if (up, repo, ref) in keys}

    def _collect_locked(self, result, allowed=None):
        gc = self.store.gc(0, self.config.gc_blob_grace, allowed_digests=allowed)
        result.update(blobs=gc.blobs, bytes=gc.bytes)
        return result

    def execute(self, token):
        if not isinstance(token, str):
            raise ValueError("preview_id is required")
        with self.store._lock:
            row = self.store.db.execute("SELECT created_at,payload FROM cleanup_previews WHERE id=?", (token,)).fetchone()
            if not row or time.time() - row[0] > PREVIEW_TTL:
                raise ValueError("preview expired or already used; preview again")
            payload = json.loads(row[1])
            targets = validate_targets(payload["targets"])
            public = payload["scope"] == "public"
            if public and not self.config.is_primary:
                raise ValueError("shared-public cleanup must execute on the primary")
            affected = self._affected_locked(targets) if public else None
            result = self._remove_locked(targets, public)
            job = uuid.uuid4().hex
            payload = {"job_id": job, "targets": [list(t) for t in targets]}
            peers = list(self.config.replication_peers) if public and targets else []
            self.store.db.execute("DELETE FROM cleanup_previews WHERE id=?", (token,))
            self.store.db.execute("INSERT INTO cleanup_jobs VALUES (?,?,?,?,?,?)",
                (job, time.time(), "public" if public else "local", json.dumps(payload), json.dumps(result), json.dumps(peers)))
            if public and targets:
                body = json.dumps(payload).encode()
                self.store.db.execute("INSERT INTO changelog(kind,digest,bytes) VALUES ('cleanup',?,?)",
                                      ('sha256:' + hashlib.sha256(body).hexdigest(), body))
            self.store.db.commit()
            self._collect_locked(result, affected)
            self.store.db.execute("UPDATE cleanup_jobs SET result=? WHERE id=?", (json.dumps(result), job))
            self.store.db.commit()
        self.store._notify()
        return {"job_id": job, "result": result}

    def apply_public(self, payload):
        targets = validate_targets(payload.get("targets"))
        job = payload.get("job_id")
        if not isinstance(job, str) or len(job) != 32 or any(c not in '0123456789abcdef' for c in job):
            raise ValueError("invalid public cleanup ID")
        with self.store._lock:
            previous = self.store.db.execute("SELECT result FROM cleanup_jobs WHERE id=? AND scope='received'", (job,)).fetchone()
            if previous:
                return json.loads(previous[0])
            # Every target is checked against this node's current public policy.
            affected = self._affected_locked(targets)
            result = self._remove_locked(targets, True)
            self.store.db.execute("INSERT INTO cleanup_jobs VALUES (?,?,?,?,?,?)",
                (job, time.time(), "received", json.dumps(payload), json.dumps(result), '[]'))
            self.store.db.commit()
            self._collect_locked(result, affected)
            self.store.db.execute("UPDATE cleanup_jobs SET result=? WHERE id=?", (json.dumps(result), job))
            self.store.db.commit()
            return result

    def acknowledge(self, job, peer, result):
        if (not isinstance(result, dict) or set(result) != {"deleted", "protected", "changed", "missing", "blobs", "bytes"}
                or any(type(v) is not int or v < 0 or v > 2**63 - 1 for v in result.values())):
            raise ValueError("invalid cleanup result")
        with self.store._lock:
            row = self.store.db.execute("SELECT peers FROM cleanup_jobs WHERE id=? AND scope='public'", (job,)).fetchone()
            if not row or peer not in json.loads(row[0]):
                raise ValueError("unknown cleanup request for this peer")
            self.store.db.execute("INSERT OR REPLACE INTO cleanup_acks VALUES (?,?,?)", (job, peer, json.dumps(result)))
            self.store.db.commit()

    def jobs(self):
        with self.store._lock:
            jobs = []
            online = {row[0] for row in self.store.live_replicas()}
            for job, created, scope, result, peers in self.store.db.execute(
                    "SELECT id,created_at,scope,result,peers FROM cleanup_jobs ORDER BY created_at DESC LIMIT 50"):
                acks = {p: json.loads(r) for p, r in self.store.db.execute(
                    "SELECT peer_id,result FROM cleanup_acks WHERE job_id=?", (job,))}
                statuses = {p: {"status": ("completed_with_skips" if acks[p]["protected"] or acks[p]["changed"] else "completed"),
                                "result": acks[p], "online": p in online} if p in acks else
                            {"status": "pending", "online": p in online} for p in json.loads(peers)}
                jobs.append({"id": job, "created_at": created, "scope": scope,
                             "result": json.loads(result), "replicas": statuses})
            return jobs

    def run(self):
        """Apply local retention/LRU policy and collect unreferenced, aged blobs."""
        policy = self.policy()
        disk = shutil.disk_usage(self.store.root)
        with self.store._lock:
            rows = snapshot_locked(self.store)
            candidates = {k for k in rows if k[0] != LOCAL}
            candidates -= protected_keys_locked(self.store, candidates, rows)
            cutoff = time.time() - policy["manifest_max_idle_days"] * 86400
            selected = {k for k in candidates if policy["manifest_max_idle_days"] and rows[k]["last_used"] < cutoff}
            cache_bytes = self.store.db.execute("SELECT COALESCE(SUM(size),0) FROM blobs").fetchone()[0]
            need = max(0, cache_bytes - policy["max_cache_bytes"]) if policy["max_cache_bytes"] else 0
            if policy["disk_high_percent"] and disk.total and 100 * disk.used / disk.total >= policy["disk_high_percent"]:
                need = max(need, disk.used - int(disk.total * policy["disk_low_percent"] / 100))
            # Remove aliases together so digest entries do not keep old layers alive.
            groups = {}
            for key in candidates:
                groups.setdefault((*key[:2], rows[key]["digest"]), []).append(key)
            for group in sorted(groups.values(), key=lambda keys: max(rows[k]["last_used"] for k in keys)):
                if self._estimate_locked(selected)["reclaimable_bytes"] >= need:
                    break
                proposed = selected | (expand_keys(rows, group) & candidates)
                proposed -= protected_keys_locked(self.store, proposed, rows)
                selected = proposed
            targets = [[*k, rows[k]["digest"]] for k in selected]
            result = self._remove_locked(targets, False)
            self.store.db.commit()
            self._collect_locked(result)
            remaining = self.store.db.execute("SELECT COALESCE(SUM(size),0) FROM blobs").fetchone()[0]
        after = shutil.disk_usage(self.store.root)
        result["pressure_remaining"] = bool((policy["max_cache_bytes"] and remaining > policy["max_cache_bytes"])
            or (policy["disk_high_percent"] and after.total and 100 * after.used / after.total >= policy["disk_high_percent"]))
        self.store.set_meta("cleanup_last_run", json.dumps({"at": time.time(), **result}))
        return result
