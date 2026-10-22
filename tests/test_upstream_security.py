"""Untrusted registry pagination must not escape its configured origin."""
import httpx
import pytest

from ociproxy.config import UpstreamConfig
from ociproxy.upstream import UpstreamClient, UpstreamError


@pytest.mark.parametrize('link', [
    'https://attacker.example/v2/app/tags/list',
    '//attacker.example/v2/app/tags/list',
    'http://registry.example/v2/app/tags/list',
    'https://registry.example:444/v2/app/tags/list',
    'https://user:pass@registry.example/v2/app/tags/list',
])
async def test_pagination_cannot_contact_other_origins(link):
    visited = []

    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(200, json={'tags': ['v1']},
                              headers={'Link': f'<{link}>; rel="next"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = UpstreamClient(UpstreamConfig('test', 'https://registry.example', 'test'), http)
        with pytest.raises(UpstreamError, match='pagination URL'):
            await client.list_tags('app')
    assert len(visited) == 1


async def test_same_origin_absolute_pagination():
    def handler(request):
        if request.url.params.get('last'):
            return httpx.Response(200, json={'tags': ['v2']})
        return httpx.Response(200, json={'tags': ['v1']}, headers={
            'Link': '<https://registry.example/v2/app/tags/list?last=v1>; rel="next"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = UpstreamClient(UpstreamConfig('test', 'https://registry.example', 'test'), http)
        assert await client.list_tags('app') == ['v1', 'v2']


async def test_https_registry_cannot_downgrade_token_exchange():
    visited = []

    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(401, headers={
            'WWW-Authenticate': 'Bearer realm="http://tokens.example/token",service="registry"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = UpstreamClient(UpstreamConfig('test', 'https://registry.example', 'test',
                                               username='user', password='private'), http)
        with pytest.raises(UpstreamError, match='unsafe upstream token endpoint'):
            await client.list_tags('app')
    assert len(visited) == 1


async def test_oidc_discovery_cannot_downgrade_token_exchange():
    from ociproxy_ui.oidc import OIDCClient, OIDCError, OIDCSettings

    def handler(request):
        return httpx.Response(200, json={
            'issuer': 'https://idp.example',
            'authorization_endpoint': 'https://idp.example/authorize',
            'token_endpoint': 'http://attacker.example/token',
        })

    settings = OIDCSettings('https://idp.example', 'client', 'private',
                            'https://ui.example/auth/oidc/callback')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OIDCError, match='unsafe token_endpoint'):
            await OIDCClient(settings, http).metadata()
