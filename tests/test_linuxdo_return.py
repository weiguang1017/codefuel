"""Resume a custom site's OAuth after LinuxDO login lands on the community homepage."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright

from backend import login_flow
from backend.engine import LOCAL_CHROME
from backend.providers import CustomProviderIn, PROVIDERS
from backend.store import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = Store(tmp_path / 'accounts.db', tmp_path / 'key')
    provider = CustomProviderIn(name='Fixture', domain='https://fixture.example', template='sub2api').provider('fixture')
    monkeypatch.setitem(PROVIDERS, 'fixture', provider)
    flow = login_flow.LoginFlow(db)
    session = login_flow.LoginSession('fixture', 'linuxdo', 'Fixture account')
    session.status = 'waiting';session.linuxdo_upstream_seen = True
    flow.sessions[session.id] = session
    yield db, provider, flow, session
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://linux.do/login', 'https://linux.do/session/sso_provider?sig=fixture',
                                 'https://linux.do.evil.example/', 'https://linux.do:8443/', 'https://linux.do/t/topic/1'])
async def test_login_challenge_and_unrelated_pages_are_not_redirected(setup, url):
    _, provider, flow, session = setup
    page = SimpleNamespace(url=url, goto=AsyncMock(), evaluate=AsyncMock(return_value=True))
    assert not await flow._recover_linuxdo_home(SimpleNamespace(pages=[page]), session, provider)
    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_home_without_login_requires_explicit_continue(setup, monkeypatch):
    db, provider, flow, session = setup
    monkeypatch.setattr(login_flow, 'LINUXDO_HOME_SETTLE', 0)
    page = SimpleNamespace(url='https://linux.do/', goto=AsyncMock(), evaluate=AsyncMock(return_value=False))
    context = SimpleNamespace(pages=[page])
    assert await flow._recover_linuxdo_home(context, session, provider)
    page.goto.assert_not_awaited()
    assert session.oauth_continue_available and not db.accounts()
    assert flow.continue_oauth(session.id)
    assert not flow.continue_oauth(session.id)
    await flow._recover_linuxdo_home(context, session, provider)
    page.goto.assert_awaited_once()
    assert page.goto.call_args.args[0] == 'https://fixture.example/login'


@pytest.mark.asyncio
async def test_automatic_resume_is_once_and_manual_attempts_are_bounded(setup, monkeypatch):
    _, provider, flow, session = setup
    monkeypatch.setattr(login_flow, 'LINUXDO_HOME_SETTLE', 0)
    page = SimpleNamespace(url='https://linux.do/', goto=AsyncMock(), evaluate=AsyncMock(return_value=True))
    context = SimpleNamespace(pages=[page])
    await flow._recover_linuxdo_home(context, session, provider)
    assert page.goto.await_count == 1
    await flow._recover_linuxdo_home(context, session, provider)
    assert page.goto.await_count == 1 and session.oauth_continue_available
    for _ in range(2):
        assert flow.continue_oauth(session.id)
        await flow._recover_linuxdo_home(context, session, provider)
        await flow._recover_linuxdo_home(context, session, provider)
    assert page.goto.await_count == 3 and not session.oauth_continue_available


@pytest.mark.asyncio
@pytest.mark.parametrize('callback_seen,returned', [(True, False), (False, True)])
async def test_callback_or_target_return_prevents_restarting_auth(setup, callback_seen, returned):
    _, provider, flow, session = setup
    session.linuxdo_callback_seen = callback_seen
    page = SimpleNamespace(url='https://linux.do/', goto=AsyncMock(), evaluate=AsyncMock(return_value=True))
    pages = [page] + ([SimpleNamespace(url='https://fixture.example/dashboard')] if returned else [])
    assert not await flow._recover_linuxdo_home(SimpleNamespace(pages=pages), session, provider)
    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_home_recovery_returns_and_saves_only_verified_target_identity(setup, monkeypatch):
    if not LOCAL_CHROME: pytest.skip('Local Chrome required')
    db, provider, flow, session = setup
    session.linuxdo_upstream_seen = False
    monkeypatch.setattr(login_flow, 'LINUXDO_HOME_SETTLE', .05)
    contexts, states, target_reads = [], [], []
    login_visits = 0
    async with async_playwright() as pw:
        async def launch(**options):
            browser = await pw.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
            async def new_context(**kwargs):
                context = await browser.new_context(**kwargs);contexts.append(context)
                async def route_request(route):
                    nonlocal login_visits
                    url = urlsplit(route.request.url)
                    body, kind, headers = '', 'text/html; charset=utf-8', {}
                    if url.netloc == 'fixture.example' and url.path == '/login':
                        login_visits += 1
                        body = f'''<meta charset="utf-8"><button onclick="location.href='https://connect.linux.do/oauth2/authorize?client_id=fixture&state=fixture-{login_visits}'">使用 Linux.do 登录</button>'''
                    elif url.netloc == 'connect.linux.do':
                        states.append(parse_qs(url.query)['state'][0])
                        if len(states) == 1:
                            body = '<script>location.href="https://linux.do/login"</script>'
                        else:
                            cookies = await context.cookies(['https://linux.do'])
                            assert any(c['name'] == 'fixture-login' and c['value'] == 'ready' for c in cookies)
                            body = '<script>location.href="https://fixture.example/auth/linuxdo/callback?code=fixture&state=fixture-2"</script>'
                    elif url.netloc == 'linux.do' and url.path == '/login':
                        body = '<button id="finish" onclick="location.href=\'/\'">完成测试登录</button>'
                    elif url.netloc == 'linux.do' and url.path == '/':
                        headers['Set-Cookie'] = 'fixture-login=ready; Path=/; Secure; HttpOnly'
                        body = '<button id="current-user">账户</button><h1>Community homepage</h1>'
                    elif url.netloc == 'fixture.example' and url.path == '/auth/linuxdo/callback':
                        body = '''<script>localStorage.setItem('auth_token','fixture-access');localStorage.setItem('refresh_token','fixture-refresh');location.replace('/dashboard')</script>'''
                    elif url.netloc == 'fixture.example' and url.path == '/api/v1/auth/me':
                        target_reads.append(True)
                        assert route.request.headers.get('authorization') == 'Bearer fixture-access'
                        kind = 'application/json'
                        body = '{"code":0,"data":{"id":7,"balance":12.5}}'
                    elif url.netloc == 'fixture.example' and url.path == '/dashboard':
                        body = '<h1>Dashboard</h1>'
                    else:
                        await route.abort();return
                    await route.fulfill(body=body, content_type=kind, headers=headers)
                await context.route('**/*', route_request)
                return context
            return SimpleNamespace(new_context=new_context, close=browser.close)
        monkeypatch.setattr(login_flow, 'launch_async', launch)
        session.task = asyncio.create_task(flow._run_window(session))
        try:
            async def at_login():
                while not contexts or not any(p.url == 'https://linux.do/login' for p in contexts[0].pages):
                    await asyncio.sleep(.05)
            await asyncio.wait_for(at_login(), 10)
            assert not db.accounts() and not target_reads
            page = next(p for p in contexts[0].pages if p.url == 'https://linux.do/login')
            await page.locator('#finish').click()
            await asyncio.wait_for(session.task, 15)
            assert session.status == 'success', session.message
            assert states == ['fixture-1', 'fixture-2']
            assert session.linuxdo_continue_count == 1 and session.linuxdo_callback_seen
            assert target_reads and len(db.accounts()) == 1
            credentials = db.credentials(db.account(session.account))
            assert credentials['session'] == 'fixture-access' and credentials['refresh_token'] == 'fixture-refresh'
            assert db.account(session.account)['balance'] == 12.5
            assert 'fixture-1' not in str(flow.view(session.id)) and 'fixture-access' not in str(flow.view(session.id))
            assert not session.oauth_continue_available
        finally:
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
            await flow.close()
