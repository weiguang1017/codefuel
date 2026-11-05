"""Custom sites use temporary databases, mock responses and local browser fixtures only."""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

from backend import main, engine
from backend.login_flow import LoginFlow, LoginSession
from backend.providers import PROVIDERS, CustomProviderIn, load_custom_providers
from backend.store import Store


@pytest.fixture
def local(tmp_path, monkeypatch):
    original = dict(PROVIDERS)
    db = Store(tmp_path / 'sites.db', tmp_path / 'key')
    monkeypatch.setattr(main, 'store', db)
    monkeypatch.setattr(main, 'login_flow', LoginFlow(db))
    monkeypatch.setattr(main, 'active', set())
    monkeypatch.setattr(main, 'queued', set())
    load_custom_providers([])
    client = TestClient(main.app, base_url='http://127.0.0.1')
    client.headers.update({'X-CSRF-Token': main.CSRF})
    yield client, db
    client.close()
    db.close()
    PROVIDERS.clear()
    PROVIDERS.update(original)


def add(client, **values):
    response = client.post('/api/providers', json={'name': '测试站点', 'domain': 'https://custom.example', **values})
    assert response.status_code == 201, response.text
    return response.json()['id']


def account(client, provider):
    response = client.post('/api/accounts', json={'name': '隔离测试账号', 'provider': provider,
                           'auth_mode': 'session', 'session': 'fixture-session', 'api_user': '7'})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_custom_site_lifecycle_and_restart(local):
    client, db = local
    provider_id = add(client)
    assert PROVIDERS[provider_id].sign_in_path == '/api/user/checkin'
    assert client.put('/api/providers/' + provider_id, json={
        'name': '改名站点', 'domain': 'https://custom.example', 'template': 'sign_in'}).status_code == 200
    reopened = Store(db.db_path, db.key_path)
    load_custom_providers(reopened.provider_configs())
    reopened.close()
    assert PROVIDERS[provider_id].display_name == '改名站点'
    assert PROVIDERS[provider_id].sign_in_path == '/api/user/sign_in'
    assert client.request('DELETE', '/api/providers/' + provider_id, json={}).status_code == 200
    load_custom_providers(db.provider_configs())
    assert not PROVIDERS[provider_id].active
    assert client.post('/api/accounts', json={'name': 'removed', 'provider': provider_id,
        'auth_mode': 'session', 'session': 'fixture', 'api_user': '7'}).status_code == 422
    assert len([p for p in client.get('/api/bootstrap').json()['providers'] if not p['custom']]) == 2


@pytest.mark.parametrize('method', ['PUT', 'DELETE'])
def test_builtins_are_readonly(local, method):
    client, _ = local
    payload = {'name': 'changed', 'domain': 'https://custom.example'} if method == 'PUT' else {}
    for provider in ('anyrouter', 'agentrouter'):
        assert client.request(method, '/api/providers/' + provider, json=payload).status_code == 409


@pytest.mark.parametrize('values', [
    {'domain': 'http://custom.example'}, {'domain': 'https://user:secret@custom.example'},
    {'domain': 'https://custom.example/v1'}, {'domain': 'https://custom.example?token=secret'},
    {'domain': 'https://custom.example/#token'}, {'domain': 'https://custom.example\\@evil.example'},
    {'domain': 'https://custom.example:0'}, {'sign_in_path': '//evil.example/check'},
    {'sign_in_path': '/api/../check'}, {'sign_in_path': '/api/%2f/check'},
    {'user_info_path': '/api/self?token=secret'}, {'api_user_key': 'cookie'},
    {'api_user_key': 'x-user\r\nCookie'}, {'quota_per_unit': 0}, {'name': '   '},
    {'script': 'untrusted'}, {'currency': 'OTHER'},
])
def test_invalid_configuration_is_rejected_without_echoing_inputs(local, values):
    client, db = local
    result = client.post('/api/providers', json={'name': '测试', 'domain': 'https://custom.example', **values})
    assert result.status_code == 422
    assert 'secret' not in result.text and 'untrusted' not in result.text
    assert not db.provider_configs()


def test_canonical_domain_duplicate_and_csrf(local):
    client, _ = local
    add(client, domain='https://CUSTOM.example:443/')
    assert client.post('/api/providers', json={'name': 'duplicate', 'domain': 'https://custom.example'}).status_code == 409
    assert client.post('/api/providers', json={'name': 'builtin', 'domain': 'https://anyrouter.top'}).status_code == 409
    assert client.post('/api/providers', headers={'X-CSRF-Token': 'wrong'}, json={
        'name': 'blocked', 'domain': 'https://blocked.example'}).status_code == 403


def test_accounts_prevent_domain_unit_changes_and_removal(local):
    client, db = local
    provider_id = add(client)
    account_id = account(client, provider_id)
    for change in ({'domain': 'https://other.example'}, {'currency': 'CNY'}, {'quota_per_unit': 1}):
        result = client.put('/api/providers/' + provider_id, json={
            'name': '测试站点', 'domain': 'https://custom.example', **change})
        assert result.status_code == 409
    assert client.request('DELETE', '/api/providers/' + provider_id, json={}).status_code == 409
    assert db.credentials(db.account(account_id))['session'] == 'fixture-session'
    assert client.put('/api/providers/' + provider_id, json={
        'name': 'new name', 'domain': 'https://custom.example', 'sign_in_path': '/api/custom_check'}).status_code == 200
    assert PROVIDERS[provider_id].sign_in_path == '/api/custom_check'


def test_running_and_new_authorization_block_config_changes(local):
    client, db = local
    provider_id = add(client)
    session = LoginSession(provider_id, 'manual', 'test', None, 6)
    session.status = 'waiting'
    main.login_flow.sessions[session.id] = session
    payload = {'name': 'changed', 'domain': 'https://custom.example'}
    assert client.put('/api/providers/' + provider_id, json=payload).status_code == 409
    assert client.request('DELETE', '/api/providers/' + provider_id, json={}).status_code == 409
    session.status = 'cancelled'
    account_id = account(client, provider_id)
    main.queued.add(account_id)
    assert client.put('/api/providers/' + provider_id, json=payload).status_code == 409
    main.queued.clear()
    db.patch(account_id, status='running')
    assert client.put('/api/providers/' + provider_id, json=payload).status_code == 409


@pytest.mark.asyncio
async def test_custom_browser_login_persists_correct_unit(local):
    client, db = local
    provider_id = add(client, currency='credits', quota_per_unit=10)
    session = LoginSession(provider_id, 'manual', 'test', None, 6)
    session.status = 'waiting'
    await LoginFlow(db)._persist(session, {'id': 7, 'quota': 150, 'used_quota': 20}, 'fixture-session')
    assert session.status == 'success'
    assert db.account(session.account)['balance'] == 15
    assert db.public_account(db.account(session.account))['currency'] == 'credits'


@pytest.mark.asyncio
@pytest.mark.parametrize('template,status,body,expected', [
    ('newapi', 200, {'success': True}, 'success'),
    ('newapi', 200, {'success': False, 'message': '今日已签到'}, 'already_checked'),
    ('newapi', 200, {'success': False, 'code': 0}, 'failed'),
    ('newapi', 200, {'code': 0}, 'failed'),
    ('newapi', 200, '<html>success</html>', 'failed'),
    ('newapi', 403, {}, 'manual_required'),
    ('newapi', 404, {}, 'failed'),
    ('sign_in', 200, {'ret': 1}, 'success'),
    ('sign_in', 200, {'code': False}, 'failed'),
    ('sign_in', 200, {'ret': True}, 'failed'),
    ('balance', 200, {}, 'synced'),
])
async def test_custom_checkin_modes(local, monkeypatch, template, status, body, expected):
    client, db = local
    provider_id = add(client, template=template, quota_per_unit=10, currency='CNY',
                      user_info_path='/profile', api_user_key='x-user')
    account_id = account(client, provider_id)
    calls = []
    class Session:
        def __init__(self, provider, cookies, **kwargs):
            assert provider.api_user_key == 'x-user'
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, path, user, method='GET', payload=None):
            calls.append((path, method))
            if method == 'POST': return {'status': status, 'body': body}
            quota = 200 if len(calls) == 1 else 230
            return {'status': 200, 'body': {'success': True, 'data': {'id': 7, 'quota': quota, 'used_quota': 50}}}
    monkeypatch.setattr(engine, 'BrowserSession', Session)
    relogin = AsyncMock()
    runner = engine.CheckinEngine(reauthenticate=relogin)
    result = await runner.run(db.account(account_id), db.credentials(db.account(account_id)))
    assert result['status'] == expected
    assert result['before_balance'] == 20
    relogin.assert_not_awaited()
    if template == 'balance':
        assert calls == [('/profile', 'GET')]
        assert result['reward'] is None
    else:
        assert len([c for c in calls if c[1] == 'POST']) == 1
    if expected == 'success':
        assert result['reward'] == 3
        assert '¥3.00' in result['message']


def test_currency_and_per_site_credits_are_not_summed(local):
    client, db = local
    for i, currency in enumerate(['USD', 'CNY', 'credits', 'credits']):
        pid = add(client, domain=f'https://site{i}.example', currency=currency)
        aid = account(client, pid)
        run = db.begin_run(db.account(aid), 'manual')
        db.finish_run(run, aid, {'status': 'success', 'message': 'fixture', 'reward': i + 1,
                                'after_balance': 10 * (i + 1)}, 'manual')
    data = db.dashboard()
    assert data['stats']['balance'] == 10 and data['stats']['today_reward'] == 1
    assert data['amounts']['CNY']['balance'] == 20
    credit_groups = [k for k in data['amounts'] if k.startswith('credits:')]
    assert len(credit_groups) == 2
    assert sorted(data['amounts'][k]['balance'] for k in credit_groups) == [30, 40]
    last = db.accounts()[-1]
    pid = last['provider']
    db.delete_account(last['id'])
    assert client.put('/api/providers/' + pid, json={
        'name': 'wrong unit', 'domain': 'https://site3.example', 'currency': 'USD'}).status_code == 409
    assert client.request('DELETE', '/api/providers/' + pid, json={}).status_code == 200
    assert db.runs()[0]['currency'] == 'credits'
    assert 'credits' in client.get('/api/logs/export').text


def test_probe_only_reads_public_status(local, monkeypatch):
    client, _ = local
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'success': True, 'data': {'system_name': 'test', 'checkin_enabled': False}})
    original = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    response = client.post('/api/providers/probe', json={'name': 'test', 'domain': 'https://custom.example'})
    assert response.status_code == 200 and response.json()['checkin_enabled'] is False
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert str(requests[0].url) == 'https://custom.example/api/status'
    assert not requests[0].headers.get('cookie') and not requests[0].headers.get('authorization')


@pytest.mark.asyncio
async def test_scheduled_custom_site_uses_shared_queue_and_logs(local, monkeypatch):
    client, db = local
    provider_id = add(client, currency='CNY', quota_per_unit=10)
    account_id = account(client, provider_id)
    runner = AsyncMock(return_value={'status': 'success', 'message': 'fixture', 'reward': 3,
                                    'after_balance': 23, 'used_quota': 5, 'total_quota': 28})
    monkeypatch.setattr(main, 'engine', SimpleNamespace(run=runner))
    monkeypatch.setattr(main, 'jobs', set())
    assert main.enqueue(account_id, 'scheduled')
    assert not main.enqueue(account_id, 'scheduled')
    await asyncio.gather(*main.jobs)
    runner.assert_awaited_once()
    assert db.account(account_id)['status'] == 'success'
    assert db.runs()[0]['provider'] == provider_id
    assert db.runs()[0]['currency'] == 'CNY'
    assert db.runs()[0]['trigger'] == 'scheduled'


@pytest.mark.asyncio
async def test_browser_request_respects_custom_header_and_origin(monkeypatch):
    if not engine.LOCAL_CHROME: pytest.skip('Local Chrome required')
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == '/profile': received.append(dict(self.headers))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if self.path == '/profile' else 'text/html')
            self.end_headers()
            self.wfile.write(b'{"success":true}' if self.path == '/profile' else b'<html>fixture</html>')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True);thread.start()
    domain = f'http://127.0.0.1:{server.server_port}'
    provider = replace(CustomProviderIn(name='test', domain='https://custom.example', api_user_key='x-user').provider('test'), domain=domain)
    monkeypatch.setattr(engine, 'REQUEST_SPACING', 0)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=engine.LOCAL_CHROME, headless=True)
            page = await browser.new_page()
            await page.goto(domain)
            session = engine.BrowserSession(provider, {})
            session.page = page
            assert (await session.request('/profile', '7'))['status'] == 200
            assert received[-1]['x-user'] == '7' and 'new-api-user' not in received[-1]
            with pytest.raises(Exception, match='Unexpected site origin'):
                await session.request('https://elsewhere.example/profile', '7')
            await page.goto('about:blank')
            with pytest.raises(Exception, match='Unexpected site origin'):
                await session.request('/profile', '7')
            assert len(received) == 1
            await browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
