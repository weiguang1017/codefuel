"""Human login stays unattached; only a verified target identity is saved."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import native_login as native
from backend.engine import LOCAL_CHROME
from backend.login_flow import LoginFlow, LoginSession, ProfileVerificationBlocked
from backend.providers import PROVIDERS, CustomProviderIn
from test_app import db, FakeContext


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.fixture
def window(db, monkeypatch):
    import backend.login_flow as login
    state = SimpleNamespace(pages=[SimpleNamespace(url='https://linux.do/session/sso_provider?sig=private',
                                                  title='请稍候…')], attached=0, closed=False, proxy=None)
    target = SimpleNamespace(url='https://agentrouter.org/console')
    context = FakeContext([target], cookies=AsyncMock(return_value=[
        {'name': 'session', 'value': 'fixture-session', 'domain': 'agentrouter.org', 'path': '/'},
        {'name': 'upstream', 'value': 'fixture-login', 'domain': '.linux.do', 'path': '/'}]))
    class Chrome:
        def __init__(self, executable, url, proxy):
            state.proxy = proxy
            self.proxy_auth_required = bool(proxy and proxy.get('username'))
            self.process = SimpleNamespace(returncode=None)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): state.closed = True
        async def tabs(self): return state.pages
        @asynccontextmanager
        async def attached(self):
            state.attached += 1
            try:
                yield SimpleNamespace(), context
            finally:
                state.attached -= 1
    monkeypatch.setattr(native, 'NativeChrome', Chrome)
    monkeypatch.setattr(native, 'POLL_INTERVAL', .01)
    monkeypatch.setattr(native, 'RETURN_SETTLE', .02)
    monkeypatch.setattr(login, 'chrome_user_agent', AsyncMock(return_value='Mozilla/5.0 Chrome/156.0.0.0 Safari/537.36'))
    state.verify = AsyncMock(return_value={'id': 7, 'quota': 50000000, 'used_quota': 0})
    monkeypatch.setattr(login, 'verify_target_profile', state.verify)
    state.flow = LoginFlow(db)
    state.context = context
    state.target = target
    return state


@pytest.mark.asyncio
async def test_no_driver_or_identity_requests_until_human_returns_then_auto_save(db, window):
    session = window.flow.start('agentrouter', 'linuxdo', 'fixture', proxy={'mode': 'direct'})
    await eventually(lambda: session.stage == 'native_login')
    assert window.flow.resume_verification(session.id)
    await asyncio.sleep(.05)
    assert window.attached == 0 and window.verify.await_count == 0 and db.accounts() == []
    assert window.proxy == {'server': 'direct://'}
    assert 'private' not in str(window.flow.view(session.id))
    window.pages = [SimpleNamespace(url=window.target.url, title='AgentRouter')]
    await asyncio.wait_for(session.task, 3)
    assert session.status == 'success' and window.closed and window.attached == 0
    assert window.verify.await_count == 1
    credentials = db.credentials(db.account(session.account))
    assert credentials['session'] == 'fixture-session'
    assert credentials['oauth_cookies'][0]['value'] == 'fixture-login'
    assert credentials['oauth_user_agent'].startswith('Mozilla/')


@pytest.mark.asyncio
async def test_blocked_profile_disconnects_and_waits_for_manual_retry(db, window):
    window.verify.side_effect = [ProfileVerificationBlocked(), {'id': 7}]
    session = window.flow.start('agentrouter', 'linuxdo', 'fixture')
    await eventually(lambda: session.stage == 'native_login')
    window.pages = [SimpleNamespace(url=window.target.url, title='AgentRouter')]
    await eventually(lambda: window.verify.await_count == 1 and session.stage == 'native_login')
    await asyncio.sleep(.08)
    assert window.attached == 0 and window.verify.await_count == 1 and db.accounts() == []
    assert window.flow.resume_verification(session.id)
    await asyncio.wait_for(session.task, 3)
    assert window.verify.await_count == 2 and session.status == 'success'


@pytest.mark.asyncio
@pytest.mark.parametrize('url,title', [
    ('https://agentrouter.org.evil.example/console', 'AgentRouter'),
    ('https://agentrouter.org:8443/console', 'AgentRouter'),
    ('https://agentrouter.org/oauth/linuxdo?code=private', 'AgentRouter'),
    ('https://agentrouter.org/console', 'Just a moment...'),
])
async def test_unfinished_or_wrong_origin_returns_never_attach(db, window, url, title):
    session = window.flow.start('agentrouter', 'linuxdo', 'fixture')
    await eventually(lambda: session.stage == 'native_login')
    window.pages = [SimpleNamespace(url=url, title=title)]
    assert window.flow.resume_verification(session.id)
    await asyncio.sleep(.06)
    window.flow.cancel(session.id)
    await asyncio.gather(session.task, return_exceptions=True)
    assert window.closed and window.attached == 0
    assert window.verify.await_count == 0 and db.accounts() == []


@pytest.mark.asyncio
async def test_browser_close_and_application_shutdown_cancel_without_saving(db, window):
    session = window.flow.start('agentrouter', 'linuxdo', 'fixture')
    await eventually(lambda: session.stage == 'native_login')
    window.pages = []
    await session.task
    assert session.status == 'cancelled' and window.closed
    window.pages = [SimpleNamespace(url='https://linux.do/login', title='Login')]
    second = window.flow.start('agentrouter', 'linuxdo', 'fixture')
    await eventually(lambda: second.stage == 'native_login')
    await window.flow.close()
    assert second.status == 'cancelled' and second.task.done() and db.accounts() == []


@pytest.mark.asyncio
async def test_agentrouter_and_custom_linuxdo_use_native_browser(db, monkeypatch):
    flow = LoginFlow(db)
    controlled = AsyncMock()
    human = AsyncMock()
    monkeypatch.setattr(flow, '_run_window', controlled)
    monkeypatch.setattr(native, 'run_native_login', human)
    monkeypatch.setitem(PROVIDERS, 'custom', CustomProviderIn(name='fixture', domain='https://fixture.example').provider('custom'))
    for provider, method in [('agentrouter', 'linuxdo'), ('agentrouter', 'github'), ('anyrouter', 'linuxdo'), ('custom', 'linuxdo')]:
        await flow._run(LoginSession(provider, method, 'fixture'))
    assert human.await_count == 2 and controlled.await_count == 2


def test_proxy_arguments_never_expose_authentication():
    chrome = native.NativeChrome(None, 'about:blank', {
        'server': 'http://fixture-user:fixture-password@127.0.0.1:18080'})
    assert chrome.proxy_args() == ['--proxy-server=http://127.0.0.1:18080']
    assert chrome.proxy_auth_required
    chrome.proxy = {'server': 'socks5://[::1]:1080'}
    assert chrome.proxy_args() == ['--proxy-server=socks5://[::1]:1080']


@pytest.mark.asyncio
async def test_real_native_chrome_keeps_native_identity_profile_and_selected_proxy(monkeypatch):
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome required')
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_CONNECT(self):
            self.send_error(502)
        def do_GET(self):
            seen.append(self.path)
            body = b'<title>Native fixture</title><h1>Human login fixture</h1>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    try:
        async with native.NativeChrome(LOCAL_CHROME, 'http://native-login.invalid/login',
                {'server': f'http://127.0.0.1:{server.server_port}'}) as chrome:
            directory = chrome.profile.name
            await eventually(lambda: any('/login' in url for url in seen))
            tabs = await chrome.tabs()
            assert any(tab.url == 'http://native-login.invalid/login' for tab in tabs)
            async with chrome.attached() as (browser, context):
                page = context.pages[0]
                assert await page.evaluate('navigator.webdriver') is False
                assert 'HeadlessChrome' not in await page.evaluate('navigator.userAgent')
                await context.add_cookies([{'name': 'fixture', 'value': 'local-only',
                                            'domain': 'native-login.invalid', 'path': '/'}])
            assert chrome.process.returncode is None
            assert await chrome.tabs()
            await chrome.open_target('http://native-login.invalid/continue', close_tabs=[tabs[0].id])
            await eventually(lambda: any('/continue' in url for url in seen))
            async with chrome.attached() as (browser, context):
                assert any(cookie['name'] == 'fixture' for cookie in await context.cookies())
        from pathlib import Path
        assert not Path(directory).exists()
        assert chrome.process.returncode is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_real_native_window_verifies_target_cookie_and_saves_account(db, monkeypatch):
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome required')
    arrived = []
    target = 'http://native-authorize.invalid'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_CONNECT(self): self.send_error(502)
        def do_GET(self):
            path = self.path.removeprefix(target)
            kind, cookie = 'text/html', None
            if path == '/login':
                body = '<script>setTimeout(()=>location.href="/console",500)</script>'
            elif path.startswith('/api/user/self'):
                assert 'session=fixture-verified' in self.headers.get('Cookie', '')
                arrived.append('identity')
                kind = 'application/json'
                body = json.dumps({'success': True, 'data': {'id': 7, 'quota': 50000000, 'used_quota': 0}})
            else:
                body = '<title>Local console</title>'
                cookie = 'session=fixture-verified; Path=/; HttpOnly'
                if path == '/console':
                    arrived.append('console')
            payload = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(payload)))
            if cookie: self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setitem(PROVIDERS, 'agentrouter', replace(PROVIDERS['agentrouter'], domain=target))
    flow = LoginFlow(db)
    try:
        session = flow.start('agentrouter', 'linuxdo', 'local browser', proxy={
            'mode': 'custom', 'server': f'http://127.0.0.1:{server.server_port}'})
        await eventually(lambda: 'console' in arrived and session.stage == 'native_login')
        assert 'identity' not in arrived and db.accounts() == []
        assert flow.resume_verification(session.id)
        await asyncio.wait_for(session.task, 8)
        assert session.status == 'success'
        credentials = db.credentials(db.account(session.account))
        assert credentials['api_user'] == '7' and credentials['session'] == 'fixture-verified'
        assert 'HeadlessChrome' not in credentials['oauth_user_agent']
        assert arrived.count('identity') == 1
    finally:
        await flow.close()
        server.shutdown()
        server.server_close()
        thread.join()
