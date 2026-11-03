import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import backend.main as main
from backend.proxy import ProxyConfig, normalize_proxy, browser_proxy
from test_app import account, db, client, FakeContext, forbid_unmocked_unit_test_browsers


CUSTOM = {'mode': 'custom', 'server': 'http://127.0.0.1:18080', 'authenticate': True,
          'username': 'fixture-user', 'password': 'fixture-private-password'}


@pytest.mark.parametrize('server', ['http://localhost:8080', 'https://proxy.example:443', 'socks5://127.0.0.1:1080', 'http://[::1]:8080'])
def test_supported_proxy_addresses(server):
    config = normalize_proxy({'mode': 'custom', 'server': server})
    assert browser_proxy(config) == {'server': server}


@pytest.mark.parametrize('proxy', [
    {'mode': 'custom', 'server': 'file:///tmp/proxy'},
    {'mode': 'custom', 'server': 'http://user:fixture-private-password@localhost:8080'},
    {'mode': 'custom', 'server': 'http://localhost:99999'},
    {'mode': 'custom', 'server': 'http://localhost:8080/path'},
    {'mode': 'custom', 'server': 'http://localhost:8080?token=fixture-private-password'},
    {'mode': 'custom', 'server': 'socks5://localhost:1080', 'authenticate': True},
    {'mode': 'custom', 'server': 123, 'password': 'fixture-private-password'},
])
def test_invalid_proxy_never_echoes_credentials(client, proxy):
    for path, body in [('/api/accounts', account()), ('/api/login/start', {'provider': 'agentrouter', 'method': 'github', 'name': 'fixture'})]:
        response = client.post(path, json={**body, 'proxy': proxy}, headers=client.write_headers)
        assert response.status_code == 422
        assert 'fixture-private-password' not in response.text


def test_proxy_secrets_are_encrypted_and_metadata_edits_preserve_oauth(client):
    response = client.post('/api/accounts', json=account(auth_mode='session', session='fixture-session', api_user='7', proxy=CUSTOM), headers=client.write_headers)
    i = response.json()['id']
    row = main.store.account(i)
    credentials = main.store.credentials(row)
    credentials['oauth_cookies'] = [{'name': 'fixture', 'value': 'fixture-cookie'}]
    main.store.patch(i, encrypted=main.store.encrypt_credentials(credentials), login_via='github')
    assert 'fixture-private-password' not in row['encrypted']
    public = client.get('/api/bootstrap').json()['accounts'][0]['proxy']
    assert public == {'mode': 'custom', 'server': CUSTOM['server'], 'has_auth': True}
    assert 'fixture-user' not in client.get('/api/bootstrap').text
    assert 'fixture-private-password' not in client.get('/api/bootstrap').text
    response = client.put(f'/api/accounts/{i}', json={'name': 'renamed', 'auth_mode': 'session',
        'proxy': {'mode': 'custom', 'server': CUSTOM['server'], 'authenticate': True}}, headers=client.write_headers)
    assert response.status_code == 200
    assert main.store.credentials(main.store.account(i)) == credentials
    # Moving to another proxy must never silently reuse the previous proxy's password.
    response = client.put(f'/api/accounts/{i}', json={'proxy': {'mode': 'custom', 'server': 'http://localhost:18081', 'authenticate': True}}, headers=client.write_headers)
    assert response.status_code == 422
    assert main.store.credentials(main.store.account(i)) == credentials


def test_proxy_change_discards_cached_browser_and_can_remove_auth(client):
    i = main.store.add_account(account(proxy=CUSTOM))
    browser = SimpleNamespace(close=AsyncMock())
    main.login_flow.browser_sessions[(i, 'agentrouter', 'github', 'fixture')] = (browser, None, None)
    response = client.put(f'/api/accounts/{i}', json={'proxy': {'mode': 'direct'}}, headers=client.write_headers)
    assert response.status_code == 200
    browser.close.assert_awaited_once()
    assert main.login_flow.browser_sessions == {}
    assert main.store.credentials(main.store.account(i))['proxy'] == {'mode': 'direct'}


@pytest.mark.asyncio
@pytest.mark.parametrize('background', [True, False])
@pytest.mark.parametrize('config', [CUSTOM, {'mode': 'direct'}])
async def test_authorization_and_checkin_use_account_proxy(db, monkeypatch, background, config):
    import backend.login_flow as login
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='saved', api_user='7', login_via='github',
                               oauth_cookies=[{'name': 'saved', 'value': 'fixture', 'domain': '.github.com', 'path': '/'}], proxy=config))
    page = SimpleNamespace(url='https://agentrouter.org/login', goto=AsyncMock(return_value=None))
    context = FakeContext([page])
    context.add_cookies = AsyncMock()
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    launch = AsyncMock(return_value=browser)
    monkeypatch.setattr(login, 'launch_async', launch)
    monkeypatch.setattr(login, 'get_playwright_proxy', lambda **kw: {'server': 'http://wrong-global:8080'})
    flow = login.LoginFlow(db)
    async def stop(context, page, session, provider):
        session.status = 'cancelled'
        return False
    monkeypatch.setattr(flow, '_open_oauth', stop)
    if background:
        await flow.authenticate_for_checkin(db.account(i), '7')
    else:
        session = flow.start('agentrouter', 'github', 'fixture', i)
        await session.task
    if config['mode'] == 'direct':
        assert 'proxy' not in browser.new_context.call_args.kwargs
        assert launch.call_args.kwargs['args'] == ['--no-proxy-server']
    else:
        assert browser.new_context.call_args.kwargs['proxy'] == browser_proxy(config)
    assert 'proxy' not in launch.call_args.kwargs  # No credentials in process arguments.
    assert launch.call_args.kwargs['headless'] is background
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_authorization_persists_the_proxy_used_by_its_browser(db, monkeypatch):
    from backend.login_flow import LoginFlow
    flow = LoginFlow(db)
    async def complete(session):
        assert session.account_proxy == CUSTOM
        await flow._persist(session, {'id': 7}, 'fixture-session')
    monkeypatch.setattr(flow, '_run', complete)
    session = flow.start('agentrouter', 'github', 'fixture', proxy=ProxyConfig(**CUSTOM))
    await session.task
    assert db.credentials(db.account(session.account))['proxy'] == CUSTOM
    assert 'proxy' not in flow.view(session.id)


@pytest.mark.asyncio
@pytest.mark.parametrize('auth_mode', ['session', 'password'])
async def test_balance_and_password_login_share_the_account_proxy(db, monkeypatch, auth_mode):
    import backend.engine as engine
    i = db.add_account(account(auth_mode=auth_mode, session='fixture', api_user='7', proxy=CUSTOM))
    events = []
    class Session:
        def __init__(self, provider, cookies, **options): events.append(options['proxy'])
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, *args, **kwargs): return {'status': 200, 'body': {'success': True}}
    login = AsyncMock(return_value=SimpleNamespace(cookies={'session': 'fixture'}, api_user='7'))
    monkeypatch.setattr(engine, 'BrowserSession', Session)
    monkeypatch.setattr(engine, 'login_with_credentials', login)
    runner = engine.CheckinEngine()
    monkeypatch.setattr(runner, 'user_info', AsyncMock(return_value={'balance': 100, 'used_quota': 0, 'total': 100}))
    result = await runner.run(db.account(i), db.credentials(db.account(i)))
    assert result['status'] == 'success'
    assert events == [browser_proxy(CUSTOM)]
    if auth_mode == 'password':
        assert login.call_args.kwargs['proxy'] == browser_proxy(CUSTOM)
    else:
        login.assert_not_awaited()
