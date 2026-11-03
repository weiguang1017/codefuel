"""Real browser traffic through local proxies; no external destinations are contacted."""
import base64
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from socketserver import StreamRequestHandler, ThreadingTCPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from backend.engine import BrowserSession, LOCAL_CHROME
from backend.login_flow import LoginFlow
from backend.proxy import browser_proxy
from backend.providers import PROVIDERS


@pytest.fixture
def local_proxy():
    servers = []
    def create(authenticate):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_CONNECT(self):
                self.send_response(502)
                self.end_headers()
            def do_GET(self):
                expected = 'Basic ' + base64.b64encode(b'fixture-user:fixture-pass').decode()
                if authenticate and self.headers.get('Proxy-Authorization') != expected:
                    self.send_response(407)
                    self.send_header('Proxy-Authenticate', 'Basic realm="fixture"')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                path = urlsplit(self.path).path
                self.server.requests.append(path)
                body = '<h1>Local proxy fixture</h1>'
                kind = 'text/html; charset=utf-8'
                cookie = None
                if path == '/login':
                    body = '''<button onclick="location.href='/oauth/github'">GitHub</button>
                        <script>localStorage.setItem('status', JSON.stringify({github_client_id:'fixture'}))</script>'''
                elif path == '/oauth/github':
                    body = '<script>fetch("/api/oauth/github?code=fixture&state=fixture&mode=login")</script>'
                elif path == '/api/oauth/github':
                    kind = 'application/json'
                    cookie = 'session=fixture-session; Path=/; HttpOnly'
                    body = json.dumps({'success': True, 'data': {'id': 7, 'checked_in': True}})
                elif path == '/api/user/self':
                    kind = 'application/json'
                    body = json.dumps({'success': True, 'data': {'id': 7, 'quota': 50000000, 'used_quota': 0}})
                payload = body.encode()
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(payload)))
                if cookie: self.send_header('Set-Cookie', cookie)
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.requests = []
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        config = {'mode': 'custom', 'server': f'http://127.0.0.1:{server.server_port}', 'authenticate': authenticate}
        if authenticate: config.update(username='fixture-user', password='fixture-pass')
        return server, config
    yield create
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize('authenticated', [True, False])
@pytest.mark.parametrize('flow_kind', ['oauth', 'balance', 'password_context'])
async def test_real_requests_use_only_the_selected_account_proxy(local_proxy, monkeypatch, authenticated, flow_kind):
    import backend.engine as engine
    import backend.login_flow as login
    import cloakbrowser
    from backend.upstream.utils.browser import load_browser_login_settings, launch_login_context
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required')
    target = 'http://codefuel-proxy-fixture.invalid'
    proxy, config = local_proxy(authenticated)
    other, _ = local_proxy(False)
    # An inherited proxy must not receive any part of a custom-routed flow.
    monkeypatch.setenv('CHECKIN_PROXY_URL', f'http://127.0.0.1:{other.server_port}')
    async with async_playwright() as p:
        async def launch(**kwargs):
            assert 'proxy' not in kwargs
            return await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        monkeypatch.setattr(engine, 'launch_async', launch)
        monkeypatch.setattr(login, 'launch_async', launch)
        monkeypatch.setattr(cloakbrowser, 'launch_async', launch)
        if flow_kind == 'balance':
            provider = replace(PROVIDERS['anyrouter'], domain=target, use_proxy=True)
            async with BrowserSession(provider, {'session': 'fixture'}, proxy=browser_proxy(config)) as session:
                response = await session.request('/api/user/self', '7')
                assert response['body']['data']['id'] == 7
            assert '/login' in proxy.requests and '/api/user/self' in proxy.requests
        elif flow_kind == 'password_context':
            settings = load_browser_login_settings('fixture', 'anyrouter', persist_profile=False)
            context = await launch_login_context(settings, use_proxy=True, proxy_override=browser_proxy(config))
            try:
                page = await context.new_page()
                assert (await page.goto(target + '/login')).status == 200
            finally:
                await context.close()
            assert '/login' in proxy.requests
        else:
            monkeypatch.setitem(PROVIDERS, 'agentrouter', replace(PROVIDERS['agentrouter'], domain=target))
            credentials = {'proxy': config, 'oauth_cookies': [
                {'name': 'fixture', 'value': 'fixture', 'domain': '.github.com', 'path': '/'}]}
            flow = LoginFlow(SimpleNamespace(credentials=lambda row: credentials))
            try:
                result = await flow.authenticate_for_checkin(
                    {'id': 1, 'provider': 'agentrouter', 'login_via': 'github', 'name': 'fixture'}, '7')
                assert result.get('profile', {}).get('checked_in') is True, result
                assert result['profile']['quota'] == 50000000
                assert proxy.requests.count('/api/oauth/github') == 1
                assert '/api/user/self' in proxy.requests
            finally:
                await flow.close()
        assert other.requests == []


@pytest.mark.asyncio
async def test_explicit_direct_mode_ignores_inherited_proxy(local_proxy, monkeypatch):
    import backend.engine as engine
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required')
    target, _ = local_proxy(False)
    inherited, _ = local_proxy(False)
    monkeypatch.setenv('CHECKIN_PROXY_URL', f'http://127.0.0.1:{inherited.server_port}')
    async with async_playwright() as p:
        async def launch(**kwargs):
            assert 'proxy' not in kwargs
            return await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True,
                args=[*kwargs.get('args', []), '--host-resolver-rules=MAP codefuel-direct-fixture.invalid 127.0.0.1'])
        monkeypatch.setattr(engine, 'launch_async', launch)
        provider = replace(PROVIDERS['anyrouter'], domain=f'http://codefuel-direct-fixture.invalid:{target.server_port}', use_proxy=True)
        async with BrowserSession(provider, {'session': 'fixture'}, proxy=browser_proxy({'mode': 'direct'})) as session:
            assert (await session.request('/api/user/self', '7'))['status'] == 200
        assert '/api/user/self' in target.requests
        assert inherited.requests == []


@pytest.mark.asyncio
async def test_socks5_routes_hostname_through_the_selected_proxy(monkeypatch):
    import backend.engine as engine
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required')
    hosts = []
    class SocksHandler(StreamRequestHandler):
        def handle(self):
            greeting = self.rfile.read(2)
            if len(greeting) != 2: return
            self.rfile.read(greeting[1])
            self.wfile.write(b'\x05\x00')
            request = self.rfile.read(4)
            if len(request) != 4: return
            if request[3] == 3:
                host = self.rfile.read(self.rfile.read(1)[0]).decode()
            elif request[3] == 1:
                host = '.'.join(str(b) for b in self.rfile.read(4))
            else:
                return
            self.rfile.read(2)
            hosts.append(host)
            self.wfile.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
            while True:
                line = self.rfile.readline(8192)
                if not line or line == b'\r\n': break
            body = b'<title>SOCKS5 fixture</title>'
            self.wfile.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/html\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\n\r\n' + body)
    server = ThreadingTCPServer(('127.0.0.1', 0), SocksHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as p:
            async def launch(**kwargs):
                return await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
            monkeypatch.setattr(engine, 'launch_async', launch)
            provider = replace(PROVIDERS['anyrouter'], domain='http://codefuel-socks-fixture.invalid')
            config = {'mode': 'custom', 'server': f'socks5://127.0.0.1:{server.server_address[1]}'}
            async with BrowserSession(provider, {'session': 'fixture'}, proxy=browser_proxy(config)) as session:
                assert await session.page.title() == 'SOCKS5 fixture'
            assert 'codefuel-socks-fixture.invalid' in hosts
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
