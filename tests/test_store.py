"""Store-level behavior: changelog hygiene, compaction, GC, temp cleanup."""

from __future__ import annotations

import json
import time

import pytest

from containerproxy.store import LOCAL, Store, parse_digest


def put_blob(store: Store, data: bytes, *, origin=None) -> str:
    tmp, w = store.new_blob_tmp()
    w.write(data)
    w.close()
    store.commit_blob(tmp, w.digest, w.size, origin=origin)
    return w.digest


def image_manifest(config: str, *layers: str) -> bytes:
    return json.dumps({
        "schemaVersion": 2,
        "config": {"digest": config, "size": 1},
        "layers": [{"digest": d, "size": 1} for d in layers],
    }).encode()


def age(store: Store, table: str, seconds: float) -> None:
    """Pretend every row in `table` was last touched `seconds` ago."""
    t = time.time() - seconds
    cols = "stored_at = ?" if table == "blobs" else "stored_at = ?, checked_at = ?, pulled_at = ?"
    args = (t,) if table == "blobs" else (t, t, t)
    with store._lock:
        store.db.execute(f"UPDATE {table} SET {cols}", args)
        store.db.commit()


def blob_rows(store: Store) -> int:
    with store._lock:
        return store.db.execute("SELECT COUNT(*) FROM changelog WHERE kind='blob'").fetchone()[0]


def test_sha512_digests_are_rejected():
    parse_digest("sha256:" + "a" * 64)
    with pytest.raises(ValueError):
        parse_digest("sha512:" + "a" * 128)


def test_recommitting_a_blob_adds_no_changelog_event(tmp_path):
    store = Store(str(tmp_path))
    digest = put_blob(store, b"same bytes")
    put_blob(store, b"same bytes")
    assert blob_rows(store) == 0  # unreferenced/private bytes do not enter the feed
    store.put_manifest("public", "app", "latest", "sha256:"+"a"*64, "m", image_manifest(digest))
    assert blob_rows(store) == 0  # a digest assertion alone is not byte provenance
    put_blob(store, b"same bytes", origin=("public", "app"))
    assert blob_rows(store) == 1
    put_blob(store, b"same bytes")
    assert blob_rows(store) == 1


def test_compaction_keeps_latest_manifest_and_serial(tmp_path):
    store = Store(str(tmp_path))
    d = put_blob(store, b"x", origin=("up", "repo"))
    for i in range(3):
        body = image_manifest(d) + b" " * i
        store.put_manifest("up", "repo", "tag", "sha256:" + f"{i}" * 64, "m", body)
    with store._lock:  # a legacy duplicate blob row (pre-dedup databases)
        store.db.execute("INSERT INTO changelog (kind, digest) VALUES ('blob', ?)", (d,))
        store.db.commit()
    before = store.current_serial()
    assert store.compact_changelog() == 3
    events = store.changelog_since(0)
    assert [e.kind for e in events] == ["blob", "manifest"]
    assert events[1].digest == "sha256:" + "2" * 64
    # Serials never go backwards, even though the newest row was deleted.
    assert store.current_serial() == before


def test_gc_deletes_only_old_unreferenced_blobs(tmp_path):
    store = Store(str(tmp_path))
    cfg, layer, orphan = (put_blob(store, b) for b in (b"cfg", b"layer", b"orphan"))
    store.put_manifest(LOCAL, "app", "v1", "sha256:" + "1" * 64, "m", image_manifest(cfg, layer))
    fresh_orphan = None
    age(store, "blobs", 2 * 86400)
    fresh_orphan = put_blob(store, b"just pushed, manifest not here yet")

    result = store.gc(manifest_max_idle=0, blob_grace=86400)

    assert (result.blobs, result.bytes) == (1, len(b"orphan"))
    assert not store.has_blob(orphan)
    assert not store.blob_path(orphan).exists()
    assert all(store.has_blob(d) for d in (cfg, layer, fresh_orphan))
    assert orphan not in {e.digest for e in store.changelog_since(0)}


def test_gc_expires_idle_mirrored_manifests_but_never_pushed_ones(tmp_path):
    store = Store(str(tmp_path))
    mirrored, pushed = put_blob(store, b"m"), put_blob(store, b"p")
    store.put_manifest("dockerhub", "library/x", "1", "sha256:" + "1" * 64, "m", image_manifest(mirrored))
    store.put_manifest(LOCAL, "app", "1", "sha256:" + "2" * 64, "m", image_manifest(pushed))
    age(store, "manifests", 40 * 86400)
    age(store, "blobs", 40 * 86400)

    result = store.gc(manifest_max_idle=30 * 86400, blob_grace=86400)

    assert (result.manifests, result.blobs) == (1, 1)
    assert not store.manifest_exists("dockerhub", "library/x", "1")
    assert store.manifest_exists(LOCAL, "app", "1")
    assert store.has_blob(pushed) and not store.has_blob(mirrored)


def test_touched_blob_survives_gc(tmp_path):
    """A pusher that finds a blob present (HEAD/mount) restarts its grace."""
    store = Store(str(tmp_path))
    d = put_blob(store, b"reused layer")
    age(store, "blobs", 2 * 86400)
    store.touch_blob(d)
    assert store.gc(manifest_max_idle=0, blob_grace=86400).blobs == 0


def test_stale_temp_files_are_removed_on_open(tmp_path):
    Store(str(tmp_path)).close()
    leftover = tmp_path / "tmp" / "blob-crashed"
    leftover.write_bytes(b"partial")
    Store(str(tmp_path))
    assert not leftover.exists()


def test_list_tags(tmp_path):
    store = Store(str(tmp_path))
    for ref in ("v2", "v1", "sha256:" + "a" * 64):
        store.put_manifest("up", "r", ref, "sha256:" + "b" * 64, "m", b"{}")
    assert store.list_tags("up", "r", None, None) == ["v1", "v2"]
    assert store.list_tags("up", "r", "v1", 10) == ["v2"]
    assert store.list_tags("up", "nope", None, None) is None
