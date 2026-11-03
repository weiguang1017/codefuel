"""Regressions for native interactive Chrome and incomplete LinuxDO SSO."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from backend.engine import LOCAL_CHROME
from backend.login_flow import LoginFlow, LoginSession
from backend.providers import PROVIDERS
from test_app import db, FakeContext


@pytest.mark.asyncio
async def test_interactive_linuxdo_keeps_native_client_hints(db, monkeypatch):
    import backend.login_flow as login
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required')
    hints = "async()=>await navigator.userAgentData.getHighEntropyValues(['architecture','platformVersion'])"
    snapshots, options = {}, []
    async with async_playwright() as p:
        async def launch(**kwargs):
            assert kwargs['headless'] is False
            # Run the interactive code path without opening a test window.
            browser = await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
            control = await browser.new_context()
            await control.route('**/*', lambda route: route.fulfill(body='<h1>fixture</h1>'))
            page = await control.new_page()
            await page.goto('https://identity.example/')
            snapshots['native'] = await page.evaluate(hints)
            async def new_context(**kwargs):
                options.append(kwargs)
                context = await browser.new_context(**kwargs)
                await context.route('**/*', lambda route: route.fulfill(body='<h1>fixture</h1>'))
                return context
            return SimpleNamespace(new_context=new_context, close=browser.close,
                                   new_browser_cdp_session=browser.new_browser_cdp_session)
        monkeypatch.setattr(login, 'launch_async', launch)
        monkeypatch.setattr(login, 'get_playwright_proxy', lambda **kw: None)
        flow = LoginFlow(db)
        async def observe(context, page, session, provider):
            snapshots['interactive'] = await page.evaluate(hints)
            session.status = 'cancelled'
            return False
        monkeypatch.setattr(flow, '_open_oauth', observe)
        # Retained controlled-browser path (background/other-provider support).
        session = LoginSession('agentrouter', 'linuxdo', 'fixture')
        await flow._run_window(session)
        assert 'user_agent' not in options[0]
        assert snapshots['native'] == snapshots['interactive']
        assert session.oauth_user_agent and 'HeadlessChrome/' not in session.oauth_user_agent
        assert db.accounts() == []


@pytest.mark.parametrize('method,url,pending', [
    ('linuxdo', 'https://linux.do/session/sso_provider?sig=fixture&sso=fixture', True),
    ('linuxdo', 'https://connect.linux.do/oauth2/authorize?client_id=fixture&state=fixture', True),
    ('linuxdo', 'https://linux.do/login', True),
    ('github', 'https://github.com/login/oauth/authorize', True),
    ('manual', 'https://linux.do/session/sso_provider', True),
    ('linuxdo', 'https://linux.do.evil.example/session/sso_provider', False),
    ('linuxdo', 'https://linux.do:8443/session/sso_provider', False),
    ('linuxdo', 'https://agentrouter.org/oauth/linuxdo', False),
    ('github', 'https://github.com/example/docs', False),
    ('linuxdo', 'about:blank', False),
])
def test_only_expected_upstream_auth_pages_pause_target_queries(method, url, pending):
    session = LoginSession('agentrouter', method, 'fixture')
    assert LoginFlow._upstream_auth_pending(session, SimpleNamespace(pages=[SimpleNamespace(url=url)])) is pending


@pytest.mark.asyncio
@pytest.mark.parametrize('already_polling', [False, True])
@pytest.mark.parametrize('upstream_url', ['https://linux.do/session/sso_provider?sig=fixture&sso=fixture', 'about:blank'])
async def test_no_target_probe_during_sso_and_automatic_save_after_return(db, monkeypatch, already_polling, upstream_url):
    import backend.login_flow as login
    target = SimpleNamespace(url='https://agentrouter.org/login', goto=AsyncMock(return_value=None))
    upstream = SimpleNamespace(url=upstream_url)
    context = FakeContext([target, upstream], cookies=AsyncMock(return_value=[
        {'name': 'session', 'value': 'anonymous-state-cookie', 'domain': 'agentrouter.org', 'path': '/'}]))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(login, 'launch_async', AsyncMock(return_value=browser))
    monkeypatch.setattr(login, 'chrome_user_agent', AsyncMock(return_value='Mozilla/5.0 Chrome/156.0.0.0 Safari/537.36'))
    monkeypatch.setattr(login, 'setup_notice_guard', AsyncMock())
    monkeypatch.setattr(login, 'VERIFICATION_RECHECK_DELAYS', (.02, .03, .05))
    verify = AsyncMock(return_value={'id': 7, 'quota': 50000000, 'used_quota': 0})
    monkeypatch.setattr(login, 'verify_target_profile', verify)
    flow = LoginFlow(db)
    monkeypatch.setattr(flow, '_open_oauth', AsyncMock(return_value=True))
    session = LoginSession('agentrouter', 'linuxdo', 'fixture')
    flow.sessions[session.id] = session
    async def run_wait():
        evidence = SimpleNamespace(changed=asyncio.Event(), get=AsyncMock(return_value=None))
        captured = await flow._wait_verified_profile(session, context, evidence, time.monotonic() + 8)
        assert captured
        await flow._complete_login(session, *captured, context)
    task = asyncio.create_task(run_wait() if already_polling else flow._run_window(session))
    try:
        async def waiting():
            while session.stage != 'waiting_upstream':
                await asyncio.sleep(.01)
        await asyncio.wait_for(waiting(), 2)
        assert session.verification_auto and not session.verification_paused
        assert not flow.resume_verification(session.id)
        # Also ignore an in-flight "check now" from before the upstream transition.
        session.resume_event.set()
        await asyncio.sleep(.08)
        verify.assert_not_awaited()
        assert db.accounts() == []
        assert 'sig=' not in str(flow.view(session.id))
        upstream.url = 'https://agentrouter.org/console'
        context.cookies.return_value = [
            {'name': 'session', 'value': 'verified-cookie', 'domain': 'agentrouter.org', 'path': '/'}]
        await asyncio.wait_for(task, 4)
        verify.assert_awaited_once()
        assert session.status == 'success'
        assert db.credentials(db.account(session.account))['session'] == 'verified-cookie'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_partitioned_oauth_cookies_survive_a_browser_round_trip():
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required')
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            source = await browser.new_context()
            cookie = {'name': 'fixture-clearance', 'value': 'unpartitioned', 'domain': '.linux.do',
                      'path': '/', 'secure': True, 'httpOnly': True, 'sameSite': 'None'}
            await source.add_cookies([cookie, {**cookie, 'value': 'partitioned',
                                               'partitionKey': 'https://agentrouter.org', '_crHasCrossSiteAncestor': True}])
            cached = LoginFlow._oauth_cookies(await source.cookies(), 'linuxdo')
            restored = await browser.new_context()
            await restored.add_cookies(cached)
            cookies = await restored.cookies()
            assert len(cookies) == 2
            assert {c.get('partitionKey') for c in cookies} == {None, 'https://agentrouter.org'}
            assert {c['value'] for c in cookies} == {'unpartitioned', 'partitioned'}
        finally:
            await browser.close()
