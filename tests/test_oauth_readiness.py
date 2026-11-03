"""Reproduce the site's delayed OAuth configuration in a real, isolated Chrome page."""
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
import pytest
from playwright.async_api import async_playwright
from backend.engine import LOCAL_CHROME
from backend.login_flow import LoginFlow, LoginSession
from backend.providers import PROVIDERS


@pytest.mark.asyncio
@pytest.mark.parametrize('method,label,origin', [
    ('github', 'GitHub', 'https://github.com/login/oauth/authorize'),
    ('linuxdo', 'LinuxDO', 'https://connect.linux.do/oauth2/authorize'),
])
async def test_oauth_waits_for_delayed_config_instead_of_sending_undefined_client(method, label, origin):
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required for the isolated browser regression')
    html = '''<meta charset="utf-8"><button id="oauth">使用 LABEL 继续</button>
        <script>
        let config = {};
        document.querySelector('button').onclick = () => window.open(
            'ORIGIN?client_id=' + config.KEY + '&state=fixture-state');
        setTimeout(() => {
            config = {KEY: 'fixture-client'};
            localStorage.setItem('status', JSON.stringify(config));
        }, 700);
        </script>'''.replace('LABEL', label).replace('ORIGIN', origin).replace('KEY', method + '_client_id')
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html; charset=utf-8', body=html if route.request.url.endswith('/login') else 'callback fixture'))
            page = await context.new_page()
            await page.goto('https://agentrouter.org/login')
            assert await page.evaluate('localStorage.getItem("status")') is None
            flow = LoginFlow(SimpleNamespace())
            session = LoginSession('agentrouter', method, 'fixture')
            async with context.expect_page() as pending:
                assert await flow._open_oauth(context, page, session, PROVIDERS['agentrouter'])
            popup = await pending.value
            await popup.wait_for_load_state()
            assert parse_qs(urlsplit(popup.url).query)['client_id'] == ['fixture-client']
            assert session.stage == 'opening_oauth'
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_client', [None, '', 'undefined', 'null'])
async def test_invalid_oauth_config_never_opens_upstream_page(monkeypatch, bad_client):
    import backend.login_flow as login
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required for the isolated browser regression')
    monkeypatch.setattr(login, 'OAUTH_ENTRY_TIMEOUT', .2)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html',
                body='<button onclick="window.open(\'/bad\')">GitHub</button>'))
            page = await context.new_page()
            await page.goto('https://agentrouter.org/login')
            await page.evaluate('(value) => localStorage.setItem("status", JSON.stringify({github_client_id:value}))', bad_client)
            session = LoginSession('agentrouter', 'github', 'fixture')
            assert not await LoginFlow(SimpleNamespace())._open_oauth(context, page, session, PROVIDERS['agentrouter'])
            assert session.stage == 'provider_config_unavailable'
            assert len(context.pages) == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('client,website,action,allowed', [
    ('fixture-client', 'https://agentrouter.org/', '/oauth2/approve?fixture=1', True),
    ('different-client', 'https://agentrouter.org/', '/oauth2/approve?fixture=1', False),
    ('fixture-client', 'https://other.example/', '/oauth2/approve?fixture=1', False),
    ('fixture-client', 'https://agentrouter.org/', 'https://other.example/approve', False),
])
async def test_linuxdo_allow_is_limited_to_current_agentrouter_request(client, website, action, allowed):
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required for the isolated browser regression')
    html = f'''<meta charset="utf-8"><h1>AgentRouter</h1><a href="{website}">应用网站</a>
        <a href="{action}" onclick="event.preventDefault();window.approvals=(window.approvals||0)+1">允许</a>
        <button onclick="window.denied=true">拒绝</button>'''
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html))
            page = await context.new_page()
            await page.goto(f'https://connect.linux.do/oauth2/authorize?client_id={client}&state=fixture-state')
            session = LoginSession('agentrouter', 'linuxdo', 'fixture')
            session.oauth_client_id = 'fixture-client'
            flow = LoginFlow(SimpleNamespace())
            assert await flow._approve_linuxdo(context, session, PROVIDERS['agentrouter']) is allowed
            assert not await flow._approve_linuxdo(context, session, PROVIDERS['agentrouter'])
            assert await page.evaluate('window.approvals||0') == int(allowed)
            assert not await page.evaluate('!!window.denied')
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('action,button_type,expected', [
    ('/oauth2/approve', 'submit', True),
    ('/oauth2/approve/fixture', 'submit', True),
    ('https://other.example/approve', 'submit', False),
    ('/oauth2/deny', 'submit', False),
    ('/oauth2/approve', 'button', False),
])
async def test_linuxdo_approval_form_must_use_official_action(action, button_type, expected):
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required')
    html = f'''<meta charset="utf-8"><a href="https://agentrouter.org">AgentRouter</a>
        <form action="{action}" method="post" onsubmit="event.preventDefault();window.approved=true">
          <input type="hidden" name="fixture" value="fixture">
          <button type="{button_type}">允许</button></form>'''
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
            page = await context.new_page()
            await page.goto('https://connect.linux.do/oauth2/authorize?client_id=fixture&state=fixture')
            session = LoginSession('agentrouter', 'linuxdo', 'fixture')
            session.oauth_client_id = 'fixture'
            flow = LoginFlow(SimpleNamespace())
            assert await flow._approve_linuxdo(context, session, PROVIDERS['agentrouter']) is expected
            assert await page.evaluate('!!window.approved') is expected
            assert not await flow._approve_linuxdo(context, session, PROVIDERS['agentrouter'])
        finally:
            await browser.close()


@pytest.fixture
def callback_server():
    # Chromium's fulfilled/intercepted responses omit Set-Cookie from the
    # response metadata. A real local response exercises cookie provenance too.
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            blocked = self.path.startswith('/api/oauth/linuxdo') and 'HeadlessChrome/' in self.headers.get('User-Agent', '')
            self.send_response(403 if blocked else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Set-Cookie', 'session=fixture-session; Path=/; HttpOnly')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'data': {
                'id': 7, 'checked_in': True, 'quota': 0, 'used_quota': 0}}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize('method,upstream', [('github', 'github.com'), ('linuxdo', 'connect.linux.do')])
@pytest.mark.parametrize('balance_status', [200, 503])
async def test_full_background_oauth_stays_headless_and_confirms_real_callback(monkeypatch, method, upstream, callback_server, balance_status):
    """Exercise window.open, consent, callback cookies and balance fetch without external requests."""
    import backend.login_flow as login
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required for the isolated browser regression')
    target = callback_server
    monkeypatch.setitem(login.PROVIDERS, 'agentrouter', replace(PROVIDERS['agentrouter'], domain=target))
    callback = f'{target}/oauth/{method}?code=fixture&state=fixture&mode=login'
    authorize = f'https://{upstream}/' + ('login/oauth/authorize' if method == 'github' else 'oauth2/authorize')
    callbacks = []
    launches = []

    async def route_request(route):
        url = urlsplit(route.request.url)
        headers = {'content-type': 'text/html; charset=utf-8'}
        if url.hostname == '127.0.0.1' and url.path == '/login':
            body = f'''<meta charset="utf-8"><button onclick="window.open('{authorize}?client_id=fixture-client&state=fixture')">{method}</button>
                <script>localStorage.setItem('status', JSON.stringify({{{method}_client_id:'fixture-client'}}))</script>'''
        elif url.hostname == '127.0.0.1' and url.path == f'/oauth/{method}':
            body = f'<script>fetch("/api/oauth/{method}?code=fixture&state=fixture&mode=login")</script>'
        elif url.hostname == '127.0.0.1' and url.path == f'/api/oauth/{method}':
            callbacks.append(url.path)
            await route.continue_()
            return
        elif url.hostname == '127.0.0.1' and url.path == '/api/user/self':
            await route.fulfill(status=balance_status, content_type='application/json',
                body=json.dumps({'success': True, 'data': {'id': 7, 'quota': 62500000, 'used_quota': 0}}))
            return
        elif url.hostname == upstream and method == 'linuxdo' and url.path == '/oauth2/authorize':
            body = f'<a href="{target}">AgentRouter</a><a href="/oauth2/approve">允许</a>'
        elif url.hostname == upstream:
            body = f'<script>location.href="{callback}"</script>'
        else:
            body = ''
        await route.fulfill(status=200, headers=headers, body=body)

    credentials = {'oauth_cookies': [{'name': 'upstream', 'value': 'fixture-cache',
                                     'domain': '.' + ('github.com' if method == 'github' else 'linux.do'), 'path': '/'}]}
    flow = LoginFlow(SimpleNamespace(credentials=lambda row: credentials))
    async with async_playwright() as playwright:
        async def launch(**kwargs):
            launches.append(kwargs)
            assert kwargs['headless'] is True and 'args' not in kwargs
            browser = await playwright.chromium.launch(executable_path=LOCAL_CHROME, headless=kwargs['headless'])
            async def new_context(**options):
                context = await browser.new_context(**options)
                await context.route('**/*', route_request)
                return context
            return SimpleNamespace(new_context=new_context, close=browser.close, is_connected=browser.is_connected,
                                   new_browser_cdp_session=browser.new_browser_cdp_session)
        monkeypatch.setattr(login, 'launch_async', launch)
        monkeypatch.setattr(login, 'get_playwright_proxy', lambda **kwargs: None)
        try:
            result = await flow.authenticate_for_checkin(
                {'id': 1, 'provider': 'agentrouter', 'login_via': method, 'name': 'fixture'}, '7')
            assert 'profile' in result, (result, callbacks)
            assert result['profile']['checked_in'] is True
            if balance_status == 200:
                assert result['profile']['quota'] == 62500000
            else:
                assert 'quota' not in result['profile']
            assert result['session'] == 'fixture-session'
            if method == 'linuxdo':
                assert 'Chrome/' in result['oauth_user_agent'] and 'HeadlessChrome/' not in result['oauth_user_agent']
            assert result['attempts'] == 1
            assert callbacks == [f'/api/oauth/{method}']
            assert len(launches) == 1 and flow.sessions == {}
        finally:
            await flow.close()
