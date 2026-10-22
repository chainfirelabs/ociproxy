"""Explicit offline migration: python -m ociproxy.migrate --config FILE --backup DIR."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sqlite3

from .config import load_config, parse_upstream, parse_helm_repo
from .store import Store, HELM_NS


def migrate(config, backup: str) -> None:
    root = Path(config.storage_path).resolve()
    target = Path(backup).resolve()
    if target == root or root in target.parents or target in root.parents:
        raise ValueError("backup must be a separate directory outside the storage tree")
    if target.exists():
        raise ValueError("backup destination must not already exist")
    # Caller must stop the node first. Copy blob files, secrets, node ID and
    # database together, then take a SQLite-consistent backup of the database.
    shutil.copytree(root, target)
    source = sqlite3.connect(root / 'index.db')
    destination = sqlite3.connect(target / 'index.db')
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    for suffix in ('-wal', '-shm'):
        (target / ('index.db'+suffix)).unlink(missing_ok=True)
    store = Store(str(root))
    try:
        if store.get_meta('node_isolation_schema') == '2':
            raise ValueError('database already uses node isolation; no migration needed')
        sources = [*config.upstreams, *config.helm_repos]
        sources += [(parse_upstream if row['kind'] == 'oci' else parse_helm_repo)(
            {'name': row['name'], **row['spec']}) for row in store.list_sources()]
        with store._lock:
            if config.is_primary:
                # These credentials were previously distributed to replicas.
                # Retain account records, but require local password resets and
                # re-enablement; no previously shared token remains valid.
                store.db.execute('UPDATE users SET disabled=1')
                store.db.execute('DELETE FROM tokens')
                for src in sources:
                    old = src.name if hasattr(src, 'prefix') else HELM_NS+src.name
                    new = src.storage_name if hasattr(src, 'prefix') else HELM_NS+src.storage_name
                    if old != new:
                        store.db.execute('UPDATE OR REPLACE manifests SET upstream=? WHERE upstream=?', (new, old))
            else:
                # There is no reliable record of which old replica accounts or
                # sources originated on that replica. Reset instead of claiming
                # formerly replicated credentials are now independent.
                for table in ('users','tokens','sources','manifests','blobs','blob_grants','manifest_blob_refs','helm_sources'):
                    store.db.execute(f'DELETE FROM {table}')
            for table in ('changelog','replicas','meta'):
                store.db.execute(f'DELETE FROM {table}')
            store.db.execute('INSERT INTO meta(key,value) VALUES (?,?)', ('node_isolation_schema','2'))
            if config.is_primary:
                store.db.execute("INSERT INTO meta(key,value) VALUES ('public_log_rebuild','1')")
            store.db.commit()
        if not config.is_primary:
            shutil.rmtree(store.blobs_dir)
            store.blobs_dir.mkdir(parents=True)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--stopped', action='store_true', help='confirm this node is stopped')
    args = parser.parse_args()
    if not args.stopped:
        parser.error('stop the node, then supply --stopped; do not migrate a live store')
    cfg = load_config(args.config)
    migrate(cfg, args.backup)
    print('Migration complete. Keep the backup private; rotate formerly shared credentials before restarting.')


if __name__ == '__main__':
    main()
