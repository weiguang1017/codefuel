import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.async_api import async_playwright

from backend import site_probe
from backend.engine import LOCAL_CHROME


@pytest.mark.parametrize('path,body,protocol,mode', [
    ('/api/status', {'success': True, 'data': {'system_name': 'test', 'checkin_enabled': True}}, 'newapi', 'newapi'),
    ('/api/status', {'success': True, 'data': {'system_name': 'test', 'checkin_enabled': False}}, 'newapi', 'balance'),
    ('/api/status', {'success': True, 'data': {'system_name': 'test', 'sign_in_enabled': True}}, 'newapi', 'sign_in'),
    ('/api/v1/settings/public', {'code': 0, 'data': {'site_name': 'test', 'check_in_enabled': True}}, 'sub2api', 'sub2api'),
    ('/api/v1/settings/public', {'code': 0, 'data': {'site_name': 'test', 'check_in_enabled': False}}, 'sub2api', 'balance'),
])
def test_detected_mode_and_protocol_defaults(path, body, protocol, mode):
    result = site_probe.classify_config('https://fixture.example', path, body)
    assert result['recognized'] and result['protocol'] == protocol and result['template'] == mode
    assert result['defaults']['login_api_path'] == ('/api/v1/auth/login' if protocol == 'sub2api' else '/api/user/login')
    if mode == 'balance': assert result['defaults']['sign_in_path'] is None


@pytest.mark.parametrize('body', [None, '<html>error</html>', {'code': False, 'data': {'site_name': 'test'}}, {'success': True, 'data': {}}])
def test_unknown_documents_are_not_claimed_as_supported(body):
    assert not site_probe.classify_config('https://fixture.example', '/api/v1/settings/public', body)


@pytest.mark.asyncio
async def test_public_protocols_are_probed_without_selected_mode_or_credentials(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == '/api/status': return httpx.Response(404)
        return httpx.Response(200, json={'code': 0, 'data': {'site_name': 'fixture', 'linuxdo_oauth_enabled': True, 'check_in_enabled': True}})
    original = httpx.AsyncClient
    monkeypatch.setattr(site_probe.httpx, 'AsyncClient', lambda **options: original(transport=httpx.MockTransport(handler), **options))
    browser = AsyncMock()
    monkeypatch.setattr(site_probe, '_browser_config', browser)
    result = await site_probe.probe_site('https://fixture.example')
    assert result['template'] == 'sub2api'
    assert len(requests) == 2 and all(r.method == 'GET' for r in requests)
    assert all(not r.headers.get('authorization') and not r.headers.get('cookie') for r in requests)
    browser.assert_not_awaited()


def test_conflicting_protocols_require_manual_choice():
    result = site_probe.choose_result([
        {'recognized': True, 'protocol': 'newapi'}, {'recognized': True, 'protocol': 'sub2api'}])
    assert result['recognized'] is False and '多种' in result['message']


@pytest.mark.asyncio
async def test_failure_is_non_destructive_and_never_claims_detection(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(site_probe.httpx, 'AsyncClient', lambda **options: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, text='blocked')), **options))
    monkeypatch.setattr(site_probe, 'browser_available', lambda: False)
    result = await site_probe.probe_site('https://fixture.example')
    assert result['recognized'] is False and 'template' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['sub2api', 'legacy'])
async def test_browser_fallback_detects_public_page_without_login_or_checkin(monkeypatch, protocol):
    if not LOCAL_CHROME: pytest.skip('Local Chrome required')
    original = httpx.AsyncClient
    monkeypatch.setattr(site_probe.httpx, 'AsyncClient', lambda **options: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(403)), **options))
    monkeypatch.setattr(site_probe, 'browser_available', lambda: True)
    mutations = []
    closed = []
    async with async_playwright() as pw:
        async def launch(**options):
            assert options['headless'] is True
            browser = await pw.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
            async def new_context(**kwargs):
                context = await browser.new_context(**kwargs)
                async def route_request(route):
                    path = urlsplit(route.request.url).path
                    if route.request.method != 'GET': mutations.append(path)
                    if path == '/login':
                        config = "window.__APP_CONFIG__={site_name:'fixture',linuxdo_oauth_enabled:true,check_in_enabled:true}" if protocol == 'sub2api' else "localStorage.setItem('status',JSON.stringify({system_name:'fixture'}))"
                        await route.fulfill(content_type='text/html', body=f"<script>{config};fetch('/api/user/checkin',{{method:'POST'}}).catch(()=>{{}})</script><script src='/app.js'></script>")
                    elif path == '/app.js':
                        await route.fulfill(content_type='application/javascript', body="window.fixturePath='/api/user/sign_in';")
                    else: await route.fulfill(status=404)
                await context.route('**/*', route_request)
                return context
            async def close():
                closed.append(True)
                await browser.close()
            return SimpleNamespace(new_context=new_context, close=close)
        monkeypatch.setattr(site_probe, 'launch_async', launch)
        result = await site_probe.probe_site('https://fixture.example')
        assert result['template'] == ('sub2api' if protocol == 'sub2api' else 'sign_in'), result
        assert not mutations and closed == [True]


@pytest.mark.asyncio
async def test_browser_is_closed_on_navigation_failure(monkeypatch):
    browser = SimpleNamespace(new_context=AsyncMock(side_effect=TimeoutError()), close=AsyncMock())
    monkeypatch.setattr(site_probe, 'launch_async', AsyncMock(return_value=browser))
    assert await site_probe._browser_config('https://fixture.example') == []
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnected_client_cancels_pending_detection(monkeypatch):
    from backend import main
    from fastapi import HTTPException
    cancelled = asyncio.Event()
    async def slow_probe(domain):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(main, 'probe_site', slow_probe)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as error:
        await main.probe_provider(main.ProviderProbeIn(domain='https://fixture.example'), request)
    assert error.value.status_code == 499 and cancelled.is_set()
