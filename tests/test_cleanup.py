"""Destructive cleanup boundaries, preview accuracy, pins and durable replication."""
from __future__ import annotations

import hashlib
import json
from collections import namedtuple

import httpx
import pytest

from ociproxy.cleanup import Cleanup, validate_policy
from ociproxy.config import Config, load_config
from ociproxy.store import Store, LOCAL
from ociproxy.auth import create_user
from .conftest import SECRET, wait_until, make_replica, Node

HEADERS = {'X-OCIProxy-Secret': SECRET, 'X-OCIProxy-Acting-User': 'ci'}


def image(store, up='public', repo='app', ref='latest', data=b'layer', children=None):
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    path, writer = store.new_blob_tmp()
    writer.write(data); writer.close(); store.commit_blob(path, digest, len(data))
    body = json.dumps({'manifests': [{'digest': child} for child in children]} if children else
                      {'layers': [{'digest': digest}]}).encode()
    manifest = 'sha256:' + hashlib.sha256(body).hexdigest()
    store.put_manifest(up, repo, ref, manifest, 'application/json', body)
    return [up, repo, ref], manifest, digest


@pytest.fixture
def engine(tmp_path):
    store = Store(str(tmp_path))
    store.shareable_repo = lambda up, repo: up == 'public'
    config = Config(role='primary', storage_path=str(tmp_path), gc_blob_grace=0)
    manager = Cleanup(store, config)
    yield manager
    store.close()


def test_shared_layers_and_pin_changes_after_preview(engine):
    s = engine.store
    one, _, blob = image(s, ref='one')
    two, _, _ = image(s, ref='two')
    preview = engine.preview({'targets': [one]})
    assert preview['reclaimable_bytes'] == 0
    assert engine.execute(preview['preview_id'])['result']['bytes'] == 0
    assert s.has_blob(blob)
    preview = engine.preview({'targets': [two]})
    engine.pin(two, True)
    result = engine.execute(preview['preview_id'])['result']
    assert result['protected'] == 1 and result['deleted'] == 0
    assert s.has_blob(blob)
    engine.pin(two, False)
    preview = engine.preview({'targets': [two]})
    assert preview['reclaimable_bytes'] == len(b'layer')
    assert engine.execute(preview['preview_id'])['result']['bytes'] == len(b'layer')
    assert not s.has_blob(blob)
    with pytest.raises(ValueError, match='expired|used'):
        engine.execute(preview['preview_id'])


def test_moved_tag_and_blob_grace(engine):
    engine.config.gc_blob_grace = 86400
    key, _, blob = image(engine.store)
    preview = engine.preview({'targets': [key]})
    assert preview['potential_bytes'] == len(b'layer') and preview['reclaimable_bytes'] == 0
    image(engine.store, data=b'newer tag')
    assert engine.execute(preview['preview_id'])['result']['changed'] == 1
    preview = engine.preview({'targets': [key]})
    assert engine.execute(preview['preview_id'])['result']['blobs'] == 0
    assert engine.store.has_blob(blob)


def test_multiarch_pins_and_retained_indexes_protect_children(engine):
    child, digest, blob = image(engine.store, ref='sha256-child')
    parent, _, _ = image(engine.store, ref='index', children=[digest])
    engine.pin(parent, True)
    assert engine.preview({'targets': [child]})['count'] == 0
    engine.pin(parent, False)
    assert engine.preview({'targets': [child]})['count'] == 0  # index retained
    preview = engine.preview({'targets': [parent]})
    assert preview['count'] == 2
    assert engine.execute(preview['preview_id'])['result']['deleted'] == 2
    assert not engine.store.has_blob(blob)


def test_public_cleanup_never_targets_private_or_pushes(engine):
    public, _, _ = image(engine.store)
    private, _, _ = image(engine.store, up='private:secret')
    pushed, _, _ = image(engine.store, up=LOCAL)
    preview = engine.preview({'scope': 'public', 'include_pushed': True, 'targets': [public, private, pushed]})
    assert preview['count'] == 1 and preview['blocked'] == 2
    job = engine.execute(preview['preview_id'])
    event = next(e for e in engine.store.public_events(0)[0] if e.kind == 'cleanup')
    assert b'private:secret' not in event.bytes and b'local' not in event.bytes
    assert json.loads(event.bytes)['job_id'] == job['job_id']
    assert engine.store.get_manifest(*private) and engine.store.get_manifest(*pushed)
    # A forged remote job is independently scoped on the receiver.
    result = engine.apply_public({'job_id': 'a'*32, 'targets': [[*private, engine.store.get_manifest(*private).digest],
                                                             [*pushed, engine.store.get_manifest(*pushed).digest]]})
    assert result['protected'] == 2 and result['deleted'] == 0
    assert engine.store.get_manifest(*private) and engine.store.get_manifest(*pushed)


def test_pushes_require_explicit_manual_selection(engine):
    key, _, _ = image(engine.store, up=LOCAL)
    assert engine.preview({'targets': [key]})['count'] == 0
    preview = engine.preview({'targets': [key], 'include_pushed': True})
    assert preview['contains_pushed']
    assert engine.execute(preview['preview_id'])['result']['deleted'] == 1


def test_lru_cache_limit_protects_pins_pushes_and_recent_content(engine):
    old, _, _ = image(engine.store, ref='old', data=b'a'*10)
    new, _, _ = image(engine.store, ref='new', data=b'b'*10)
    pin, _, _ = image(engine.store, ref='pin', data=b'c'*10)
    push, _, _ = image(engine.store, up=LOCAL, ref='push', data=b'd'*10)
    engine.pin(pin, True)
    with engine.store._lock:
        engine.store.db.execute('UPDATE manifests SET stored_at=1,checked_at=1 WHERE reference="old"')
        engine.store.db.commit()
    engine.set_policy({'manifest_max_idle_days': 0, 'disk_high_percent': 0, 'disk_low_percent': 75, 'max_cache_bytes': 30})
    result = engine.run()
    assert result['deleted'] == 1 and result['bytes'] == 10 and not result['pressure_remaining']
    assert engine.store.get_manifest(*old) is None
    assert all(engine.store.get_manifest(*key) for key in [new, pin, push])
    assert engine.policy()['max_cache_bytes'] == 30
    assert Cleanup(engine.store, engine.config).policy()['max_cache_bytes'] == 30


def test_disk_watermarks_and_retention(engine, monkeypatch):
    key, _, _ = image(engine.store, data=b'x'*10)
    Disk = namedtuple('Disk', 'total used free')
    monkeypatch.setattr('ociproxy.cleanup.shutil.disk_usage', lambda _: Disk(100, 90, 10))
    engine.set_policy({'manifest_max_idle_days': 0, 'disk_high_percent': 85, 'disk_low_percent': 75, 'max_cache_bytes': 0})
    result = engine.run()
    assert result['deleted'] == 1 and result['pressure_remaining']  # other filesystem data still uses space
    key, _, _ = image(engine.store)
    engine.pin(key, True)
    with engine.store._lock:
        engine.store.db.execute('UPDATE manifests SET stored_at=1,checked_at=1,pulled_at=1')
        engine.store.db.commit()
    assert engine.store.gc(1, 0).manifests == 0
    engine.pin(key, False)
    engine.set_policy({'manifest_max_idle_days': 1, 'disk_high_percent': 0, 'disk_low_percent': 75, 'max_cache_bytes': 0})
    assert engine.run()['deleted'] == 1


@pytest.mark.parametrize('policy', [
    {'manifest_max_idle_days': -1, 'disk_high_percent': 0, 'disk_low_percent': 75, 'max_cache_bytes': 0},
    {'manifest_max_idle_days': 0, 'disk_high_percent': 80, 'disk_low_percent': 90, 'max_cache_bytes': 0},
    {'manifest_max_idle_days': 0, 'disk_high_percent': 101, 'disk_low_percent': 75, 'max_cache_bytes': 0},
    {'manifest_max_idle_days': float('nan'), 'disk_high_percent': 0, 'disk_low_percent': 75, 'max_cache_bytes': 0},
])
def test_invalid_policy_rejected(policy):
    with pytest.raises(ValueError):
        validate_policy(policy)


def test_cleanup_config(tmp_path):
    path = tmp_path/'config.toml'
    path.write_text('[gc]\ndisk_high_percent=85\ndisk_low_percent=70\nmax_cache_bytes=1000\n')
    cfg = load_config(str(path))
    assert (cfg.gc_disk_high_percent, cfg.gc_disk_low_percent, cfg.gc_max_cache_bytes) == (85, 70, 1000)


async def test_public_cleanup_replicates_acknowledges_and_catches_up_offline(tmp_path, primary, replica, fake_upstream):
    fake_upstream.install_image('app')
    async with httpx.AsyncClient(trust_env=False) as c:
        assert (await c.get(primary.url+'/v2/fake/app/manifests/v1')).status_code == 200
        await wait_until(lambda: replica.app_obj.store.get_manifest('fake', 'app', 'v1') is not None)
        # An offline replica already has the selected content cached.
        offline = make_replica(tmp_path, primary, 'late', upstreams=[primary.config.upstreams[0]])
        await wait_until(lambda: offline.app_obj.store.get_manifest('fake', 'app', 'v1') is not None)
        offline_config = offline.config
        offline.stop()
        preview = await c.post(primary.url+'/internal/cleanup/preview', headers=HEADERS,
                               json={'scope': 'public', 'targets': [['fake', 'app', 'v1']]})
        assert preview.status_code == 200
        result = await c.post(primary.url+'/internal/cleanup/execute', headers=HEADERS,
                              json={'preview_id': preview.json()['preview_id']})
        assert result.status_code == 200
        job = result.json()['job_id']
        await wait_until(lambda: replica.app_obj.store.get_manifest('fake', 'app', 'v1') is None)
        await wait_until(lambda: primary.app_obj.cleanup.jobs()[0]['replicas']['r1']['status'] == 'completed')
        assert primary.app_obj.cleanup.jobs()[0]['replicas']['late']['status'] == 'pending'
        primary.app_obj.store.compact_changelog()
        late = Node(offline_config).start()
        try:
            await wait_until(lambda: primary.app_obj.cleanup.jobs()[0]['replicas']['late']['status'] == 'completed')
            assert late.app_obj.store.get_manifest('fake', 'app', 'v1') is None
            assert any(e.kind == 'cleanup' for e in primary.app_obj.store.changelog_since(0))
            # A repeated event returns its saved result, preserving content refetched later.
            _, fresh_digest, _ = image(late.app_obj.store, up='fake', ref='v1', data=b'refetched')
            event = next(e for e in primary.app_obj.store.changelog_since(0) if e.kind == 'cleanup')
            again = late.app_obj.cleanup.apply_public(json.loads(event.bytes))
            assert again['deleted'] == 1
            assert late.app_obj.store.get_manifest('fake', 'app', 'v1').digest == fresh_digest
        finally:
            late.stop()
        # Cleanup never blocks upstream: the next pull can repopulate the cache.
        assert (await c.get(primary.url+'/v2/fake/app/manifests/v1')).status_code == 200
        assert (await c.post(primary.url+'/replication/v2/cleanup/'+job+'/ack',
                             headers=HEADERS, json={})).status_code == 403


async def test_cleanup_api_and_ui_permissions(primary):
    from .test_webapp import ui_client, login, CSRF
    create_user(primary.app_obj.store, 'reader', 'reader', 'reader-password')
    async with httpx.AsyncClient(trust_env=False) as c:
        assert (await c.get(primary.url+'/internal/cleanup')).status_code == 403
        assert (await c.get(primary.url+'/internal/cleanup', headers={**HEADERS, 'X-OCIProxy-Acting-User':'reader'})).status_code == 403
    async with ui_client(primary.url) as c:
        assert (await c.get('/api/cleanup')).status_code == 401
        await login(c, 'reader', 'reader-password')
        assert (await c.get('/api/cleanup')).status_code == 403
        await login(c)
        assert (await c.get('/api/cleanup')).status_code == 200
        assert (await c.post('/api/cleanup/run', json={})).status_code == 403
        assert (await c.post('/api/cleanup/run', headers=CSRF, json={})).status_code == 200
        assert (await c.post('/api/cleanup/execute', headers=CSRF, json={'preview_id':'invalid'})).status_code == 400
        assert (await c.post('/api/cleanup/policy', headers=CSRF, json={})).status_code == 400


async def test_replica_pins_report_skips_without_deleting_private_data(primary, replica, fake_upstream):
    fake_upstream.install_image('app')
    async with httpx.AsyncClient(trust_env=False) as c:
        await c.get(primary.url+'/v2/fake/app/manifests/v1')
        await wait_until(lambda: replica.app_obj.store.get_manifest('fake', 'app', 'v1') is not None)
        replica.app_obj.cleanup.pin(['fake', 'app', 'v1'], True)
        local, _, _ = image(replica.app_obj.store, up='private:only-here')
        preview = (await c.post(primary.url+'/internal/cleanup/preview', headers=HEADERS,
                    json={'scope':'public', 'targets':[['fake','app','v1']]})).json()
        response = await c.post(primary.url+'/internal/cleanup/execute', headers=HEADERS,
                                json={'preview_id':preview['preview_id']})
        assert response.status_code == 200
        await wait_until(lambda: primary.app_obj.cleanup.jobs()[0]['replicas']['r1']['status'] == 'completed_with_skips')
        assert replica.app_obj.store.get_manifest('fake', 'app', 'v1') is not None
        assert replica.app_obj.store.get_manifest(*local) is not None
        # Peer tokens cannot trigger cleanup or mutate policy/pins.
        peer = {'Authorization':'Bearer replication-test-token-r1','X-OCIProxy-Peer':'r1'}
        assert (await c.post(primary.url+'/internal/cleanup/run', headers=peer, json={})).status_code == 403
        assert (await c.post(primary.url+'/replication/v2/cleanup/unknown/ack', headers=peer,
                             json={'deleted':0,'protected':0,'changed':0,'missing':0,'blobs':0,'bytes':0})).status_code == 400


def test_cleanup_state_survives_restart_and_preview_expires(tmp_path):
    store = Store(str(tmp_path))
    config = Config(role='replica', storage_path=str(tmp_path))
    store.replication_enabled = False
    cleanup = Cleanup(store, config)
    key, digest, _ = image(store)
    cleanup.pin(key, True)
    policy = {'manifest_max_idle_days':7, 'disk_high_percent':85,'disk_low_percent':70,'max_cache_bytes':1234}
    cleanup.set_policy(policy)
    payload = {'job_id':'b'*32,'targets':[[*key,digest]]}
    assert cleanup.apply_public(payload)['protected'] == 1
    preview = cleanup.preview({'targets':[key]})
    with store._lock:
        store.db.execute('UPDATE cleanup_previews SET created_at=0');store.db.commit()
    with pytest.raises(ValueError,match='expired'):
        cleanup.execute(preview['preview_id'])
    store.close()
    store = Store(str(tmp_path));store.replication_enabled = False
    cleanup = Cleanup(store, config)
    assert cleanup.policy() == policy
    assert cleanup.overview()['entries'][0]['pinned']
    assert cleanup.apply_public(payload)['protected'] == 1
    assert cleanup.jobs()[0]['scope'] == 'received'
    store.close()


def test_lru_removes_platform_group_and_preserves_private_shared_layers(engine):
    child, digest, _ = image(engine.store, ref='platform', data=b'platform')
    root, _, _ = image(engine.store, ref='index', children=[digest])
    engine.set_policy({'manifest_max_idle_days':0,'disk_high_percent':0,'disk_low_percent':75,'max_cache_bytes':1})
    result = engine.run()
    assert result['deleted'] == 2
    assert engine.store.get_manifest(*child) is None and engine.store.get_manifest(*root) is None
    public, _, blob = image(engine.store, data=b'shared-private-bytes')
    private, _, _ = image(engine.store, up=LOCAL, data=b'shared-private-bytes')
    preview = engine.preview({'scope':'public','targets':[public]})
    assert preview['reclaimable_bytes'] == 0
    engine.execute(preview['preview_id'])
    assert engine.store.has_blob(blob) and engine.store.get_manifest(*private)


def test_public_cleanup_does_not_collect_unrelated_private_orphans(engine):
    public, _, _ = image(engine.store, data=b'public bytes')
    private, _, private_blob = image(engine.store, up='private:secret', data=b'private orphan')
    with engine.store._lock:
        engine.store.db.execute('DELETE FROM manifests WHERE upstream=? AND repo=? AND reference=?', private)
        engine.store.db.commit()
    preview = engine.preview({'scope':'public','targets':[public]})
    assert preview['reclaimable_bytes'] == len(b'public bytes')
    engine.execute(preview['preview_id'])
    assert engine.store.has_blob(private_blob)
    result = engine.apply_public({'job_id':'c'*32,'targets':[[*private,'sha256:'+'a'*64]]})
    assert result['protected'] == 1 and result['missing'] == 0 and result['bytes'] == 0
    assert engine.store.has_blob(private_blob)
    assert engine.run()['bytes'] == len(b'private orphan')


def test_gc_cannot_unlink_a_concurrently_replaced_blob(engine, monkeypatch):
    import threading
    from pathlib import Path
    _, _, digest = image(engine.store)
    with engine.store._lock:
        engine.store.db.execute('DELETE FROM manifests');engine.store.db.commit()
    path, writer = engine.store.new_blob_tmp();writer.write(b'layer');writer.close()
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    original = Path.unlink
    errors = []

    def unlink(file, *args, **kwargs):
        if file == engine.store.blob_path(digest):
            entered.set()
            assert release.wait(5)
        return original(file, *args, **kwargs)

    def collect():
        try:
            engine.store.gc(0, 0)
        except Exception as exc:
            errors.append(exc)

    def replace():
        try:
            engine.store.commit_blob(path, digest, len(b'layer'))
            committed.set()
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(Path, 'unlink', unlink)
    collector = threading.Thread(target=collect);collector.start()
    assert entered.wait(5)
    writer_thread = threading.Thread(target=replace);writer_thread.start()
    assert not committed.wait(.1)
    release.set();collector.join(5);writer_thread.join(5)
    assert not errors
    assert committed.is_set() and engine.store.has_blob(digest)
    assert engine.store.blob_path(digest).read_bytes() == b'layer'


async def test_serving_cached_blob_renews_gc_grace(primary, fake_upstream):
    fake_upstream.install_image('active')
    async with httpx.AsyncClient(trust_env=False) as c:
        manifest = (await c.get(primary.url+'/v2/fake/active/manifests/v1')).json()
        digest = manifest['layers'][0]['digest']
        assert (await c.get(primary.url+'/v2/fake/active/blobs/'+digest)).status_code == 200
        store = primary.app_obj.store
        with store._lock:
            store.db.execute('UPDATE blobs SET stored_at=0 WHERE digest=?',(digest,));store.db.commit()
        assert (await c.get(primary.url+'/v2/fake/active/blobs/'+digest)).status_code == 200
        with store._lock:
            store.db.execute('DELETE FROM manifests');store.db.commit()
        assert store.gc(0, 1).blobs == 0
        assert store.has_blob(digest)


def test_cleanup_stays_acknowledgeable_without_exporting_newly_private_names(engine, tmp_path):
    key, _, _ = image(engine.store)
    preview = engine.preview({'scope':'public','targets':[key]})
    job = engine.execute(preview['preview_id'])['job_id']
    engine.store.shareable_repo = lambda up, repo: False
    events, _ = engine.store.public_events(0)
    event = next(e for e in events if e.kind == 'cleanup')
    payload = json.loads(event.bytes)
    assert payload == {'job_id':job,'targets':[]}
    assert b'public' not in event.bytes and b'app' not in event.bytes
    receiver = Store(str(tmp_path / 'receiver'))
    receiver.shareable_repo = lambda up, repo: False
    try:
        cleanup = Cleanup(receiver, Config(role='replica', storage_path=str(tmp_path / 'receiver')))
        assert cleanup.apply_public(payload)['deleted'] == 0
    finally:
        receiver.close()
