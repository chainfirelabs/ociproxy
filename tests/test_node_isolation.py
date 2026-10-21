"""End-to-end node isolation across public Docker/Helm sharing and local accounts."""
import base64
import asyncio
import hashlib
import json

import httpx
import pytest

from containerproxy.auth import create_user, create_token
from containerproxy.config import Config, UpstreamConfig, HelmRepoConfig
from .conftest import Node, free_port, wait_until


@pytest.fixture
def cluster(tmp_path, fake_upstream):
    base = f'http://127.0.0.1:{fake_upstream.port}'
    fake_upstream.install_image(repo='shared/app')
    fake_upstream.install_image(repo='secret/app')
    fake_upstream.add_helm_chart('mychart', '1.0.0')
    nodes = []
    for index in range(3):
        cfg = Config(role='primary' if index == 0 else 'replica',
                     storage_path=str(tmp_path / str(index)), listen='127.0.0.1', port=free_port(),
                     secret=f'local-admin-secret-{index}', push_username='admin',
                     push_password=f'local-admin-password-{index}',
                     replication_id=f'node{index}' if index else '',
                     replication_token=f'peer-token-{index}' if index else '',
                     primary_url=nodes[0].url if index else None)
        if index == 0:
            cfg.upstreams = [UpstreamConfig('docker', base, 'docker', visibility='public', sharing='public'),
                             UpstreamConfig('secret', base, 'secret', visibility='private')]
            cfg.helm_repos = [HelmRepoConfig('charts', base+'/charts', visibility='public', sharing='public'),
                              HelmRepoConfig('privatecharts', base+'/charts')]
        node = Node(cfg).start()
        nodes.append(node)
    yield nodes, base
    for node in reversed(nodes):
        node.stop()


def local_headers(index):
    return {'X-ContainerProxy-Secret': f'local-admin-secret-{index}',
            'X-ContainerProxy-Acting-User': 'admin'}


def peer_headers(index):
    return {'Authorization': f'Bearer peer-token-{index}', 'X-ContainerProxy-Peer': f'node{index}'}


@pytest.mark.parametrize('index', [0, 1])
async def test_sync_now_refreshes_replica_catalog_with_local_admin_only(cluster, monkeypatch, index):
    from containerproxy_ui.app import create_app
    nodes, _ = cluster
    tail = nodes[1].app_obj.tail
    await wait_until(lambda: tail.primary_serial is not None)
    calls = []
    original = tail.client.catalog

    async def catalog():
        result = await original()
        calls.append(result)
        return result

    monkeypatch.setattr(tail.client, 'catalog', catalog)
    create_user(nodes[index].app_obj.store, 'sync-reader', 'reader', 'sync-reader-password')
    app = create_app(proxy_url=nodes[index].url, secret=nodes[index].config.secret)
    csrf = {'X-Requested-With': 'containerproxy'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://ui') as c:
        assert (await c.post('/api/sync', headers=csrf)).status_code == 401
        await c.post('/api/auth/login', headers=csrf,
                     json={'username': 'sync-reader', 'password': 'sync-reader-password'})
        assert (await c.post('/api/sync', headers=csrf)).status_code == 403
        await c.post('/api/auth/login', headers=csrf,
                     json={'username': 'admin', 'password': f'local-admin-password-{index}'})
        assert (await c.post('/api/sync')).status_code == 403
        response = await c.post('/api/sync', headers=csrf)
        assert response.status_code == 202
        await wait_until(lambda: bool(calls), timeout=5, msg='immediate public catalog refresh')
        if index == 0:
            assert nodes[1].app_obj.store.get_user('sync-reader') is None
    await app.state.ui.aclose()
    async with httpx.AsyncClient(trust_env=False) as c:
        assert (await c.post(nodes[index].url+'/internal/sync', headers=peer_headers(1))).status_code == 403
        headers = {**local_headers(index), 'X-ContainerProxy-Acting-User': 'sync-reader'}
        assert (await c.post(nodes[index].url+'/internal/sync', headers=headers)).status_code == 403


async def test_replica_sync_now_interrupts_retry_backoff(cluster, monkeypatch):
    nodes, _ = cluster
    tail = nodes[1].app_obj.tail
    await wait_until(lambda: tail.primary_serial is not None)
    attempts = []

    async def unavailable(*args):
        attempts.append(True)
        raise httpx.ConnectError('primary unavailable')

    original = tail.client.register
    monkeypatch.setattr(tail.client, 'register', unavailable)
    async with httpx.AsyncClient(trust_env=False) as c:
        assert (await c.post(nodes[1].url+'/internal/sync', headers=local_headers(1))).status_code == 202
    await wait_until(lambda: len(attempts) >= 3)
    assert not tail.connected
    monkeypatch.setattr(tail.client, 'register', original)
    started = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(trust_env=False) as c:
        assert (await c.post(nodes[1].url+'/internal/sync', headers=local_headers(1))).status_code == 202
    await wait_until(lambda: tail.connected, timeout=2, msg='sync bypasses retry backoff')
    assert asyncio.get_running_loop().time() - started < 2


async def test_accounts_and_tokens_are_node_local(cluster):
    nodes, _ = cluster
    async with httpx.AsyncClient(trust_env=False) as c:
        for index, node in enumerate(nodes):
            r = await c.post(node.url+'/internal/users', headers=local_headers(index),
                             json={'username': 'alice', 'password': f'alice-password-{index}', 'role': 'reader'})
            assert r.status_code == 201, r.text
        token, _ = create_token(nodes[1].app_obj.store, 'alice', 'node1-only')
        for index, node in enumerate(nodes):
            r = await c.post(node.url+'/internal/auth/login', headers=local_headers(index),
                             json={'username': 'alice', 'password': f'alice-password-{index}'})
            assert r.status_code == 200
            for other in range(3):
                result = await node.app_obj.auth.check('alice', f'alice-password-{other}')
                assert (result is not None) == (other == index)
            assert (await node.app_obj.auth.check('alice', token) is not None) == (index == 1)
        events = nodes[0].app_obj.store.changelog_since(0, limit=10000)
        assert all(e.kind not in ('user', 'token') for e in events)
        assert all(b'alice' not in (e.bytes or b'') for e in events)


async def test_public_docker_and_helm_share_private_primary_stays_local(cluster):
    nodes, _ = cluster
    async with httpx.AsyncClient(trust_env=False, timeout=20) as c:
        primary = nodes[0]
        assert (await c.get(primary.url+'/v2/docker/shared/app/manifests/v1')).status_code == 200
        assert (await c.get(primary.url+'/helm/charts/index.yaml')).status_code == 200
        chart = await c.get(primary.url+'/helm/charts/charts/mychart/1.0.0/mychart-1.0.0.tgz')
        assert chart.status_code == 200, chart.text
        private = await c.get(primary.url+'/v2/secret/secret/app/manifests/v1', auth=('admin','local-admin-password-0'))
        assert private.status_code == 200, private.text
        assert (await c.get(primary.url+'/helm/privatecharts/index.yaml',auth=('admin','local-admin-password-0'))).status_code == 200
        for node in nodes[1:]:
            await wait_until(lambda: node.app_obj.store.get_manifest('docker','shared/app','v1') is not None)
            assert (await c.get(node.url+'/v2/docker/shared/app/manifests/v1')).status_code == 200
            assert (await c.get(node.url+'/helm/charts/charts/mychart/1.0.0/mychart-1.0.0.tgz')).content == chart.content
            assert node.app_obj.config.upstream_by_prefix('secret') is None
            assert node.app_obj.config.helm_repo('privatecharts') is None
            assert node.app_obj.store.get_manifest('private:secret','secret/app','v1') is None
        feed = await c.get(primary.url+'/replication/v2/changelog',headers=peer_headers(1),params={'wait':0})
        assert feed.status_code == 200
        for event in feed.json()['events']:
            assert 'private:' not in str(event)
            if event.get('bytes_b64'):
                assert b'privatecharts' not in base64.b64decode(event['bytes_b64'])


async def test_private_replica_sources_and_public_publication(cluster):
    nodes, base = cluster
    async with httpx.AsyncClient(trust_env=False, timeout=20) as c:
        r = await c.post(nodes[1].url+'/internal/sources',headers=local_headers(1),json={
            'kind':'oci','name':'mine','base_url':base,
            'credentials':{'username':'local-robot','password':'local-upstream-password'}})
        assert r.status_code == 201, r.text
        assert r.json()['sharing'] == 'local'
        r = await c.get(nodes[1].url+'/v2/mine/secret/app/manifests/v1',auth=('admin','local-admin-password-1'))
        assert r.status_code == 200, r.text
        assert nodes[0].app_obj.config.upstream_by_prefix('mine') is None
        assert nodes[2].app_obj.config.upstream_by_prefix('mine') is None
        r = await c.post(nodes[1].url+'/internal/sources',headers=local_headers(1),json={
            'kind':'helm','name':'minecharts','base_url':base+'/charts'})
        assert r.status_code == 201, r.text
        assert (await c.get(nodes[1].url+'/helm/minecharts/index.yaml',auth=('admin','local-admin-password-1'))).status_code == 200
        public = {'kind':'oci','name':'quay','base_url':base,'visibility':'public','sharing':'public'}
        r = await c.post(nodes[1].url+'/internal/sources',headers=local_headers(1),json=public)
        assert r.status_code == 201, r.text
        await wait_until(lambda: nodes[2].app_obj.config.upstream_by_prefix('quay') is not None)
        assert nodes[0].app_obj.config.upstream_by_prefix('quay').sharing == 'public'
        r = await c.post(nodes[0].url+'/replication/v2/sources',headers=peer_headers(2),json={**public,'action':'delete'})
        assert r.status_code == 403
        # Local private state and account management are usable with the coordinator down.
        nodes[0].stop()
        r = await c.get(nodes[1].url+'/v2/mine/shared/app/manifests/v1',auth=('admin','local-admin-password-1'))
        assert r.status_code == 200
        assert (await c.post(nodes[1].url+'/internal/auth/login',headers=local_headers(1),
            json={'username':'admin','password':'local-admin-password-1'})).status_code == 200


async def test_replication_credentials_never_authorize_private_or_admin(cluster):
    nodes, _ = cluster
    async with httpx.AsyncClient(trust_env=False, timeout=20) as c:
        primary = nodes[0]
        assert (await c.get(primary.url+'/replication/v2/catalog',headers=local_headers(0))).status_code == 403
        assert (await c.get(primary.url+'/internal/users',headers={**peer_headers(1), 'X-ContainerProxy-Acting-User':'admin'})).status_code == 403
        for endpoint in ('manifest','tags'):
            r = await c.get(primary.url+'/replication/v2/pull/'+endpoint,headers=peer_headers(1),params={
                'upstream':'private:secret','repo':'secret/app','reference':'v1'})
            assert r.status_code == 404, r.text
        digest='sha256:'+hashlib.sha256(b'only-private').hexdigest()
        store=primary.app_obj.store
        path, writer=store.new_blob_tmp();writer.write(b'only-private');writer.close()
        store.commit_blob(path,digest,12)
        store.put_manifest('private:secret','secret/app','test','sha256:'+'a'*64,'application/json',
            json.dumps({'layers':[{'digest':digest}]}).encode())
        for query in ({}, {'upstream':'public-feed'}, {'upstream':'docker','repo':'shared/app'}):
            r = await c.get(primary.url+'/replication/v2/pull/blob/'+digest,headers=peer_headers(1),params=query)
            assert r.status_code == 404
        assert (await c.post(nodes[1].url+'/internal/ingest/manifest',headers=local_headers(1),json={})).status_code == 404
        assert (await c.get(primary.url+'/internal/changelog',headers=local_headers(0))).status_code == 404


async def test_ui_authenticates_against_its_own_node(cluster):
    from containerproxy_ui.app import create_app
    nodes, _ = cluster
    csrf = {'X-Requested-With':'containerproxy'}
    app = create_app(proxy_url=nodes[0].url, node_url=nodes[1].url, secret=nodes[1].config.secret)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://ui') as c:
        r = await c.post('/api/auth/login',headers=csrf,json={'username':'admin','password':'local-admin-password-0'})
        assert r.status_code == 401
        r = await c.post('/api/auth/login',headers=csrf,json={'username':'admin','password':'local-admin-password-1'})
        assert r.status_code == 200
        overview = (await c.get('/api/overview')).json()
        assert overview['node_id'] == nodes[1].app_obj.node_id
        await wait_until(lambda: nodes[1].app_obj.tail.primary_serial is not None)
        overview = (await c.get('/api/overview')).json()
        assert overview['primary']['registered'] is True
        assert overview['primary']['serial'] is not None
        assert overview['primary']['last_contact_at'] is not None
        await nodes[1].app_obj.tail.stop()
        overview = (await c.get('/api/overview')).json()
        assert overview['primary']['registered'] is False
        assert all(source['name'] != 'secret' for source in overview['upstreams'])
        r = await c.post('/api/users',headers=csrf,json={'username':'ui-local','role':'reader','password':'node1-ui-password'})
        assert r.status_code == 201
        assert nodes[1].app_obj.store.get_user('ui-local') is not None
        assert nodes[0].app_obj.store.get_user('ui-local') is None
        assert nodes[2].app_obj.store.get_user('ui-local') is None
    await app.state.ui.aclose()


@pytest.mark.parametrize('kind', ['oci','helm'])
async def test_public_catalog_publication_updates_and_revocation(cluster, kind):
    nodes, base = cluster
    path = '/charts' if kind == 'helm' else ''
    spec = {'kind':kind,'name':'published','base_url':base+path,'sharing':'public','visibility':'public'}
    async with httpx.AsyncClient(trust_env=False, timeout=20) as c:
        r = await c.post(nodes[1].url+'/internal/sources',headers=local_headers(1),json=spec)
        assert r.status_code == 201, r.text
        lookup = lambda node: node.app_obj.sources.resolve(kind,'published')
        await wait_until(lambda: lookup(nodes[2]) is not None)
        if kind == 'oci':
            r = await c.get(nodes[2].url+'/v2/published/shared/app/manifests/v1')
        else:
            r = await c.get(nodes[2].url+'/helm/published/index.yaml')
        assert r.status_code == 200
        updated = {**spec, 'base_url':base+(path+'-other' if path else '/other')}
        r = await c.put(nodes[1].url+f'/internal/sources/{kind}/published',headers=local_headers(1),json=updated)
        assert r.status_code == 200, r.text
        await wait_until(lambda: lookup(nodes[2]).base_url == updated['base_url'])
        namespace = 'published' if kind == 'oci' else 'helm/published'
        assert not any(row['upstream'] == namespace for row in nodes[2].app_obj.store.list_manifests()[0])
        r = await c.delete(nodes[1].url+f'/internal/sources/{kind}/published',headers=local_headers(1))
        assert r.status_code == 204, r.text
        await wait_until(lambda: lookup(nodes[2]) is None)


async def test_private_helm_chart_fetches_locally_with_primary_down(cluster):
    nodes, base = cluster
    async with httpx.AsyncClient(trust_env=False, timeout=20) as c:
        r = await c.post(nodes[1].url+'/internal/sources',headers=local_headers(1),json={
            'kind':'helm','name':'my-private-charts','base_url':base+'/charts'})
        assert r.status_code == 201
        nodes[0].stop()
        r = await c.get(nodes[1].url+'/helm/my-private-charts/charts/mychart/1.0.0/mychart-1.0.0.tgz',
                        auth=('admin','local-admin-password-1'))
        assert r.status_code == 200, r.text
        assert nodes[1].app_obj.store.get_manifest('helm/private:my-private-charts','mychart','1.0.0')
        assert nodes[2].app_obj.config.helm_repo('my-private-charts') is None


async def test_read_only_peer_and_protocol_downgrade_are_rejected(cluster):
    nodes, base = cluster
    nodes[0].config.replication_peers['node1']['publish'] = False
    async with httpx.AsyncClient(trust_env=False) as c:
        r = await c.post(nodes[0].url+'/replication/v2/sources',headers=peer_headers(1),json={
            'kind':'oci','name':'forbidden','base_url':base,'sharing':'public','visibility':'public'})
        assert r.status_code == 403
        r = await c.post(nodes[0].url+'/replication/v2/register',headers=peer_headers(1),
                         json={'node_id':'node1','base_url':nodes[1].url})
        assert r.status_code == 409
        assert nodes[0].app_obj.sources.resolve('oci','forbidden') is None
        assert (await c.get(nodes[0].url+'/replication/v2/catalog',headers=peer_headers(1))).status_code == 200


def test_legacy_account_events_and_private_catalogs_are_rejected(tmp_path):
    from containerproxy.store import Store
    store = Store(str(tmp_path))
    with pytest.raises(ValueError,match='account replication'):
        store.apply_account_event('user','copied',b'{}')
    snapshot = {'protocol':'public-v2','upstreams':[{'name':'private','base_url':'https://secret.example',
                'sharing':'local','visibility':'private'}], 'helm':[]}
    with pytest.raises(ValueError,match='invalid public catalog'):
        store.apply_source_event('config',json.dumps(snapshot).encode())
    assert store.config_sources() is None
    store.close()


def test_legacy_databases_require_explicit_migration(tmp_path):
    from containerproxy.api import App
    from containerproxy.store import Store
    cfg=Config(role='primary',storage_path=str(tmp_path/'legacy'),secret='local-secret')
    store=Store(cfg.storage_path)
    create_user(store,'old-user','reader','previous-password')
    store.close()
    with pytest.raises(ValueError,match='offline isolation migration'):
        App(cfg)


@pytest.mark.parametrize('role', ['primary','replica'])
def test_offline_migration_backs_up_and_separates_old_data(tmp_path, role):
    from containerproxy.migrate import migrate
    from containerproxy.store import Store
    cfg=Config(role=role,storage_path=str(tmp_path/'legacy'),secret='new-local-secret',
               replication_id='site-a' if role=='replica' else '',
               replication_token='rep-token' if role=='replica' else '',
               primary_url='https://primary.example' if role=='replica' else None,
               upstreams=[UpstreamConfig('local-registry','https://private.example','local-registry')])
    store=Store(cfg.storage_path)
    create_user(store,'legacy-user','reader','legacy-password')
    create_token(store,'legacy-user','legacy-token')
    store.put_manifest('local-registry','app','v1','sha256:'+'b'*64,'application/json',b'{}')
    path,w=store.new_blob_tmp();w.write(b'private-layer');w.close();store.commit_blob(path,w.digest,w.size)
    store.close()
    backup=tmp_path/'backup'
    migrate(cfg,str(backup))
    assert (backup/'index.db').is_file()
    assert list((backup/'blobs').rglob('*'))
    old=Store(str(backup))
    assert old.get_user('legacy-user') is not None
    old.close()
    migrated=Store(cfg.storage_path)
    assert migrated.get_meta('node_isolation_schema') == '2'
    assert not migrated.list_tokens()
    if role=='primary':
        assert migrated.get_user('legacy-user')['disabled']
        assert migrated.get_manifest('private:local-registry','app','v1') is not None
    else:
        assert migrated.get_user('legacy-user') is None
        assert migrated.list_manifests()[1] == 0
        assert not list(migrated.blobs_dir.rglob('*'))
    migrated.close()


async def test_public_digest_assertion_cannot_export_privately_cached_bytes(cluster, fake_upstream):
    nodes, _ = cluster
    store = nodes[0].app_obj.store
    secret = b'private-layer-that-public-origin-cannot-produce'
    digest = 'sha256:'+hashlib.sha256(secret).hexdigest()
    path,w = store.new_blob_tmp();w.write(secret);w.close();store.commit_blob(path,digest,w.size)
    # A public manifest can assert an arbitrary digest, and an untrusted server
    # can answer 200 without possessing its actual bytes. Neither is proof.
    body = json.dumps({'layers':[{'digest':digest}]}).encode()
    store.put_manifest('docker','forged','latest','sha256:'+hashlib.sha256(body).hexdigest(),'application/json',body)
    fake_upstream.blobs[digest] = b'wrong-public-origin-bytes'
    assert not store.public_blob(digest)
    async with httpx.AsyncClient(trust_env=False, timeout=20) as c:
        r = await c.head(nodes[0].url+'/replication/v2/pull/blob/'+digest,
                         headers=peer_headers(1),params={'upstream':'docker','repo':'forged'})
        assert r.status_code == 502
        r = await c.head(nodes[0].url+'/v2/docker/forged/blobs/'+digest)
        assert r.status_code == 502
        r = await c.get(nodes[0].url+'/replication/v2/pull/blob/'+digest,
                        headers=peer_headers(1),params={'upstream':'@public-feed'})
        assert r.status_code == 404
    assert store.blob_path(digest).read_bytes() == secret
    assert not store.public_blob(digest)


def test_catalog_owner_is_checked_atomically_at_write(tmp_path):
    from containerproxy.store import Store
    store = Store(str(tmp_path))
    spec = {'base_url':'https://public.example','sharing':'public','visibility':'public','owner':'site-a'}
    store.put_source('oci','public',spec,None,expected_owner='site-a')
    with pytest.raises(PermissionError):
        store.put_source('oci','public',{**spec,'owner':'site-b'},None,expected_owner='site-b')
    with pytest.raises(PermissionError):
        store.delete_source('oci','public',expected_owner='site-b')
    with pytest.raises(FileExistsError):
        store.put_source('oci','other',{**spec,'prefix':'public','owner':'site-b'},None,expected_owner='site-b')
    assert store.get_source('oci','public')['spec']['owner'] == 'site-a'
    store.close()


async def test_public_publication_errors_do_not_disclose_private_source_names(cluster):
    nodes, base = cluster
    nodes[0].config.upstream_by_prefix('secret').default = True
    async with httpx.AsyncClient(trust_env=False) as c:
        r = await c.post(nodes[0].url+'/replication/v2/sources',headers=peer_headers(1),json={
            'kind':'oci','name':'new-default','base_url':base,'sharing':'public','visibility':'public','default':True})
        assert r.status_code == 409
        assert 'secret' not in r.text
        assert r.json()['errors'][0]['message'] == 'public catalog conflict'
