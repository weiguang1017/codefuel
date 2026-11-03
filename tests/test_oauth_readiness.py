"""Reproduce the site's delayed OAuth configuration in a real, isolated Chrome page."""
import json
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
