"""Node-local SQLite accounts/sources and content-addressed cache.

Only explicitly shared public catalog, manifest and referenced-blob changes enter
the public replication feed. Visibility and ownership are checked again when
serving events or content. Users, tokens and private sources are local only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ociproxy.store")

# Namespace of images pushed to the primary (vs. an upstream mirror's name).
LOCAL = "local"

# Manifest-table namespace prefix of classic Helm repos: "helm/<repo name>".
# Upstream names cannot contain "/", so this never collides with a mirror.
HELM_NS = "helm/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS blob_grants (
    upstream TEXT NOT NULL,
    repo TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(upstream, repo, digest)
);
CREATE INDEX IF NOT EXISTS idx_blob_grants_digest ON blob_grants(digest);
CREATE TABLE IF NOT EXISTS blobs (
    digest    TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    stored_at REAL NOT NULL                -- refreshed on reuse (GC grace)
);
CREATE TABLE IF NOT EXISTS manifests (
    upstream    TEXT NOT NULL,
    repo        TEXT NOT NULL,
    reference   TEXT NOT NULL,
    digest      TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    bytes       BLOB NOT NULL,
    stored_at   REAL NOT NULL,
    checked_at  REAL NOT NULL DEFAULT 0,  -- last upstream revalidation (tags)
    pulled_at   REAL NOT NULL DEFAULT 0,  -- last served (GC idle tracking)
    PRIMARY KEY (upstream, repo, reference)
);
CREATE INDEX IF NOT EXISTS idx_manifests_stored_at ON manifests (stored_at);
CREATE INDEX IF NOT EXISTS idx_manifests_repo ON manifests (repo);
CREATE TABLE IF NOT EXISTS manifest_blob_refs (
    upstream TEXT NOT NULL,
    repo TEXT NOT NULL,
    reference TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(upstream, repo, reference, digest)
);
CREATE INDEX IF NOT EXISTS idx_manifest_blob_refs_digest ON manifest_blob_refs(digest);
CREATE TRIGGER IF NOT EXISTS remove_manifest_blob_refs AFTER DELETE ON manifests
BEGIN
    DELETE FROM manifest_blob_refs WHERE upstream=OLD.upstream AND repo=OLD.repo AND reference=OLD.reference;
END;

CREATE TABLE IF NOT EXISTS changelog (
    serial    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,              -- 'blob' | 'manifest' | 'user' | 'token' | 'source'
    upstream  TEXT,
    repo      TEXT,
    reference TEXT,
    digest    TEXT NOT NULL,
    bytes     BLOB,                      -- manifest bytes only
    media_type TEXT                      -- manifest media type only
);
CREATE INDEX IF NOT EXISTS idx_changelog_manifest
    ON changelog (upstream, repo, reference, serial);
CREATE INDEX IF NOT EXISTS idx_changelog_digest ON changelog (kind, digest);
CREATE TABLE IF NOT EXISTS replicas (
    node_id     TEXT PRIMARY KEY,
    base_url    TEXT NOT NULL,
    last_seen   REAL NOT NULL,
    acked_serial INTEGER NOT NULL DEFAULT 0,
    last_pull_at REAL NOT NULL DEFAULT 0
);
-- Classic Helm repos (primary only): where each chart version listed in the
-- repo's index.yaml is downloaded from. Replaced wholesale per index update.
CREATE TABLE IF NOT EXISTS helm_sources (
    repo     TEXT NOT NULL,
    name     TEXT NOT NULL,
    version  TEXT NOT NULL,
    url      TEXT NOT NULL,
    digest   TEXT,                       -- 'sha256:<hex>' if the index lists one
    PRIMARY KEY (repo, name, version)
);
-- Accounts. password_hash is NULL for OIDC users (they use tokens).
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    role          TEXT NOT NULL,         -- 'reader' | 'pusher' | 'admin'
    source        TEXT NOT NULL,         -- 'local' | 'oidc'
    password_hash TEXT,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
-- Personal access tokens; only a hash of the secret part is stored.
CREATE TABLE IF NOT EXISTS tokens (
    id           TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    name         TEXT NOT NULL,
    secret_hash  TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL,                   -- NULL = never
    last_used_at REAL NOT NULL DEFAULT 0 -- this node only; not replicated
);
CREATE INDEX IF NOT EXISTS idx_tokens_username ON tokens (username);
-- Upstreams / Helm repos managed in the web UI (config-file ones are not
-- stored). credentials is encrypted and only ever set on the primary.
CREATE TABLE IF NOT EXISTS sources (
    kind        TEXT NOT NULL,           -- 'oci' | 'helm'
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL,           -- JSON, without credentials
    credentials TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (kind, name)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the initial schema: (table, column, definition).
MIGRATIONS = [
    ("replicas", "last_pull_at", "REAL NOT NULL DEFAULT 0"),
    ("manifests", "checked_at", "REAL NOT NULL DEFAULT 0"),
    ("manifests", "pulled_at", "REAL NOT NULL DEFAULT 0"),
]

# pulled_at is only rewritten when older than this, so hot tags don't turn
# every pull into a database write.
PULLED_AT_RESOLUTION = 3600.0

ACCOUNT_KINDS = ("user", "token")
SOURCE_KIND = "source"
SOURCE_TYPES = ("oci", "helm")
# The primary's config-file sources, as one 'source' event (no "/" in the
# key, so it can't clash with "<kind>/<name>") and a meta row.
CONFIG_SOURCES_KEY = "config"
CONFIG_SOURCES_META = "config_sources"
_USER_COLS = ("username", "role", "source", "password_hash", "disabled", "created_at", "updated_at")
_TOKEN_COLS = ("id", "username", "name", "secret_hash", "created_at", "expires_at")


def parse_digest(digest: str) -> tuple[str, str]:
    """'sha256:<hex>' -> ('sha256', '<hex>'). Raises ValueError otherwise.

    Only sha256 is accepted: it is the digest every registry and client uses,
    and the only one the store can verify.
    """
    algo, _, hexpart = digest.partition(":")
    if algo != "sha256" or len(hexpart) != 64:
        raise ValueError(f"invalid digest: {digest!r}")
    if any(c not in "0123456789abcdef" for c in hexpart):
        raise ValueError(f"invalid digest: {digest!r}")
    return algo, hexpart


def referenced_blobs(manifest_bytes: bytes) -> list[str]:
    """Blob digests (config + layers) referenced by an image manifest.

    Indexes / manifest lists reference child manifests, not blobs, and
    unparseable documents reference nothing.
    """
    try:
        doc = json.loads(manifest_bytes)
    except (ValueError, TypeError):
        return []
    if not isinstance(doc, dict):
        return []
    blobs: list[str] = []
    config = doc.get("config")
    if isinstance(config, dict) and isinstance(config.get("digest"), str):
        blobs.append(config["digest"])
    layers = doc.get("layers")
    if isinstance(layers, list):
        for layer in layers:
            if isinstance(layer, dict) and isinstance(layer.get("digest"), str):
                blobs.append(layer["digest"])
    return blobs


@dataclass
class ManifestRecord:
    upstream: str
    repo: str
    reference: str
    digest: str
    media_type: str
    bytes: bytes
    # When this entry was last known to match its source (stored or
    # revalidated). Only meaningful for tag references.
    checked_at: float = 0.0
    pulled_at: float = 0.0


@dataclass
class ChangeEvent:
    serial: int
    kind: str  # 'blob' | 'manifest'
    digest: str
    upstream: str | None = None
    repo: str | None = None
    reference: str | None = None
    bytes: bytes | None = None
    media_type: str | None = None


@dataclass
class GCResult:
    manifests: int = 0
    blobs: int = 0
    bytes: int = 0


_MANIFEST_COLS = (
    "upstream, repo, reference, digest, media_type, bytes, "
    "MAX(stored_at, checked_at), pulled_at"
)


class Store:
    """Thread-safe (one connection behind a lock; WAL).

    Reads are cheap index lookups and are called directly from the event
    loop; writes that commit are called through asyncio.to_thread by async
    callers.
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.tmp_dir = self.root / "tmp"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_tmp()
        self._lock = threading.Lock()
        self.shareable_repo = lambda upstream, repo: True
        self.replication_enabled = True
        self._listeners: list[Callable[[], None]] = []
        self._account_listeners: list[Callable[[], None]] = []
        self._source_listeners: list[Callable[[], None]] = []
        self.db = sqlite3.connect(str(self.root / "index.db"), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        for table, column, ddl in MIGRATIONS:
            exists = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _sweep_tmp(self) -> None:
        """Delete temp files left by a crash (nothing is in flight at open)."""
        removed = 0
        for p in self.tmp_dir.iterdir():
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            log.info("removed %d stale temp file(s) from %s", removed, self.tmp_dir)

    def close(self) -> None:
        self.db.close()

    # -- change notification -------------------------------------------------

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Called (from any thread) after each new changelog event."""
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in self._listeners:
            fn()

    def add_account_listener(self, fn: Callable[[], None]) -> None:
        """Called (from any thread) after any user/token change."""
        self._account_listeners.append(fn)

    def _notify_accounts(self) -> None:
        for fn in self._account_listeners:
            fn()

    def add_source_listener(self, fn: Callable[[], None]) -> None:
        """Called (from any thread) after any managed source change."""
        self._source_listeners.append(fn)

    def _notify_sources(self) -> None:
        for fn in self._source_listeners:
            fn()

    # -- meta ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.db.commit()

    # -- blobs ---------------------------------------------------------------

    def has_blob(self, digest: str) -> bool:
        with self._lock:
            row = self.db.execute("SELECT 1 FROM blobs WHERE digest = ?", (digest,)).fetchone()
        if row is None:
            return False
        return self.blob_path(digest).exists()

    def blob_path(self, digest: str) -> Path:
        algo, hexpart = parse_digest(digest)
        return self.blobs_dir / algo / hexpart

    def new_blob_tmp(self) -> tuple[Path, HashingWriter]:
        path = self.tmp_dir / f"blob-{uuid.uuid4().hex}"
        return path, HashingWriter(path)

    def commit_blob(self, tmp_path: Path, digest: str, size: int, *, origin: tuple | None = None) -> bool:
        """Move a fully-written, digest-verified temp file into the store.

        Returns True if the blob is new. A blob that was already indexed
        (concurrent fetch, re-push) gets no second changelog event.
        """
        algo, hexpart = parse_digest(digest)
        final = self.blobs_dir / algo / hexpart
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, final)
        with self._lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO blobs (digest, size, stored_at) VALUES (?, ?, ?)",
                (digest, size, time.time()),
            )
            new = cur.rowcount == 1
            if origin:
                self.db.execute("INSERT OR IGNORE INTO blob_grants VALUES (?,?,?)", (*origin, digest))
            if ((new or origin) and self.replication_enabled and self._public_blob_locked(digest)
                and not self.db.execute("SELECT 1 FROM changelog WHERE kind='blob' AND digest=?", (digest,)).fetchone()):
                self.db.execute(
                    "INSERT INTO changelog (kind, digest) VALUES ('blob', ?)", (digest,)
                )
            if not new:
                self.db.execute(
                    "UPDATE blobs SET stored_at = ? WHERE digest = ?", (time.time(), digest)
                )
            self.db.commit()
        if new or origin:
            self._notify()
        return new

    def touch_blob(self, digest: str) -> None:
        """Mark an existing blob as in use (restarts its GC grace period).

        Called when a pusher finds the blob already present and skips the
        upload, so it cannot be collected before the manifest arrives.
        """
        with self._lock:
            self.db.execute(
                "UPDATE blobs SET stored_at = ? WHERE digest = ?", (time.time(), digest)
            )
            self.db.commit()

    # -- manifests -----------------------------------------------------------

    def get_manifest(self, upstream: str, repo: str, reference: str) -> ManifestRecord | None:
        with self._lock:
            row = self.db.execute(
                f"SELECT {_MANIFEST_COLS} FROM manifests "
                "WHERE upstream=? AND repo=? AND reference=?",
                (upstream, repo, reference),
            ).fetchone()
        if row is None:
            return None
        return ManifestRecord(*row)

    def initialize_isolation(self) -> None:
        """Fail closed for legacy databases; migration is explicit and offline."""
        if self.get_meta("node_isolation_schema") == "2":
            return
        with self._lock:
            legacy = any(self.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                         for table in ("users", "tokens", "sources", "manifests", "changelog", "replicas"))
        if legacy:
            raise ValueError("legacy database requires offline isolation migration; see docs/ISOLATION.md")
        self.set_meta("node_isolation_schema", "2")

    def rebuild_public_log(self) -> None:
        """Seed migrated public cache history after the sanitized catalog exists."""
        with self._lock:
            rows = self.db.execute("SELECT upstream, repo, reference, digest, media_type, bytes FROM manifests").fetchall()
            for upstream, repo, ref, digest, media, body in rows:
                self.db.executemany("INSERT OR IGNORE INTO manifest_blob_refs VALUES (?,?,?,?)",
                    [(upstream,repo,ref,blob) for blob in referenced_blobs(body)])
                if self.shareable_repo(upstream, repo):
                    self.db.execute("INSERT INTO changelog(kind,upstream,repo,reference,digest,media_type,bytes) VALUES ('manifest',?,?,?,?,?,?)",
                                    (upstream,repo,ref,digest,media,body))
                    for blob in referenced_blobs(body):
                        if self.db.execute("SELECT 1 FROM blobs WHERE digest=?",(blob,)).fetchone():
                            self.db.execute("INSERT INTO changelog(kind,digest) VALUES ('blob',?)",(blob,))
            self.db.execute("DELETE FROM meta WHERE key='public_log_rebuild'")
            self.db.commit()
        self._notify()

    def _public_blob_locked(self, digest: str) -> bool:
        for upstream, repo in self.db.execute("SELECT upstream, repo FROM blob_grants WHERE digest=?", (digest,)):
            if (upstream == "@public-feed" and not self.replication_enabled) or self.shareable_repo(upstream, repo):
                return True
        return False

    def public_blob(self, digest: str) -> bool:
        with self._lock:
            return self._public_blob_locked(digest)

    def public_events(self, since: int, limit: int = 500) -> tuple[list, int]:
        """Scan a bounded batch and advance past excluded events too."""
        events = self.changelog_since(since, limit=limit)
        scanned = events[-1].serial if events else self.current_serial()
        return [event for event in events if self.event_is_public(event)], scanned

    def event_is_public(self, event) -> bool:
        if event.kind == "manifest":
            return self.shareable_repo(event.upstream, event.repo)
        if event.kind == "blob":
            return self.public_blob(event.digest)
        if event.kind == SOURCE_KIND and event.reference == CONFIG_SOURCES_KEY:
            current = self.config_sources()
            return bool(current and event.bytes and json.loads(event.bytes) == current)
        return False

    def drop_namespace(self, namespace: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM manifests WHERE upstream=?", (namespace,))
            self.db.execute("DELETE FROM blob_grants WHERE upstream=?", (namespace,))
            self.db.execute("DELETE FROM changelog WHERE upstream=?", (namespace,))
            self.db.commit()

    def has_blob_grant(self, upstream: str, repo: str, digest: str) -> bool:
        with self._lock:
            return bool(self.db.execute("SELECT 1 FROM blob_grants WHERE upstream=? AND repo=? AND digest=?",
                                       (upstream, repo, digest)).fetchone())

    def repo_references_blob(self, upstream: str, repo: str, digest: str) -> bool:
        """A manifest reference or upstream-confirmed repository ownership."""
        with self._lock:
            if self.db.execute("SELECT 1 FROM blob_grants WHERE upstream=? AND repo=? AND digest=?",
                               (upstream, repo, digest)).fetchone():
                return True
            if self.db.execute("SELECT 1 FROM manifests WHERE upstream=? AND repo=? AND digest=?",
                               (upstream, repo, digest)).fetchone():
                return True
            return bool(self.db.execute("SELECT 1 FROM manifest_blob_refs WHERE upstream=? AND repo=? AND digest=?",
                                        (upstream, repo, digest)).fetchone())

    def get_manifest_by_digest(self, upstream: str, repo: str, digest: str) -> ManifestRecord | None:
        with self._lock:
            row = self.db.execute(
                f"SELECT {_MANIFEST_COLS} FROM manifests "
                "WHERE upstream=? AND repo=? AND digest=? LIMIT 1",
                (upstream, repo, digest),
            ).fetchone()
        if row is None:
            return None
        return ManifestRecord(*row)

    def put_manifest(
        self, upstream: str, repo: str, reference: str, digest: str,
        media_type: str, manifest_bytes: bytes,
    ) -> None:
        """Store current-state manifest AND append the changelog event atomically."""
        now = time.time()
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO manifests "
                "(upstream, repo, reference, digest, media_type, bytes, stored_at, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (upstream, repo, reference, digest, media_type, manifest_bytes, now, now),
            )
            self.db.execute("DELETE FROM manifest_blob_refs WHERE upstream=? AND repo=? AND reference=?", (upstream, repo, reference))
            self.db.executemany("INSERT OR IGNORE INTO manifest_blob_refs VALUES (?,?,?,?)",
                [(upstream,repo,reference,blob) for blob in referenced_blobs(manifest_bytes)])
            if self.replication_enabled and self.shareable_repo(upstream, repo):
                self.db.execute(
                    "INSERT INTO changelog (kind, upstream, repo, reference, digest, bytes, media_type) "
                    "VALUES ('manifest', ?, ?, ?, ?, ?, ?)",
                    (upstream, repo, reference, digest, manifest_bytes, media_type),
                )
                # A previously private/unreferenced layer becomes shareable only
                # after a public manifest explicitly references it.
                for blob in referenced_blobs(manifest_bytes):
                    if (self.db.execute("SELECT 1 FROM blobs WHERE digest=?", (blob,)).fetchone()
                        and self._public_blob_locked(blob)
                        and not self.db.execute("SELECT 1 FROM changelog WHERE kind='blob' AND digest=?", (blob,)).fetchone()):
                        self.db.execute("INSERT INTO changelog (kind, digest) VALUES ('blob', ?)", (blob,))
            self.db.commit()
        self._notify()

    def touch_manifest(self, upstream: str, repo: str, reference: str) -> None:
        """Mark a tag as revalidated (source still has the same digest).

        Not a content change, so no changelog event.
        """
        with self._lock:
            self.db.execute(
                "UPDATE manifests SET checked_at = ? WHERE upstream=? AND repo=? AND reference=?",
                (time.time(), upstream, repo, reference),
            )
            self.db.commit()

    def mark_pulled(self, rec: ManifestRecord) -> None:
        """Record that a manifest was served (GC idle tracking), throttled."""
        now = time.time()
        if now - rec.pulled_at < PULLED_AT_RESOLUTION:
            return
        with self._lock:
            self.db.execute(
                "UPDATE manifests SET pulled_at = ? WHERE upstream=? AND repo=? AND reference=?",
                (now, rec.upstream, rec.repo, rec.reference),
            )
            self.db.commit()
        rec.pulled_at = now

    def manifest_digest(self, upstream: str, repo: str, reference: str) -> str | None:
        with self._lock:
            row = self.db.execute(
                "SELECT digest FROM manifests WHERE upstream=? AND repo=? AND reference=?",
                (upstream, repo, reference),
            ).fetchone()
        return row[0] if row else None

    def manifest_exists(self, upstream: str, repo: str, reference: str) -> bool:
        return self.manifest_digest(upstream, repo, reference) is not None

    def list_tags(self, upstream: str, repo: str, last: str | None, n: int | None) -> list[str] | None:
        """Tags (non-digest references) of a repo, sorted; None if the repo is unknown."""
        with self._lock:
            known = self.db.execute(
                "SELECT 1 FROM manifests WHERE upstream=? AND repo=? LIMIT 1", (upstream, repo)
            ).fetchone()
            if known is None:
                return None
            q = ("SELECT reference FROM manifests WHERE upstream=? AND repo=? "
                 "AND instr(reference, ':') = 0")
            args: list = [upstream, repo]
            if last:
                q += " AND reference > ?"
                args.append(last)
            q += " ORDER BY reference"
            if n is not None:
                q += " LIMIT ?"
                args.append(n)
            return [r[0] for r in self.db.execute(q, args).fetchall()]

    # -- accounts --------------------------------------------------------------

    def _log_account(self, kind: str, key: str, payload: dict) -> None:
        """Append an account changelog event. Caller holds the lock."""
        # Account hashes, tokens and local source definitions never enter a feed.
        return

    def _after_account_change(self, logged: bool) -> None:
        if logged:
            self._notify()
        self._notify_accounts()

    def get_user(self, username: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                f"SELECT {', '.join(_USER_COLS)} FROM users WHERE username = ?", (username,)
            ).fetchone()
        return self._user_row(row) if row else None

    @staticmethod
    def _user_row(row) -> dict:
        d = dict(zip(_USER_COLS, row))
        d["disabled"] = bool(d["disabled"])
        return d

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                f"SELECT {', '.join(_USER_COLS)} FROM users ORDER BY username"
            ).fetchall()
        return [self._user_row(r) for r in rows]

    def put_user(self, user: dict, *, log: bool = True) -> None:
        """Insert or replace a user (all of _USER_COLS)."""
        row = {c: user.get(c) for c in _USER_COLS}
        row["disabled"] = bool(row["disabled"])
        with self._lock:
            self.db.execute(
                f"INSERT OR REPLACE INTO users ({', '.join(_USER_COLS)}) "
                f"VALUES ({', '.join('?' * len(_USER_COLS))})",
                tuple(int(v) if isinstance(v, bool) else v for v in row.values()),
            )
            if log:
                self._log_account("user", row["username"], {"user": row})
            self.db.commit()
        self._after_account_change(log)

    def delete_user(self, username: str, *, log: bool = True) -> bool:
        """Delete a user and all of their tokens."""
        with self._lock:
            n = self.db.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount
            self.db.execute("DELETE FROM tokens WHERE username = ?", (username,))
            if n and log:
                self._log_account("user", username, {"deleted": True})
            self.db.commit()
        if n:
            self._after_account_change(log)
        return bool(n)

    def get_token(self, token_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                f"SELECT {', '.join(_TOKEN_COLS)}, last_used_at FROM tokens WHERE id = ?",
                (token_id,),
            ).fetchone()
        return dict(zip((*_TOKEN_COLS, "last_used_at"), row)) if row else None

    def list_tokens(self, username: str | None = None) -> list[dict]:
        q = f"SELECT {', '.join(_TOKEN_COLS)}, last_used_at FROM tokens"
        args: tuple = ()
        if username is not None:
            q += " WHERE username = ?"
            args = (username,)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY created_at", args).fetchall()
        return [dict(zip((*_TOKEN_COLS, "last_used_at"), r)) for r in rows]

    def put_token(self, token: dict, *, log: bool = True) -> None:
        row = {c: token.get(c) for c in _TOKEN_COLS}
        with self._lock:
            self.db.execute(
                f"INSERT OR REPLACE INTO tokens ({', '.join(_TOKEN_COLS)}) "
                f"VALUES ({', '.join('?' * len(_TOKEN_COLS))})",
                tuple(row.values()),
            )
            if log:
                self._log_account("token", row["id"], {"token": row})
            self.db.commit()
        self._after_account_change(log)

    def delete_token(self, token_id: str, *, log: bool = True) -> bool:
        with self._lock:
            n = self.db.execute("DELETE FROM tokens WHERE id = ?", (token_id,)).rowcount
            if n and log:
                self._log_account("token", token_id, {"deleted": True})
            self.db.commit()
        if n:
            self._after_account_change(log)
        return bool(n)

    def touch_token(self, token_id: str, last_used_at: float) -> None:
        """Record token use (throttled by the caller; local to this node)."""
        with self._lock:
            self.db.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?",
                            (last_used_at, token_id))
            self.db.commit()

    def apply_account_event(self, kind: str, key: str, body: bytes) -> None:
        raise ValueError("account replication is disabled")

    def list_sources(self) -> list[dict]:
        """Every managed source: {kind, name, spec, credentials, created_at, updated_at}."""
        with self._lock:
            rows = self.db.execute(
                "SELECT kind, name, spec, credentials, created_at, updated_at "
                "FROM sources ORDER BY kind, name"
            ).fetchall()
        return [{"kind": r[0], "name": r[1], "spec": json.loads(r[2]), "credentials": r[3],
                 "created_at": r[4], "updated_at": r[5]} for r in rows]

    def get_source(self, kind: str, name: str) -> dict | None:
        return next((s for s in self.list_sources()
                     if s["kind"] == kind and s["name"] == name), None)

    def put_source(self, kind: str, name: str, spec: dict, credentials: str | None, *,
                   log: bool = True, expected_owner: str | None = None) -> None:
        """Insert or replace a managed source. ``spec`` must not hold
        credentials: it is what replicas receive."""
        now = time.time()
        with self._lock:
            row = self.db.execute(
                "SELECT created_at, spec FROM sources WHERE kind = ? AND name = ?", (kind, name)
            ).fetchone()
            if expected_owner is not None and row:
                previous = json.loads(row[1])
                if previous.get("owner") != expected_owner or previous.get("sharing") != "public":
                    raise PermissionError("catalog source belongs to another owner")
            if expected_owner is not None and kind == "oci":
                mine = {name, spec.get("prefix") or name}
                for other_name, other_spec in self.db.execute("SELECT name, spec FROM sources WHERE kind='oci' AND name<>?", (name,)):
                    theirs = json.loads(other_spec)
                    if mine & {other_name, theirs.get("prefix") or other_name}:
                        raise FileExistsError("catalog name/prefix already belongs to another source")
            created = row[0] if row else now
            self.db.execute(
                "INSERT OR REPLACE INTO sources (kind, name, spec, credentials, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, name, json.dumps(spec, sort_keys=True), credentials, created, now),
            )
            if log:
                self._log_account(SOURCE_KIND, f"{kind}/{name}", {"source": spec})
            self.db.commit()
        if log:
            self._notify()
        self._notify_sources()

    def delete_source(self, kind: str, name: str, *, log: bool = True, expected_owner: str | None = None) -> bool:
        """Delete a managed source and everything cached from it (its blobs
        are left to GC). Returns False if it did not exist."""
        with self._lock:
            row = self.db.execute("SELECT spec FROM sources WHERE kind=? AND name=?", (kind, name)).fetchone()
            if expected_owner is not None and row:
                previous = json.loads(row[0])
                if previous.get("owner") != expected_owner or previous.get("sharing") != "public":
                    raise PermissionError("catalog source belongs to another owner")
            source_name = name if row and json.loads(row[0]).get("sharing") == "public" else f"private:{name}"
            namespace = source_name if kind == "oci" else HELM_NS+source_name
            n = self.db.execute(
                "DELETE FROM sources WHERE kind = ? AND name = ?", (kind, name)
            ).rowcount
            if n:
                # Cached content must not outlive its source: a mirrored tag
                # with no source left would never be revalidated again.
                self.db.execute("DELETE FROM manifests WHERE upstream = ?", (namespace,))
                self.db.execute("DELETE FROM blob_grants WHERE upstream=?", (namespace,))
                if kind == "helm":
                    self.db.execute("DELETE FROM helm_sources WHERE repo = ?", (name,))
                if log:
                    self._log_account(SOURCE_KIND, f"{kind}/{name}", {"deleted": True})
            self.db.commit()
        if n:
            if log:
                self._notify()
            self._notify_sources()
        return bool(n)

    def publish_config_sources(self, snapshot: dict) -> bool:
        """Primary: log the config-file upstreams / Helm repos for replicas
        when they differ from what was last published. Returns True if an
        event was written."""
        body = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        with self._lock:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key = ?", (CONFIG_SOURCES_META,)
            ).fetchone()
            if row is not None and row[0] == body:
                return False
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (CONFIG_SOURCES_META, body),
            )
            self.db.execute(
                "INSERT INTO changelog (kind, reference, digest, bytes) VALUES (?, ?, ?, ?)",
                (SOURCE_KIND, CONFIG_SOURCES_KEY, "sha256:"+hashlib.sha256(body.encode()).hexdigest(), body.encode()),
            )
            self.db.commit()
        self._notify()
        return True

    def config_sources(self) -> dict | None:
        """The primary's config-file sources: on the primary what it last
        published, on a replica what it last received. None = nothing yet."""
        value = self.get_meta(CONFIG_SOURCES_META)
        return json.loads(value) if value else None

    def apply_source_event(self, key: str, body: bytes) -> None:
        if key != CONFIG_SOURCES_KEY:
            raise ValueError("legacy source replication is disabled")
        snapshot = json.loads(body)
        if snapshot.get("protocol") != "public-v2":
            raise ValueError("legacy catalog rejected")
        from .sources import _parse_snapshot
        _parse_snapshot(snapshot)
        self.set_meta(CONFIG_SOURCES_META, json.dumps(snapshot, sort_keys=True))
        self._notify_sources()

    def has_local_repos_under(self, prefix: str) -> bool:
        """Whether pushed images live under ``<prefix>/`` (an upstream with
        that prefix would hide them)."""
        head = prefix + "/"
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM manifests WHERE upstream = ? AND substr(repo, 1, ?) = ? LIMIT 1",
                (LOCAL, len(head), head),
            ).fetchone()
        return row is not None

    # -- helm sources ----------------------------------------------------------

    def replace_helm_sources(
        self, repo: str, rows: list[tuple[str, str, str, str | None]]
    ) -> None:
        """Set the (name, version, url, digest) rows of a Helm repo."""
        with self._lock:
            self.db.execute("DELETE FROM helm_sources WHERE repo = ?", (repo,))
            self.db.executemany(
                "INSERT OR REPLACE INTO helm_sources (repo, name, version, url, digest) "
                "VALUES (?, ?, ?, ?, ?)",
                [(repo, *row) for row in rows],
            )
            self.db.commit()

    def helm_source(self, repo: str, name: str, version: str) -> tuple[str, str | None] | None:
        """(url, digest | None) of a chart version, or None if not listed."""
        with self._lock:
            row = self.db.execute(
                "SELECT url, digest FROM helm_sources WHERE repo=? AND name=? AND version=?",
                (repo, name, version),
            ).fetchone()
        return (row[0], row[1]) if row else None

    # -- changelog -----------------------------------------------------------

    def current_serial(self) -> int:
        """Highest serial ever issued (monotonic, survives row deletion)."""
        with self._lock:
            row = self.db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'changelog'"
            ).fetchone()
        return row[0] if row else 0

    def changelog_since(self, serial: int, limit: int = 500) -> list[ChangeEvent]:
        with self._lock:
            rows = self.db.execute(
                "SELECT serial, kind, upstream, repo, reference, digest, bytes, media_type "
                "FROM changelog WHERE serial > ? ORDER BY serial LIMIT ?",
                (serial, limit),
            ).fetchall()
        return [
            ChangeEvent(
                serial=r[0], kind=r[1], digest=r[5], upstream=r[2],
                repo=r[3], reference=r[4], bytes=r[6], media_type=r[7],
            )
            for r in rows
        ]

    def compact_changelog(self) -> int:
        """Drop superseded changelog rows; returns how many were deleted.

        Keeps the newest row per manifest key (older ones only describe a
        state that was replaced) and the oldest row per blob digest. Safe
        for tailing replicas: any deleted row has a surviving row for the
        same key at a higher serial (manifests), or at a lower one the
        replica has already passed (blobs).
        """
        with self._lock:
            n = self.db.execute(
                "DELETE FROM changelog WHERE kind = 'manifest' AND serial NOT IN ("
                " SELECT MAX(serial) FROM changelog WHERE kind = 'manifest'"
                " GROUP BY upstream, repo, reference)"
            ).rowcount
            n += self.db.execute(
                "DELETE FROM changelog WHERE kind = 'blob' AND serial NOT IN ("
                " SELECT MIN(serial) FROM changelog WHERE kind = 'blob' GROUP BY digest)"
            ).rowcount
            # Accounts and sources: the newest event per key is its whole state.
            n += self.db.execute(
                "DELETE FROM changelog WHERE kind IN ('user', 'token', 'source') AND serial NOT IN ("
                " SELECT MAX(serial) FROM changelog WHERE kind IN ('user', 'token', 'source')"
                " GROUP BY kind, reference)"
            ).rowcount
            self.db.commit()
        return n

    # -- garbage collection ----------------------------------------------------

    def gc(self, manifest_max_idle: float, blob_grace: float) -> GCResult:
        """Expire idle mirrored manifests, then delete unreferenced blobs.

        - Manifests from upstream mirrors not stored, revalidated or pulled
          for ``manifest_max_idle`` seconds are dropped (0 = never). Pushed
          (local) manifests are never expired: they have no other source.
        - Blobs referenced by no stored manifest and untouched for
          ``blob_grace`` seconds are deleted. The grace period protects
          in-flight pulls and pushes whose manifest has not arrived yet.

        Runs in a worker thread; only the final deletes hold the lock.
        """
        result = GCResult()
        start = time.time()
        if manifest_max_idle > 0:
            cutoff = start - manifest_max_idle
            with self._lock:
                keys = self.db.execute(
                    "SELECT upstream, repo, reference FROM manifests WHERE upstream != ? "
                    "AND MAX(stored_at, checked_at, pulled_at) < ?",
                    (LOCAL, cutoff),
                ).fetchall()
                for key in keys:
                    self.db.execute(
                        "DELETE FROM manifests WHERE upstream=? AND repo=? AND reference=? "
                        "AND MAX(stored_at, checked_at, pulled_at) < ?", (*key, cutoff))
                    self.db.execute(
                        "DELETE FROM changelog WHERE kind='manifest' "
                        "AND upstream=? AND repo=? AND reference=?", key)
                self.db.commit()
            result.manifests = len(keys)

        blob_cutoff = start - blob_grace
        with self._lock:
            candidates = [r[0] for r in self.db.execute(
                "SELECT digest FROM blobs WHERE stored_at < ?", (blob_cutoff,)
            ).fetchall()]
        if not candidates:
            return result
        referenced = self._referenced_blobs()
        orphans = [d for d in candidates if d not in referenced]
        deleted: list[tuple[str, int]] = []
        with self._lock:
            # Re-check under the lock: manifests stored since the scan began
            # may reference a candidate, and a pusher may have touched one.
            for (body,) in self.db.execute(
                "SELECT bytes FROM manifests WHERE stored_at >= ?", (start,)
            ).fetchall():
                referenced.update(referenced_blobs(body))
            for digest in orphans:
                if digest in referenced:
                    continue
                row = self.db.execute(
                    "SELECT size FROM blobs WHERE digest = ? AND stored_at < ?",
                    (digest, blob_cutoff),
                ).fetchone()
                if row is None:
                    continue
                self.db.execute("DELETE FROM blobs WHERE digest = ?", (digest,))
                self.db.execute(
                    "DELETE FROM changelog WHERE kind='blob' AND digest = ?", (digest,)
                )
                deleted.append((digest, row[0]))
            self.db.commit()
        for digest, size in deleted:
            try:
                self.blob_path(digest).unlink()
            except FileNotFoundError:
                pass
            result.blobs += 1
            result.bytes += size
        return result

    def _referenced_blobs(self) -> set[str]:
        refs: set[str] = set()
        with self._lock:
            rows = self.db.execute("SELECT bytes FROM manifests").fetchall()
        for (body,) in rows:
            refs.update(referenced_blobs(body))
        return refs

    # -- replicas ------------------------------------------------------------

    def register_replica(self, node_id: str, base_url: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO replicas (node_id, base_url, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET base_url=excluded.base_url, last_seen=excluded.last_seen",
                (node_id, base_url, time.time()),
            )
            self.db.commit()

    def ack_serial(self, node_id: str, serial: int) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE replicas SET acked_serial = max(acked_serial, ?), last_seen = ? "
                "WHERE node_id = ?",
                (serial, time.time(), node_id),
            )
            self.db.commit()

    def touch_replica(self, node_id: str) -> None:
        """Refresh liveness for a registered replica (any internal request)."""
        with self._lock:
            self.db.execute(
                "UPDATE replicas SET last_seen = ? WHERE node_id = ?",
                (time.time(), node_id),
            )
            self.db.commit()

    def record_pull(self, node_id: str) -> None:
        """Mark that this replica actually pulled content from the primary."""
        now = time.time()
        with self._lock:
            self.db.execute(
                "UPDATE replicas SET last_seen = ?, last_pull_at = ? WHERE node_id = ?",
                (now, now, node_id),
            )
            self.db.commit()

    def live_replicas(self, max_age: float = 90.0) -> list[tuple[str, str, int, float]]:
        with self._lock:
            return self.db.execute(
                "SELECT node_id, base_url, acked_serial, last_pull_at FROM replicas "
                "WHERE last_seen > ?",
                (time.time() - max_age,),
            ).fetchall()

    # -- catalog -------------------------------------------------------------

    def catalog(self, upstream: str | None = None, last: str | None = None, n: int = 100) -> list[str]:
        q = "SELECT DISTINCT repo FROM manifests"
        args: list = [len(HELM_NS), HELM_NS]
        conds = ["substr(upstream, 1, ?) != ?"]  # Helm charts are not registry repos
        if upstream:
            conds.append("upstream = ?")
            args.append(upstream)
        if last:
            conds.append("repo > ?")
            args.append(last)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY repo LIMIT ?"
        args.append(n)
        with self._lock:
            return [r[0] for r in self.db.execute(q, args).fetchall()]

    def list_manifests(self, limit: int = 200, offset: int = 0) -> tuple[list[dict], int]:
        """Stored manifests (tags and digest-pinned children), newest first.

        Returns (items, total). `size` is the manifest JSON length; use
        blob_stats() for the heavy bytes.
        """
        with self._lock:
            total = self.db.execute("SELECT COUNT(*) FROM manifests").fetchone()[0]
            rows = self.db.execute(
                "SELECT upstream, repo, reference, digest, media_type, "
                "LENGTH(bytes), stored_at FROM manifests "
                "ORDER BY stored_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [
            {
                "upstream": r[0],
                "repo": r[1],
                "reference": r[2],
                "digest": r[3],
                "media_type": r[4],
                "size": r[5],
                "stored_at": r[6],
            }
            for r in rows
        ], total

    def blob_stats(self) -> dict:
        with self._lock:
            count, total = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM blobs"
            ).fetchone()
        return {"count": count, "total_size": total}


class HashingWriter:
    """Streams bytes to a temp file while hashing them.

    Use ``awrite`` from async code: the write and the hash run in a worker
    thread (hashlib releases the GIL), and the data is flushed so readers
    of the same file see it immediately.
    """

    def __init__(self, path: Path):
        self.path = path
        self._f = open(path, "wb")  # noqa: SIM115 (handle owned by writer; closed in close/abort)
        self._h = hashlib.sha256()
        self.size = 0

    def write(self, chunk: bytes) -> None:
        self._f.write(chunk)
        self._h.update(chunk)
        self.size += len(chunk)

    def _write_flush(self, chunk: bytes) -> None:
        self._f.write(chunk)
        self._f.flush()
        self._h.update(chunk)

    async def awrite(self, chunk: bytes) -> None:
        if not chunk:
            return
        await asyncio.to_thread(self._write_flush, chunk)
        self.size += len(chunk)

    @property
    def digest(self) -> str:
        return "sha256:" + self._h.hexdigest()

    def close(self) -> None:
        self._f.close()

    def abort(self) -> None:
        self.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def ensure_node_id(root: str) -> str:
    path = Path(root) / "node_id"
    if path.exists():
        return path.read_text().strip()
    nid = uuid.uuid4().hex
    path.write_text(nid)
    return nid
