#!/usr/bin/env python3
"""Check that reviewed license inventories still match the locked releases."""
from pathlib import Path
import hashlib
import json
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def check_project(root: Path) -> None:
    config = tomllib.loads((root / 'pyproject.toml').read_text())
    if config['project']['license'] != 'MIT' or 'LICENSE' not in config['project']['license-files']:
        raise ValueError(f'MIT license metadata is missing: {root}')
    if (root / 'LICENSE').read_bytes() != (ROOT / 'LICENSE').read_bytes():
        raise ValueError(f'MIT license differs: {root}')
    locked = {name.lower().replace('_', '-'): version for name, version in re.findall(
        r'^([\w-]+)==([^\s]+)', (root / 'requirements.lock').read_text(), re.MULTILINE)}
    records = json.loads((root / 'third_party/python-inventory.json').read_text())
    reviewed = {r['name'].lower().replace('_', '-'): r['version'] for r in records}
    if reviewed != locked:
        raise ValueError(f'License inventory needs review: {root}')
    for record in records:
        if not record['license_files'] or not record['source_url'].startswith('https://'):
            raise ValueError(f'Missing license/source notice: {record["name"]}')
        check_notices(root, record['license_files'])
    print(f'{root.name}: {len(records)} locked Python licenses verified')


def check_notices(root: Path, notices: list) -> None:
    for notice in notices:
        if hashlib.sha256((root / notice['path']).read_bytes()).hexdigest() != notice['sha256']:
            raise ValueError(f'License text changed: {notice["path"]}')


def main() -> None:
    check_project(ROOT)
    check_project(ROOT / 'webapp')
    if (ROOT / 'charts/ociproxy/LICENSE').read_bytes() != (ROOT / 'LICENSE').read_bytes():
        raise ValueError('Chart MIT license differs')
    for record in json.loads((ROOT / 'third_party/rust-inventory.json').read_text()):
        if not record['license_files']:
            raise ValueError(f'Missing Rust notice: {record["name"]}')
        check_notices(ROOT, record['license_files'])
    print('Chart MIT license and Rust notice checksums verified')


if __name__ == '__main__':
    main()
