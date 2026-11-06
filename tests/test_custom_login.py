"""Custom password and Sub2API OAuth fixtures. All account/network data is local and synthetic."""
import asyncio
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from backend import engine, login_flow
from backend.providers import CustomProviderIn, PROVIDERS
from backend.protocols import resolve_custom_protocol
from backend.store import Store


@pytest.mark.parametrize('label', ['使用 Linux.do 登录', 'Linux.do', '使用 LINUX DO 登录', 'LinuxDO', '通过 Linux.do 授权'])
def test_linuxdo_entry_labels(label):
    assert login_flow.OAUTH_BUTTONS['linuxdo'].fullmatch(label)


@pytest.fixture
def database(tmp_path):
    db = Store(tmp_path / 'accounts.db', tmp_path / 'key')
    yield db
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('template', ['newapi', 'sub2api'])
async def test_custom_password_uses_protocol_login_and_confirms_profile(database, monkeypatch, template):
    provider = CustomProviderIn(name='fixture', domain='https://fixture.example', template=template).provider('fixture')
    monkeypatch.setitem(PROVIDERS, 'fixture', provider)
    aid = database.add_account({'name': 'fixture', 'provider': 'fixture', 'auth_mode': 'password', 'email': 'fixture-user',
                                'password': 'fixture-password', 'interval_hours': 6})
    calls = []
    class Session:
        def __init__(self, p, cookies, **kwargs): self.provider = p;self.auth_token = None
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, path, user, method='GET', payload=None, auth=True):
            calls.append((path, method))
            if path == provider.login_api_path:
                assert auth is False and user is None
                assert payload == {'email' if template == 'sub2api' else 'username': 'fixture-user', 'password': 'fixture-password'}
                return {'status': 200, 'body': {'code': 0, 'data': {'access_token': 'fixture-token', 'user': {'id': 7}}} if template == 'sub2api' else {'success': True, 'data': {'id': 7}}}
            if path == provider.sign_in_path:
                return {'status': 200, 'body': {'code': 0, 'data': {'reward': 2.5}} if template == 'sub2api' else {'success': True}}
            assert str(user) == '7'
            if template == 'sub2api':
                assert self.auth_token == 'fixture-token'
                return {'status': 200, 'body': {'code': 0, 'data': {'id': 7, 'balance': 12.5}}}
            return {'status': 200, 'body': {'success': True, 'data': {'id': 7, 'quota': 6250000, 'used_quota': 0}}}
    monkeypatch.setattr(engine, 'BrowserSession', Session)
    result = await engine.CheckinEngine().run(database.account(aid), database.credentials(database.account(aid)))
    assert result['status'] == 'success' and result['after_balance'] == 12.5
    assert calls == [(provider.login_api_path, 'POST'), (provider.user_info_path, 'GET'), (provider.sign_in_path, 'POST'), (provider.user_info_path, 'GET')]
    if template == 'sub2api':
        assert result['reward'] == 2.5 and result['used_quota'] is None and result['total_quota'] is None
    assert 'fixture-password' not in result['message'] and 'fixture-token' not in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [7, 8, None, True, 0])
async def test_expired_sub2api_token_refresh_is_identity_checked(database, monkeypatch, identity):
    provider = CustomProviderIn(name='fixture', domain='https://fixture.example', template='sub2api').provider('fixture')
    monkeypatch.setitem(PROVIDERS, 'fixture', provider)
    aid = database.add_account({'name': 'fixture', 'provider': 'fixture', 'auth_mode': 'session', 'session': 'old-access',
                                'refresh_token': 'old-refresh', 'api_user': '7', 'interval_hours': 6})
    calls = []
    class Session:
        def __init__(self, p, cookies, **kwargs):
            assert cookies == {};self.auth_token = kwargs['auth_token']
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def export_site_cookies(self): return []
        async def request(self, path, user, method='GET', payload=None, auth=True):
            calls.append((path, method))
            if path.endswith('/auth/refresh'):
                assert not auth and payload == {'refresh_token': 'old-refresh'}
                return {'status': 200, 'body': {'code': 0, 'data': {'access_token': 'new-access', 'refresh_token': 'new-refresh'}}}
            if path == provider.sign_in_path:
                return {'status': 200, 'body': {'code': 0, 'data': {'reward': 0}}}
            if self.auth_token == 'old-access': return {'status': 401, 'body': {'code': 401}}
            return {'status': 200, 'body': {'code': 0, 'data': {'id': identity, 'balance': 8}}}
    monkeypatch.setattr(engine, 'BrowserSession', Session)
    result = await engine.CheckinEngine().run(database.account(aid), database.credentials(database.account(aid)))
    if identity == 7:
        assert result['status'] == 'success'
        assert result['_session_update']['session'] == 'new-access'
        assert result['_session_update']['refresh_token'] == 'new-refresh'
    else:
        assert result['status'] == 'failed'
        assert '_session_update' not in result
        assert not any(path == provider.sign_in_path for path, _ in calls)
    assert sum(path.endswith('/auth/refresh') for path, _ in calls) == 1


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            path = urlsplit(self.path).path
            status, kind = 200, 'text/html; charset=utf-8'
            if path == '/login':
                body = '''<meta charset="utf-8"><button onclick="location.href='https://connect.linux.do/oauth2/authorize?state=fixture'">使用 Linux.do 登录</button>
                <script>setTimeout(()=>{window.__APP_CONFIG__={site_name:'fixture',linuxdo_oauth_enabled:true}},100)</script>'''
            elif path == '/auth/linuxdo/callback':
                body = '''<script>localStorage.setItem('auth_token','fixture-access');localStorage.setItem('refresh_token','fixture-refresh');location.replace('/dashboard')</script>'''
            elif path == '/api/v1/auth/me':
                self.server.identity_reads += 1
                assert self.headers.get('Authorization') == 'Bearer fixture-access'
                assert self.headers.get('X-User-UI-Request') == '1'
                kind = 'application/json'
                body = json.dumps({'code': 0, 'data': {'id': 7, 'balance': 23.5}})
            else: body = '<h1>Fixture dashboard</h1>'
            payload = body.encode()
            self.send_response(status);self.send_header('Content-Type', kind);self.send_header('Content-Length', str(len(payload)))
            self.end_headers();self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.identity_reads = 0
    thread = Thread(target=server.serve_forever, daemon=True);thread.start()
    yield server
    server.shutdown();server.server_close();thread.join()


@pytest.mark.asyncio
async def test_sub2api_linuxdo_redirect_callback_and_encrypted_account_save(site, database, monkeypatch):
    if not engine.LOCAL_CHROME: pytest.skip('Local Chrome required')
    origin = f'http://127.0.0.1:{site.server_port}'
    config = CustomProviderIn(name='LOXO fixture', domain='https://fixture.example', template='sub2api')
    monkeypatch.setitem(PROVIDERS, 'fixture', replace(config.provider('fixture'), domain=origin))
    jumps = []
    async with async_playwright() as pw:
        async def launch(**kwargs):
            browser = await pw.chromium.launch(executable_path=engine.LOCAL_CHROME, headless=True)
            async def new_context(**options):
                context = await browser.new_context(**options)
                async def route_request(route):
                    host = urlsplit(route.request.url).hostname
                    if host == 'connect.linux.do':
                        jumps.append(True)
                        await route.fulfill(content_type='text/html', body=f'<script>location.href="{origin}/auth/linuxdo/callback"</script>')
                    elif host == '127.0.0.1': await route.continue_()
                    else: await route.abort()
                await context.route('**/*', route_request)
                return context
            return SimpleNamespace(new_context=new_context, close=browser.close)
        monkeypatch.setattr(login_flow, 'launch_async', launch)
        flow = login_flow.LoginFlow(database)
        session = login_flow.LoginSession('fixture', 'linuxdo', 'fixture account')
        flow.sessions[session.id] = session
        session.task = asyncio.create_task(flow._run_window(session))
        await asyncio.wait_for(session.task, timeout=25)
        assert session.status == 'success', session.message
        assert jumps and site.identity_reads >= 1
        row = database.account(session.account)
        credentials = database.credentials(row)
        assert credentials['session'] == 'fixture-access' and credentials['refresh_token'] == 'fixture-refresh'
        assert row['balance'] == 23.5 and row['used_quota'] is None and row['total_quota'] is None
        assert 'fixture-access' not in row['encrypted']
        assert not any(key in database.public_account(row) for key in ('session', 'refresh_token', 'api_user'))
        await flow.close()


@pytest.mark.asyncio
async def test_new_default_site_is_resolved_to_sub2api_before_credentials(database, monkeypatch):
    config = CustomProviderIn(name='fixture', domain='https://fixture.example')
    provider = config.provider('fixture')
    monkeypatch.setitem(PROVIDERS, 'fixture', provider)
    database.save_provider('fixture', config.model_dump())
    from unittest.mock import AsyncMock
    handle = SimpleNamespace(json_value=AsyncMock(return_value='sub2api'), dispose=AsyncMock())
    page = SimpleNamespace(wait_for_function=AsyncMock(return_value=handle))
    resolved = await resolve_custom_protocol(page, provider, database)
    assert resolved.template == 'sub2api' and resolved.domain == provider.domain
    assert resolved.user_info_path == '/api/v1/auth/me' and resolved.quota_per_unit == 1
    assert database.provider_configs()[0]['config']['template'] == 'sub2api'
