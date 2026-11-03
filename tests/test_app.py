import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from backend.store import Store, utcnow, iso
from backend.engine import classify_response
import backend.main as main


def test_codefuel_homepage_brand(client):
    response = client.get('/')
    assert response.status_code == 200
    assert '码粮 CodeFuel' in response.text
    assert '为代码添粮，<span>让灵感续航。</span>' in response.text
    assert 'DailyQuota' not in response.text


def test_brand_uses_codefuel_storage_namespace():
    from backend.paths import APP_NAME, DATA_NAMESPACE, DB_NAME, LEGACY_NAMESPACE
    assert APP_NAME == '码粮 CodeFuel'
    assert DATA_NAMESPACE == 'CodeFuel'
    assert DB_NAME == 'codefuel.db'
    assert LEGACY_NAMESPACE == 'DailyQuota'


def test_legacy_data_migrates_into_new_namespace(tmp_path, monkeypatch):
    """改名后首次启动必须能接上旧账号，且不覆盖已有数据。"""
    from backend import paths

    legacy = tmp_path / 'DailyQuota'
    legacy.mkdir()
    (legacy / 'dailyquota.db').write_bytes(b'old-db')
    (legacy / 'secret.key').write_bytes(b'old-key')
    (legacy / 'secret.key').chmod(0o600)

    monkeypatch.setattr(paths, '_platform_data_root', lambda: tmp_path)
    monkeypatch.delenv('CODEFUEL_DATA_DIR', raising=False)
    monkeypatch.delenv('DAILYQUOTA_DATA_DIR', raising=False)

    target = tmp_path / 'CodeFuel'
    moved = paths.migrate_legacy_data(target)
    assert moved == ['dailyquota.db → codefuel.db', 'secret.key → secret.key']
    assert (target / 'codefuel.db').read_bytes() == b'old-db'
    assert (target / 'secret.key').read_bytes() == b'old-key'
    # 旧目录必须保留，迁移可回退。
    assert (legacy / 'dailyquota.db').exists()


def test_migration_never_overwrites_existing_data(tmp_path, monkeypatch):
    from backend import paths

    legacy = tmp_path / 'DailyQuota'
    legacy.mkdir()
    (legacy / 'dailyquota.db').write_bytes(b'old-db')
    target = tmp_path / 'CodeFuel'
    target.mkdir()
    (target / 'codefuel.db').write_bytes(b'new-db')

    monkeypatch.setattr(paths, '_platform_data_root', lambda: tmp_path)
    monkeypatch.delenv('CODEFUEL_DATA_DIR', raising=False)
    monkeypatch.delenv('DAILYQUOTA_DATA_DIR', raising=False)

    assert paths.migrate_legacy_data(target) == []
    assert (target / 'codefuel.db').read_bytes() == b'new-db'


def test_codefuel_data_dir_override(tmp_path, monkeypatch):
    from backend.paths import data_dir
    monkeypatch.setenv('CODEFUEL_DATA_DIR', str(tmp_path / 'new'))
    monkeypatch.setenv('DAILYQUOTA_DATA_DIR', str(tmp_path / 'legacy'))
    assert data_dir() == tmp_path / 'new'
    monkeypatch.delenv('CODEFUEL_DATA_DIR')
    assert data_dir() == tmp_path / 'legacy'


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / 'test.db', tmp_path / 'secret.key')
    yield store
    store.close()


def account(**kw):
    return {'name': '主账号', 'provider': 'anyrouter', 'auth_mode': 'password',
            'email': 'test@example.com', 'password': 'correct-horse', 'enabled': True,
            'interval_hours': 6, **kw}


class FakeContext:
    """Playwright context 的最小替身：支持响应监听器的注册与移除。"""
    def __init__(self, pages=None, cookies=None):
        self.pages = list(pages or [])
        self.listeners = {}
        self.add_cookies = AsyncMockSideEffect('新登录窗口不得导入旧站点 cookie')
        self.cookies = cookies if cookies is not None else AsyncMockDefaultList()

    async def new_page(self):
        page = self.pages[0]
        if page not in self.pages:
            self.pages.append(page)
        return page

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.listeners[event].remove(handler)


class AsyncMockDefaultList:
    """未显式配置 cookies 时返回空列表，避免伪造目标站点会话。"""
    async def __call__(self, *args, **kwargs):
        return []


class AsyncMockSideEffect(AsyncMock):
    """被调用即失败的替身：用于断言某能力绝不被使用。"""
    def __init__(self, reason):
        super().__init__(side_effect=AssertionError(reason))


def test_credentials_encrypted_and_never_public(db):
    account_id = db.add_account(account())
    raw = db.db_path.read_bytes()
    assert b'correct-horse' not in raw
    row = db.account(account_id)
    assert db.credentials(row)['password'] == 'correct-horse'
    assert 'encrypted' not in db.public_account(row)
    assert 'correct-horse' not in json.dumps(db.dashboard(), ensure_ascii=False)


def test_credential_edit_keeps_existing_password(db):
    account_id = db.add_account(account())
    data = account(name='新名称', password=None)
    db.update_account(account_id, data)
    assert db.account(account_id)['name'] == '新名称'
    assert db.credentials(db.account(account_id))['password'] == 'correct-horse'


def test_create_does_not_execute_immediately(db):
    account_id = db.add_account(account())
    assert db.account(account_id)['status'] == 'idle'
    assert db.account(account_id)['next_run'] > iso()
    assert db.runs() == []


def test_disabled_account_still_allows_manual_semantics(db):
    account_id = db.add_account(account(enabled=False))
    assert db.accounts(enabled_only=True) == []
    assert db.account(account_id)['enabled'] == 0


def test_scheduled_next_run_is_persistent(db):
    account_id = db.add_account(account())
    db.patch(account_id, next_run=iso(utcnow() - timedelta(seconds=1)))
    assert db.account(account_id)['next_run'] < iso()


def test_deleted_account_preserves_log_snapshot(db):
    account_id = db.add_account(account())
    row = db.account(account_id)
    run_id = db.begin_run(row, 'manual')
    db.finish_run(run_id, account_id, {'status': 'failed', 'message': 'test'}, 'manual')
    assert db.delete_account(account_id)
    assert db.runs()[0]['account_name'] == '主账号'
    assert db.accounts() == []


def test_response_does_not_accept_html_success():
    assert classify_response(200, '<html>success</html>')[0] == 'failed'
    assert classify_response(200, {'success': False, 'message': 'successful login only'})[0] == 'failed'
    assert classify_response(200, {'ret': 1})[0] == 'success'
    assert classify_response(200, {'code': 0})[0] == 'success'
    assert classify_response(200, {'success': False, 'msg': '今日已签到'})[0] == 'already_checked'
    assert classify_response(401, {'success': True})[0] == 'manual_required'


def test_first_agentrouter_sync_is_not_a_reward():
    # A first snapshot has no baseline, so its total must never be reported as a reward.
    previous_snapshot = False
    reward = None if not previous_snapshot else 25
    assert reward is None


@pytest.mark.asyncio
async def test_agentrouter_password_query_without_checkin_evidence_only_syncs(db, monkeypatch):
    import backend.engine as adapter
    from types import SimpleNamespace
    account_id = db.add_account(account(provider='agentrouter'))
    db.patch(account_id, has_snapshot=1, total_quota=100)
    async def fake_login(*args):
        return SimpleNamespace(cookies={'session': 'x'}, api_user='123')
    requests = []
    class FakeSession:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, path, user, method='GET', payload=None):
            assert method == 'GET', 'AgentRouter must not call any sign-in endpoint'
            requests.append((path, user))
            return {'status': 200, 'body': {'success': True, 'data': {'quota': 62500000, 'used_quota': 0}}}
    monkeypatch.setattr(adapter, 'login_with_credentials', fake_login)
    monkeypatch.setattr(adapter, 'BrowserSession', FakeSession)
    result = await adapter.CheckinEngine().run(db.account(account_id), {'email': 'a', 'password': 'b'}, True)
    assert len(requests) == 1
    assert result['status'] == 'synced'
    assert result['reward'] is None
    assert '尚无本次签到凭据' in result['message']


@pytest.mark.asyncio
async def test_browser_session_primes_site_before_account_session(monkeypatch):
    import backend.engine as adapter
    from backend.providers import PROVIDERS

    events = []

    class FakePage:
        url = 'https://anyrouter.top'

        async def goto(self, url, **kwargs):
            events.append(('goto', url, kwargs.get('timeout')))

        async def wait_for_load_state(self, *args, **kwargs):
            return None

    class FakeContext:
        async def new_page(self):
            events.append(('new_page',))
            return FakePage()

        async def add_cookies(self, cookies):
            events.append(('cookies', [cookie['name'] for cookie in cookies]))

    class FakeBrowser:
        async def new_context(self):
            events.append(('new_context',))
            return FakeContext()

        async def close(self):
            events.append(('close',))

    async def fake_launch(**kwargs):
        events.append(('launch', kwargs))
        return FakeBrowser()

    async def fake_prepare(page):
        events.append(('prepare',))

    async def forbidden_waf(page):
        raise AssertionError('签到流程不应等待站点 UI 渲染')

    monkeypatch.setattr(adapter, 'launch_async', fake_launch)
    monkeypatch.setattr(adapter, 'prepare_browser_page', fake_prepare)
    monkeypatch.setattr(adapter, 'wait_for_waf_ready', forbidden_waf, raising=False)

    async with adapter.BrowserSession(PROVIDERS['anyrouter'], {'session': 'account-session'}) as session:
        assert session.page is not None

    assert [event[0] for event in events] == [
        'launch', 'new_context', 'new_page', 'prepare', 'goto', 'cookies', 'goto', 'close',
    ]
    assert events[0][1] == {'headless': True}
    assert events[4][1] == 'https://anyrouter.top'
    assert events[4][2] == 60_000
    assert events[5] == ('cookies', ['session'])
    assert events[6][1] == 'https://anyrouter.top/login'
    assert events[6][2] == 60_000


@pytest.mark.asyncio
async def test_agentrouter_background_session_uses_proxy_and_skips_login_spa(monkeypatch):
    import backend.engine as adapter
    from backend.providers import PROVIDERS

    events = []

    class FakePage:
        url = 'https://agentrouter.org'

        async def goto(self, url, **kwargs):
            self.url = url
            events.append(('goto', url))

        async def wait_for_load_state(self, *args, **kwargs):
            return None

    class FakeContext:
        async def new_page(self):
            events.append(('new_page',))
            return FakePage()

        async def cookies(self, urls=None):
            events.append(('read_cookies', urls))
            return [{'name': 'acw_tc', 'value': 'waf-cookie', 'domain': '.agentrouter.org'}]

        async def add_cookies(self, cookies):
            events.append(('cookies', [cookie['name'] for cookie in cookies]))

    class FakeBrowser:
        async def new_context(self):
            events.append(('new_context',))
            return FakeContext()

        async def close(self):
            events.append(('close',))

    launch_calls = []

    async def fake_launch(**kwargs):
        launch_calls.append(kwargs)
        return FakeBrowser()

    monkeypatch.setattr(adapter, 'get_playwright_proxy', lambda *, use_proxy: (
        {'server': 'http://127.0.0.1:7890'} if use_proxy else None))
    monkeypatch.setattr(adapter, 'launch_async', fake_launch)
    monkeypatch.setattr(adapter, 'prepare_browser_page', AsyncMock())
    async def wait_for_waf(page):
        events.append(('wait_for_waf', page.url))
    monkeypatch.setattr(adapter, 'wait_for_waf_ready', wait_for_waf)

    async with adapter.BrowserSession(PROVIDERS['agentrouter'], {'session': 'saved-session'}):
        pass

    assert launch_calls == [{'headless': True, 'proxy': {'server': 'http://127.0.0.1:7890'}}]
    assert events == [
        ('new_context',), ('new_page',), ('goto', 'https://agentrouter.org/login'),
        ('wait_for_waf', 'https://agentrouter.org/login'),
        ('read_cookies', ['https://agentrouter.org']),
        ('cookies', ['session']), ('close',),
    ]


@pytest.mark.asyncio
async def test_agentrouter_waf_cookie_missing_closes_browser_before_session_injection(monkeypatch):
    import backend.engine as adapter
    from backend.providers import PROVIDERS

    added = []
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=SimpleNamespace(
            new_page=AsyncMock(return_value=SimpleNamespace(
                url='https://agentrouter.org/login',
                goto=AsyncMock(), wait_for_load_state=AsyncMock())),
            cookies=AsyncMock(return_value=[]),
            add_cookies=AsyncMock(side_effect=lambda cookies: added.extend(cookies)))),
        close=AsyncMock())
    monkeypatch.setattr(adapter, 'launch_async', AsyncMock(return_value=browser))
    monkeypatch.setattr(adapter, 'get_playwright_proxy', lambda *, use_proxy: None)
    monkeypatch.setattr(adapter, 'prepare_browser_page', AsyncMock())
    monkeypatch.setattr(adapter, 'wait_for_waf_ready', AsyncMock())

    with pytest.raises(adapter.WAFCookiesUnavailable):
        async with adapter.BrowserSession(PROVIDERS['agentrouter'], {'session': 'saved-session'}):
            pass

    assert not added
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_spa_self_redirect_abort_is_not_a_checkin_failure(monkeypatch):
    """AnyRouter 会在根页自行跳转；我们的下一次导航被它打断时不能算失败。"""
    from playwright.async_api import Error as PlaywrightError
    import backend.engine as adapter
    from backend.providers import PROVIDERS

    class FakePage:
        def __init__(self):
            self.url = 'https://anyrouter.top'
            self.calls = 0

        async def goto(self, url, **kwargs):
            self.calls += 1
            if self.calls == 2:
                self.url = 'https://anyrouter.top/login'
                raise PlaywrightError('Page.goto: net::ERR_ABORTED at ' + url)

        async def wait_for_load_state(self, *args, **kwargs):
            return None

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def add_cookies(self, cookies):
            pass

    class FakeBrowser:
        async def new_context(self):
            return FakeContext()

        async def close(self):
            pass

    async def fake_launch(**kwargs):
        return FakeBrowser()

    async def fake_prepare(page):
        pass

    monkeypatch.setattr(adapter, 'launch_async', fake_launch)
    monkeypatch.setattr(adapter, 'prepare_browser_page', fake_prepare)

    session = adapter.BrowserSession(PROVIDERS['anyrouter'], {'session': 'x'})
    async with session:
        assert session.page.url.startswith('https://anyrouter.top')


@pytest.mark.asyncio
async def test_offsite_abort_still_fails(monkeypatch):
    from playwright.async_api import Error as PlaywrightError
    import backend.engine as adapter
    from backend.providers import PROVIDERS

    class FakePage:
        url = 'about:blank'

        async def goto(self, url, **kwargs):
            raise PlaywrightError('Page.goto: net::ERR_ABORTED at ' + url)

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def add_cookies(self, cookies):
            pass

    class FakeBrowser:
        async def new_context(self):
            return FakeContext()

        async def close(self):
            pass

    async def fake_launch(**kwargs):
        return FakeBrowser()

    async def fake_prepare(page):
        pass

    monkeypatch.setattr(adapter, 'launch_async', fake_launch)
    monkeypatch.setattr(adapter, 'prepare_browser_page', fake_prepare)

    with pytest.raises(PlaywrightError, match='ERR_ABORTED'):
        async with adapter.BrowserSession(PROVIDERS['anyrouter'], {'session': 'x'}):
            pass


@pytest.mark.asyncio
async def test_browser_startup_error_is_not_misreported_as_manual_required(db, monkeypatch):
    import backend.engine as adapter
    from types import SimpleNamespace

    account_id = db.add_account(account())

    async def fake_login(*args):
        return SimpleNamespace(cookies={'session': 'x'}, api_user='123')

    class BrokenSession:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            raise RuntimeError('browser startup failed')

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(adapter, 'login_with_credentials', fake_login)
    monkeypatch.setattr(adapter, 'BrowserSession', BrokenSession)
    result = await adapter.CheckinEngine().run(db.account(account_id), {'email': 'a', 'password': 'b'})

    assert result['status'] == 'failed'
    assert 'RuntimeError' in result['message']
    assert '未确认签到结果' in result['message']


@pytest.mark.asyncio
async def test_destroyed_context_is_retried_once(monkeypatch):
    """SPA 自身跳转会销毁执行上下文；重试一次即可，不把偶发跳转算成签到失败。"""
    from playwright.async_api import Error as PlaywrightError
    import backend.engine as adapter
    from backend.providers import PROVIDERS

    monkeypatch.setattr(adapter, 'SETTLE_STABLE', 0.0)
    monkeypatch.setattr(adapter, 'SETTLE_TIMEOUT', 1.0)

    class FakePage:
        url = 'https://anyrouter.top/login'

        def __init__(self):
            self.calls = 0

        async def evaluate(self, script, args):
            self.calls += 1
            if self.calls == 1:
                raise PlaywrightError('Page.evaluate: Execution context was destroyed, most likely because of a navigation')
            return {'status': 401, 'body': {'success': False}}

        async def wait_for_load_state(self, *args, **kwargs):
            return None

    session = adapter.BrowserSession(PROVIDERS['anyrouter'], {'session': 'x'})
    session.page = FakePage()
    result = await session.request('/api/user/self', '1')

    assert result == {'status': 401, 'body': {'success': False}}
    assert session.page.calls == 2


@pytest.mark.asyncio
async def test_unrelated_browser_error_is_not_retried(monkeypatch):
    from playwright.async_api import Error as PlaywrightError
    import backend.engine as adapter
    from backend.providers import PROVIDERS

    monkeypatch.setattr(adapter, 'SETTLE_STABLE', 0.0)
    monkeypatch.setattr(adapter, 'SETTLE_TIMEOUT', 1.0)

    class FakePage:
        url = 'https://anyrouter.top/login'

        def __init__(self):
            self.calls = 0

        async def evaluate(self, script, args):
            self.calls += 1
            raise PlaywrightError('Page.evaluate: Target closed')

        async def wait_for_load_state(self, *args, **kwargs):
            return None

    session = adapter.BrowserSession(PROVIDERS['anyrouter'], {'session': 'x'})
    session.page = FakePage()

    with pytest.raises(PlaywrightError, match='Target closed'):
        await session.request('/api/user/self', '1')
    assert session.page.calls == 1


def test_interrupted_task_recovers_honestly(db):
    account_id = db.add_account(account())
    run_id = db.begin_run(db.account(account_id), 'scheduled')
    assert db.recover_interrupted() == 1
    assert db.runs()[0]['status'] == 'interrupted'
    assert db.account(account_id)['status'] == 'interrupted'
    assert '未完成' in db.runs()[0]['message']


@pytest.fixture
def client(tmp_path, monkeypatch):
    local = Store(tmp_path / 'api.db', tmp_path / 'api.key')
    monkeypatch.setattr(main, 'store', local)
    monkeypatch.setattr(main, 'login_flow', __import__('backend.login_flow', fromlist=['LoginFlow']).LoginFlow(local))
    main.queued.clear()
    main.active.clear()
    async def fake_run(row, credentials, previous):
        return {'status': 'synced', 'message': 'mocked only', 'before_balance': 10,
                'after_balance': 10, 'reward': 0, 'duration_ms': 1}
    monkeypatch.setattr(main.engine, 'run', fake_run)
    with TestClient(main.app, base_url='http://127.0.0.1:8765') as c:
        c.csrf = c.get('/api/bootstrap').json()['csrf_token']
        c.write_headers = {'X-CSRF-Token': c.csrf, 'Content-Type': 'application/json'}
        yield c
    local.close()


def test_api_csrf_and_public_account(client):
    assert client.post('/api/accounts', json=account()).status_code == 403
    response = client.post('/api/accounts', json=account(), headers=client.write_headers)
    assert response.status_code == 201
    payload = client.get('/api/bootstrap').json()
    assert payload['accounts'][0]['name'] == '主账号'
    assert 'correct-horse' not in json.dumps(payload)
    assert payload['stats']['total_accounts'] == 1


def test_api_queue_deduplicates(client):
    account_id = client.post('/api/accounts', json=account(), headers=client.write_headers).json()['id']
    main.queued.add(account_id)
    assert client.post(f'/api/accounts/{account_id}/run', json={}, headers=client.write_headers).json()['queued'] is False
    main.queued.clear()
    assert client.post(f'/api/accounts/{account_id}/run', json={}, headers=client.write_headers).json()['queued'] is True


def test_api_settings_and_delete(client):
    account_id = client.post('/api/accounts', json=account(), headers=client.write_headers).json()['id']
    response = client.patch('/api/settings', json={'scheduler_enabled': False, 'interval_hours': 12}, headers=client.write_headers)
    assert response.status_code == 200
    data = client.get('/api/bootstrap').json()
    assert data['settings']['scheduler_enabled'] is False
    assert data['accounts'][0]['interval_hours'] == 12
    assert client.delete(f'/api/accounts/{account_id}', headers=client.write_headers).status_code == 200


def test_csv_formula_protection(client):
    client.post('/api/accounts', json=account(name='=HYPERLINK("bad")'), headers=client.write_headers)
    main.store.patch(1, status='idle')
    run_id = main.store.begin_run(main.store.account(1), 'manual')
    main.store.finish_run(run_id, 1, {'status': 'failed', 'message': 'mock'}, 'manual')
    text = client.get('/api/logs/export').text
    assert "'=HYPERLINK" in text


def test_login_rejects_unknown_method(client):
    response = client.post('/api/login/start', json={'provider': 'anyrouter', 'method': 'facebook', 'name': 'x'},
                           headers=client.write_headers)
    assert response.status_code in (400, 422)


def test_login_rejects_unknown_provider(client):
    response = client.post('/api/login/start', json={'provider': 'evil.example', 'method': 'github', 'name': 'x'},
                           headers=client.write_headers)
    assert response.status_code in (400, 422)


def test_login_requires_name_before_opening_window(client):
    response = client.post('/api/login/start', json={'provider': 'agentrouter', 'method': 'github'},
                           headers=client.write_headers)
    assert response.status_code == 400
    assert '备注' in response.json()['detail']


def test_login_flow_single_window_and_cancel(client, monkeypatch):
    async def stub_run(self, session):
        session.status = 'waiting'
        session.message = 'mocked window'

    monkeypatch.setattr(main.login_flow.__class__, '_run', stub_run)
    first = client.post('/api/login/start', json={'provider': 'agentrouter', 'method': 'github', 'name': 'gra'},
                        headers=client.write_headers)
    assert first.status_code == 202
    session_id = first.json()['id']
    assert client.get(f'/api/login/{session_id}').json()['status'] in ('starting', 'waiting')
    second = client.post('/api/login/start', json={'provider': 'anyrouter', 'method': 'linuxdo', 'name': 'ar'},
                         headers=client.write_headers)
    assert second.status_code == 409
    assert client.post(f'/api/login/{session_id}/cancel', json={}, headers=client.write_headers).json()['ok'] is True
    assert client.get(f'/api/login/{session_id}').json()['status'] == 'cancelled'
    assert client.get('/api/login/unknown-session').status_code == 404


@pytest.mark.parametrize('initial,expected', [('waiting', 'cancelled'), ('success', 'success')])
def test_cancel_response_reports_final_status(client, initial, expected):
    from backend.login_flow import LoginSession
    session = LoginSession('agentrouter', 'github', '取消结果测试')
    session.status = initial
    if initial == 'success':
        session.account = 42
    main.login_flow.sessions[session.id] = session
    response = client.post(f'/api/login/{session.id}/cancel', json={}, headers=client.write_headers)
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['status'] == expected
    assert body['provider'] == 'agentrouter'
    assert body['account_id'] == (42 if initial == 'success' else None)
    assert 'session' not in body and 'encrypted' not in body


@pytest.mark.asyncio
async def test_oauth_persist_creates_encrypted_session_account(db):
    from backend.login_flow import LoginFlow, LoginSession
    flow = LoginFlow(db)
    session = LoginSession('agentrouter', 'github', 'GitHub 主账号')
    await flow._persist(session, {'id': 42, 'username': 'neo', 'quota': 500000, 'used_quota': 0}, 'sess-value-123')
    assert session.status == 'success'
    row = db.account(session.account)
    assert row['login_via'] == 'github'
    assert row['auth_mode'] == 'session'
    assert db.credentials(row) == {'session': 'sess-value-123', 'api_user': '42'}
    assert row['balance'] == 1.0
    assert b'sess-value-123' not in db.db_path.read_bytes()
    assert 'sess-value-123' not in json.dumps(db.dashboard(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_oauth_relogin_refreshes_existing_account(db):
    from backend.login_flow import LoginFlow, LoginSession
    account_id = db.add_account(account(auth_mode='session', session='old-session', api_user='7', login_via='github'))
    flow = LoginFlow(db)
    session = LoginSession('anyrouter', 'linuxdo', '主账号', account_id=account_id)
    await flow._persist(session, {'id': 7, 'quota': 250000, 'used_quota': 0}, 'new-session')
    row = db.account(account_id)
    assert row['login_via'] == 'linuxdo'
    assert db.credentials(row)['session'] == 'new-session'
    assert db.accounts()[0]['name'] == '主账号'


@pytest.mark.asyncio
async def test_oauth_persist_without_cookie_is_not_success(db):
    from backend.login_flow import LoginFlow, LoginSession
    flow = LoginFlow(db)
    session = LoginSession('anyrouter', 'manual', '手动账号')
    await flow._persist(session, {'id': 1}, None)
    assert session.status == 'error'
    assert db.accounts() == []


def test_old_database_gains_login_via_column(tmp_path):
    import sqlite3
    from cryptography.fernet import Fernet
    db_path = tmp_path / 'legacy.db'
    key_path = tmp_path / 'legacy.key'
    key_path.write_bytes(Fernet.generate_key())
    key_path.chmod(0o600)
    legacy = sqlite3.connect(db_path)
    legacy.executescript("""CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        provider TEXT NOT NULL, auth_mode TEXT NOT NULL, encrypted TEXT NOT NULL, enabled INTEGER DEFAULT 1,
        status TEXT DEFAULT 'idle', last_run TEXT, next_run TEXT, last_message TEXT, balance REAL,
        used_quota REAL, total_quota REAL, has_snapshot INTEGER DEFAULT 0, interval_hours INTEGER DEFAULT 6,
        created_at TEXT NOT NULL);
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, account_name TEXT NOT NULL,
        provider TEXT NOT NULL, status TEXT NOT NULL, trigger_type TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, before_balance REAL, after_balance REAL, reward REAL, message TEXT, duration_ms INTEGER);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);""")
    encrypted = Fernet(key_path.read_bytes()).encrypt(json.dumps({'session': 'legacy-session', 'api_user': '7'}).encode()).decode()
    legacy.execute('INSERT INTO accounts (name,provider,auth_mode,encrypted,created_at) VALUES(?,?,?,?,?)',
                   ('旧账号', 'anyrouter', 'session', encrypted, iso()))
    legacy.commit()
    legacy.close()
    migrated = Store(db_path, key_path)
    try:
        columns = {row['name'] for row in migrated.db.execute('PRAGMA table_info(accounts)')}
        assert 'login_via' in columns
        assert migrated.dashboard()['accounts'][0]['name'] == '旧账号'
        assert migrated.dashboard()['accounts'][0]['login_via'] is None
        assert migrated.credentials(migrated.account(1)) == {'session': 'legacy-session', 'api_user': '7'}
    finally:
        migrated.close()


def test_rename_keeps_oauth_origin(db):
    account_id = db.add_account(account(auth_mode='session', session='s', api_user='9', login_via='github'))
    db.update_account(account_id, {**account(name='改名', auth_mode='session', session='s', api_user='9')})
    assert db.account(account_id)['login_via'] == 'github'


def test_switching_to_password_clears_oauth_origin(db):
    account_id = db.add_account(account(auth_mode='session', session='s', api_user='9', login_via='linuxdo'))
    db.update_account(account_id, account(auth_mode='password'))
    assert db.account(account_id)['login_via'] is None


def test_bootstrap_provider_registry(client):
    from backend import engine, providers
    assert engine.PROVIDERS is providers.PROVIDERS
    catalog = client.get('/api/bootstrap').json()['providers']
    assert {p['id'] for p in catalog} == set(providers.PROVIDERS)
    for entry in catalog:
        assert entry['name']
        assert entry['login_methods'] == ['github', 'linuxdo', 'manual', 'password', 'session']
        assert entry['oauth_availability'] == 'detect_on_login'


@pytest.mark.parametrize('provider', ['anyrouter', 'agentrouter'])
@pytest.mark.parametrize('method', ['github', 'linuxdo', 'manual', 'password', 'session'])
def test_all_registered_provider_login_methods(client, monkeypatch, provider, method):
    async def stub_run(self, session):
        session.status = 'waiting'
    monkeypatch.setattr(main.login_flow.__class__, '_run', stub_run)
    if method in ('password', 'session'):
        response = client.post('/api/accounts', headers=client.write_headers,
                               json=account(provider=provider, auth_mode=method, session='test-session', api_user='7'))
        assert response.status_code == 201
    else:
        response = client.post('/api/login/start', headers=client.write_headers,
                               json={'provider': provider, 'method': method, 'name': '测试身份'})
        assert response.status_code == 202
        view = client.get('/api/login/' + response.json()['id']).json()
        assert (view['provider'], view['method']) == (provider, method)


def test_validators_follow_registry_not_fixed_names(monkeypatch):
    from backend.providers import PROVIDERS, Provider
    monkeypatch.setitem(PROVIDERS, 'isolated-test', Provider(name='isolated-test', domain='https://adapter.test'))
    assert main.AccountIn(**account(provider='isolated-test')).provider == 'isolated-test'
    assert main.LoginIn(provider='isolated-test', method='github').provider == 'isolated-test'
    monkeypatch.setattr(PROVIDERS['isolated-test'], 'login_methods', ('session',))
    with pytest.raises(ValueError):
        main.LoginIn(provider='isolated-test', method='github')
    with pytest.raises(ValueError):
        main.AccountIn(**account(provider='isolated-test'))


@pytest.mark.parametrize('provider', ['anyrouter', 'agentrouter'])
@pytest.mark.parametrize('method', ['github', 'linuxdo', 'manual'])
@pytest.mark.asyncio
async def test_browser_source_labels_and_independent_accounts(db, provider, method):
    from backend.login_flow import LoginFlow, LoginSession
    flow = LoginFlow(db)
    first = LoginSession(provider, method, '相同备注')
    second = LoginSession(provider, method, '相同备注')
    await flow._persist(first, {'id': 12}, 'first-isolated-cookie')
    await flow._persist(second, {'id': 13}, 'second-isolated-cookie')
    assert first.status == second.status == 'success'
    assert first.account != second.account
    assert db.account(first.account)['login_via'] == method
    assert db.account(second.account)['provider'] == provider
    assert db.credentials(db.account(first.account)) == {'session': 'first-isolated-cookie', 'api_user': '12'}
    assert db.credentials(db.account(second.account)) == {'session': 'second-isolated-cookie', 'api_user': '13'}


def test_edit_provider_returns_conflict_without_modifying_credentials(client):
    account_id = client.post('/api/accounts', json=account(), headers=client.write_headers).json()['id']
    before = dict(main.store.account(account_id))
    response = client.put(f'/api/accounts/{account_id}', json={'provider': 'agentrouter'}, headers=client.write_headers)
    assert response.status_code == 409
    assert dict(main.store.account(account_id)) == before


def test_store_edit_cannot_change_provider(db):
    account_id = db.add_account(account())
    with pytest.raises(ValueError, match='平台'):
        db.update_account(account_id, account(provider='agentrouter'))
    assert db.account(account_id)['provider'] == 'anyrouter'


@pytest.mark.parametrize('reason,expected', [('missing', 404), ('provider', 409), ('queued', 409), ('running', 409)])
def test_relogin_rejects_missing_mismatched_or_busy_account(client, reason, expected):
    account_id = main.store.add_account(account())
    if reason == 'missing':
        main.store.delete_account(account_id)
    if reason in ('queued', 'running'):
        main.store.patch(account_id, status=reason)
    provider = 'agentrouter' if reason == 'provider' else 'anyrouter'
    response = client.post('/api/login/start', headers=client.write_headers,
                           json={'provider': provider, 'method': 'github', 'account_id': account_id})
    assert response.status_code == expected
    assert not main.login_flow.sessions


@pytest.mark.parametrize('busy_set', ['queued', 'active'])
def test_relogin_checks_in_memory_jobs(client, busy_set):
    account_id = main.store.add_account(account())
    getattr(main, busy_set).add(account_id)
    response = client.post('/api/login/start', headers=client.write_headers,
                           json={'provider': 'anyrouter', 'method': 'github', 'account_id': account_id})
    assert response.status_code == 409


def test_authorizing_blocks_edit_delete_run_and_second_login(client, monkeypatch):
    async def stub_run(self, session):
        session.status = 'waiting'
    monkeypatch.setattr(main.login_flow.__class__, '_run', stub_run)
    account_id = main.store.add_account(account())
    payload = {'provider': 'anyrouter', 'method': 'github', 'account_id': account_id}
    started = client.post('/api/login/start', headers=client.write_headers, json=payload)
    assert started.status_code == 202
    assert client.put(f'/api/accounts/{account_id}', headers=client.write_headers, json={'name': '变更'}).status_code == 409
    assert client.delete(f'/api/accounts/{account_id}', headers=client.write_headers).status_code == 409
    assert client.post(f'/api/accounts/{account_id}/run', headers=client.write_headers, json={}).status_code == 409
    assert client.post('/api/run-all', headers=client.write_headers, json={}).json()['queued'] == 0
    assert client.post('/api/login/start', headers=client.write_headers, json=payload).status_code == 409
    client.post('/api/login/' + started.json()['id'] + '/cancel', headers=client.write_headers, json={})
    assert client.put(f'/api/accounts/{account_id}', headers=client.write_headers, json={'name': '变更'}).status_code == 200


@pytest.mark.parametrize('reason', ['missing', 'provider', 'queued', 'running', 'identity'])
@pytest.mark.asyncio
async def test_relogin_rechecks_before_persist(db, reason):
    from backend.login_flow import LoginFlow, LoginSession
    account_id = db.add_account(account(auth_mode='session', session='old', api_user='7'))
    flow = LoginFlow(db)
    session = LoginSession('anyrouter', 'github', '测试', account_id=account_id)
    if reason == 'missing':
        db.delete_account(account_id)
    elif reason == 'provider':
        db.patch(account_id, provider='agentrouter')
    elif reason in ('queued', 'running'):
        db.patch(account_id, status=reason)
    await flow._persist(session, {'id': 8 if reason == 'identity' else 7}, 'new')
    assert session.status == 'failed'
    row = db.account(account_id)
    if row:
        assert db.credentials(row)['session'] == 'old'
    else:
        assert db.accounts() == []


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('terminal', ['cancelled', 'failed', 'unavailable', 'expired'])
@pytest.mark.asyncio
async def test_cancelled_or_failed_login_never_saves(db, existing, terminal):
    from backend.login_flow import LoginFlow, LoginSession
    account_id = db.add_account(account(auth_mode='session', session='old', api_user='7')) if existing else None
    flow = LoginFlow(db)
    session = LoginSession('anyrouter', 'github', '测试', account_id=account_id)
    session.status = terminal
    session.cancelled = terminal == 'cancelled'
    await flow._persist(session, {'id': 7}, 'new')
    assert session.status == terminal
    if existing:
        assert db.credentials(db.account(account_id))['session'] == 'old'
    else:
        assert db.accounts() == []


@pytest.mark.parametrize('provider', ['anyrouter', 'agentrouter'])
@pytest.mark.parametrize('method', ['github', 'linuxdo'])
@pytest.mark.asyncio
async def test_missing_oauth_entry_is_unavailable_without_fallback(db, monkeypatch, provider, method):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import backend.login_flow as login
    page = SimpleNamespace(url=login.PROVIDERS[provider].domain + '/login', goto=AsyncMock())
    context = FakeContext([page])
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(login, 'launch_async', AsyncMock(return_value=browser))
    monkeypatch.setattr(login, 'prepare_browser_page', AsyncMock())
    monkeypatch.setattr(login, 'wait_for_waf_ready', AsyncMock())
    flow = login.LoginFlow(db)
    monkeypatch.setattr(flow, '_click_oauth', AsyncMock(return_value=False))
    monkeypatch.setattr(flow, '_navigate_login_link', AsyncMock(return_value=False))
    verify = AsyncMock()
    monkeypatch.setattr(login, 'verify_target_profile', verify)
    session = login.LoginSession(provider, method, '测试')
    await flow._run(session)
    assert session.status == 'unavailable'
    assert login.OAUTH_LABELS[method] in session.message
    assert '未提供' in session.message
    assert not verify.called
    assert db.accounts() == []
    browser.close.assert_awaited_once()


@pytest.mark.parametrize('domain,accepted', [
    ('anyrouter.top', True), ('.anyrouter.top', True), ('router.top', False),
    ('.router.top', False), ('agentrouter.org', False), ('github.com', False),
    ('anyrouter.top.other.test', False), ('', False),
])
@pytest.mark.asyncio
async def test_target_cookie_domain_isolation(db, domain, accepted):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import LoginFlow
    from backend.providers import PROVIDERS
    cookies = AsyncMock(return_value=[{'name': 'session', 'domain': domain, 'value': 'test-cookie', 'path': '/'}])
    result = await LoginFlow(db)._session_cookie(SimpleNamespace(cookies=cookies), PROVIDERS['anyrouter'])
    assert result == ('test-cookie' if accepted else None)
    cookies.assert_awaited_once_with(['https://anyrouter.top/api/user/self'])


@pytest.mark.asyncio
async def test_cookie_filter_does_not_reuse_other_platform_session(db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import LoginFlow
    from backend.providers import PROVIDERS
    cookies = AsyncMock(return_value=[
        {'name': 'session', 'domain': '.github.com', 'value': 'upstream'},
        {'name': 'session', 'domain': '.agentrouter.org', 'value': 'agent'},
        {'name': 'session', 'domain': '.anyrouter.top', 'value': 'any'},
    ])
    flow = LoginFlow(db)
    context = SimpleNamespace(cookies=cookies)
    assert await flow._session_cookie(context, PROVIDERS['anyrouter']) == 'any'
    assert await flow._session_cookie(context, PROVIDERS['agentrouter']) == 'agent'


@pytest.mark.parametrize('page_url,response_url,accepted', [
    ('https://anyrouter.top/console', 'https://anyrouter.top/api/user/self?fresh=1', True),
    ('https://github.com/console', 'https://anyrouter.top/api/user/self', False),
    ('https://anyrouter.top/console', 'https://agentrouter.org/api/user/self', False),
    ('https://anyrouter.top/console', 'https://anyrouter.top/api/user/self/old', False),
    ('https://anyrouter.top/console', 'http://anyrouter.top/api/user/self', False),
])
@pytest.mark.asyncio
async def test_profile_verification_requires_target_origin_and_path(page_url, response_url, accepted):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import verify_target_profile
    from backend.providers import PROVIDERS
    evaluate = AsyncMock(return_value={'status': 200, 'url': response_url, 'body': {'success': True, 'data': {'id': 7}}})
    profile = await verify_target_profile(SimpleNamespace(url=page_url, evaluate=evaluate), PROVIDERS['anyrouter'])
    assert bool(profile) is accepted
    if 'github.com' in page_url:
        evaluate.assert_not_called()
    else:
        script, args = evaluate.call_args.args
        assert "cache: 'no-store'" in script
        assert "redirect: 'error'" in script
        assert "credentials: 'same-origin'" in script
        assert args['url'] == 'https://anyrouter.top/api/user/self'
        assert args['nonce']


@pytest.mark.asyncio
async def test_profile_does_not_reuse_previous_success():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import verify_target_profile
    from backend.providers import PROVIDERS
    page = SimpleNamespace(url='https://anyrouter.top/console', evaluate=AsyncMock(side_effect=[
        {'status': 200, 'url': 'https://anyrouter.top/api/user/self', 'body': {'success': True, 'data': {'id': 7}}},
        {'status': 401, 'url': 'https://anyrouter.top/api/user/self', 'body': {'success': False}},
    ]))
    assert await verify_target_profile(page, PROVIDERS['anyrouter']) == {'id': 7}
    assert await verify_target_profile(page, PROVIDERS['anyrouter']) is None


def test_editing_api_user_clears_oauth_source(client):
    account_id = main.store.add_account(account(auth_mode='session', session='old', api_user='7', login_via='github'))
    response = client.put(f'/api/accounts/{account_id}', json={'api_user': '8'}, headers=client.write_headers)
    assert response.status_code == 200
    assert main.store.account(account_id)['login_via'] == 'cookie'


def test_legacy_session_without_source_stays_compatible(db):
    account_id = db.add_account(account(auth_mode='session', session='legacy', api_user='7'))
    row = db.account(account_id)
    assert db.public_account(row)['login_via'] is None
    db.update_account(account_id, account(name='旧账号', auth_mode='session', session='legacy', api_user='7'))
    assert db.account(account_id)['login_via'] is None
    assert db.credentials(db.account(account_id)) == {'session': 'legacy', 'api_user': '7'}


@pytest.mark.parametrize('role', ['button', 'link'])
@pytest.mark.parametrize('method', ['github', 'linuxdo'])
@pytest.mark.asyncio
async def test_oauth_uses_only_matching_site_entry(db, role, method):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from backend.login_flow import LoginFlow, OAUTH_BUTTONS
    from backend.providers import PROVIDERS
    button = SimpleNamespace(is_visible=AsyncMock(return_value=True), is_enabled=AsyncMock(return_value=True),
                             click=AsyncMock(), get_attribute=AsyncMock(return_value=f'/api/oauth/{method}'))
    found = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=button))
    absent = SimpleNamespace(count=AsyncMock(return_value=0))
    def locate(actual_role, name):
        return found if actual_role == role and name == OAUTH_BUTTONS[method] else absent
    page = SimpleNamespace(url='https://anyrouter.top/login', get_by_role=locate)
    assert await LoginFlow(db)._click_oauth(None, page, method, PROVIDERS['anyrouter']) is True
    button.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_during_profile_verification_closes_browser_without_saving(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import backend.login_flow as login
    reached = asyncio.Event()
    page = SimpleNamespace(url='https://anyrouter.top/console', goto=AsyncMock())
    context = FakeContext([page])
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(login, 'launch_async', AsyncMock(return_value=browser))
    monkeypatch.setattr(login, 'prepare_browser_page', AsyncMock())
    monkeypatch.setattr(login, 'wait_for_waf_ready', AsyncMock())
    async def pending_profile(*args):
        reached.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(login, 'verify_target_profile', pending_profile)
    flow = login.LoginFlow(db)
    monkeypatch.setattr(flow, '_session_cookie', AsyncMock(return_value='isolated-cookie'))
    session = flow.start('anyrouter', 'manual', '测试')
    await asyncio.wait_for(reached.wait(), 2)
    assert session.status == 'verifying'
    assert flow.cancel(session.id)
    await asyncio.gather(session.task, return_exceptions=True)
    assert session.status == 'cancelled'
    assert db.accounts() == []
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_flow_uses_fresh_profile_and_isolated_session(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import backend.login_flow as login
    page = SimpleNamespace(url='https://agentrouter.org/console', goto=AsyncMock(), evaluate=AsyncMock(return_value={
        'status': 200, 'url': 'https://agentrouter.org/api/user/self', 'body': {'success': True, 'data': {'id': 15}}}))
    context = FakeContext([page], cookies=AsyncMock(return_value=[
        {'name': 'session', 'domain': '.github.com', 'value': 'never-persist'},
        {'name': 'session', 'domain': '.agentrouter.org', 'value': 'target-only'},
    ]))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(login, 'launch_async', AsyncMock(return_value=browser))
    monkeypatch.setattr(login, 'prepare_browser_page', AsyncMock())
    monkeypatch.setattr(login, 'wait_for_waf_ready', AsyncMock())
    flow = login.LoginFlow(db)
    monkeypatch.setattr(flow, '_click_oauth', AsyncMock(return_value=True))
    session = login.LoginSession('agentrouter', 'github', '测试')
    await flow._run(session)
    assert session.status == 'success'
    assert db.credentials(db.account(session.account)) == {
        'session': 'target-only', 'api_user': '15',
        'oauth_cookies': [{'name': 'session', 'domain': '.github.com', 'value': 'never-persist'}]}
    assert 'never-persist' not in json.dumps(db.dashboard())
    assert db.account(session.account)['login_via'] == 'github'
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_serial_windows_wait_until_previous_window_closed(db, monkeypatch):
    from backend.login_flow import LoginFlow, LoginSession
    flow = LoginFlow(db)
    entered = []
    first_started = asyncio.Event()
    first_closed = asyncio.Event()
    async def fake_window(session):
        entered.append(session.id)
        if len(entered) == 1:
            first_started.set()
            await first_closed.wait()
    monkeypatch.setattr(flow, '_run_window', fake_window)
    first = LoginSession('anyrouter', 'manual', '一')
    second = LoginSession('agentrouter', 'manual', '二')
    first_task = asyncio.create_task(flow._run(first))
    await first_started.wait()
    second_task = asyncio.create_task(flow._run(second))
    await asyncio.sleep(0)
    assert entered == [first.id]
    first_closed.set()
    await asyncio.gather(first_task, second_task)
    assert entered == [first.id, second.id]


@pytest.mark.asyncio
async def test_stale_relogin_window_does_not_block_same_account(db, monkeypatch):
    import time
    from backend.login_flow import LoginFlow, WINDOW_TIMEOUT
    flow = LoginFlow(db)
    account_id = db.add_account(account())
    async def waiting(session):
        session.status = 'waiting'
    monkeypatch.setattr(flow, '_run', waiting)
    first = flow.start('anyrouter', 'github', None, account_id)
    await first.task
    first.created = time.time() - WINDOW_TIMEOUT - 31
    second = flow.start('anyrouter', 'linuxdo', None, account_id)
    await second.task
    assert first.status == 'expired'
    assert second.status == 'waiting'
    assert len(db.accounts()) == 1


@pytest.mark.parametrize('method,label', [('github', '  continue WITH  GitHub  '),
                                         ('github', '使用 git hub 继续'),
                                         ('github', 'github_logo 使用 GitHub 继续'),
                                         ('linuxdo', '使用 Linux DO 登录'),
                                         ('linuxdo', 'linuxdo_logo 使用 LinuxDO 继续'),
                                         ('linuxdo', 'SIGN IN WITH linuxdo')])
def test_oauth_entry_label_normalization(method, label):
    from backend.login_flow import OAUTH_BUTTONS
    assert OAUTH_BUTTONS[method].fullmatch(label)
    assert not OAUTH_BUTTONS[method].fullmatch(label.strip() + ' 帮助')


@pytest.mark.asyncio
async def test_oauth_entry_waits_for_react_render(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import backend.login_flow as login
    ticks = iter([0, 0, 1])
    monkeypatch.setattr(login, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    button = SimpleNamespace(is_visible=AsyncMock(return_value=True), is_enabled=AsyncMock(return_value=True), click=AsyncMock())
    found = SimpleNamespace(count=AsyncMock(side_effect=[0, 1]), nth=Mock(return_value=button))
    absent = SimpleNamespace(count=AsyncMock(return_value=0))
    page = SimpleNamespace(url='https://anyrouter.top/login',
                           get_by_role=lambda role, name: found if role == 'button' else absent)
    assert await login.LoginFlow(db)._click_oauth(None, page, 'github', login.PROVIDERS['anyrouter'])
    assert found.count.await_count == 2
    button.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_oauth_entry_does_not_click_same_name_help_link(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import backend.login_flow as login
    ticks = iter([0, 0, login.OAUTH_ENTRY_TIMEOUT + 1])
    monkeypatch.setattr(login, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    link = SimpleNamespace(is_visible=AsyncMock(return_value=True), is_enabled=AsyncMock(return_value=True),
                           get_attribute=AsyncMock(return_value='https://github.com/help'), click=AsyncMock())
    found = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=link))
    absent = SimpleNamespace(count=AsyncMock(return_value=0))
    page = SimpleNamespace(url='https://anyrouter.top/login',
                           get_by_role=lambda role, name: found if role == 'link' else absent)
    assert not await login.LoginFlow(db)._click_oauth(None, page, 'github', login.PROVIDERS['anyrouter'])
    link.click.assert_not_called()


@pytest.mark.asyncio
async def test_oauth_entry_stops_when_page_leaves_target(db):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from backend.login_flow import LoginFlow
    from backend.providers import PROVIDERS
    locate = Mock()
    page = SimpleNamespace(url='https://github.com/login', get_by_role=locate)
    assert not await LoginFlow(db)._click_oauth(None, page, 'github', PROVIDERS['anyrouter'])
    locate.assert_not_called()


@pytest.mark.asyncio
async def test_relogin_updates_metadata_only_after_identity_verified(db):
    from backend.login_flow import LoginFlow, LoginSession
    account_id = db.add_account(account(auth_mode='session', session='old', api_user='7', enabled=False))
    flow = LoginFlow(db)
    rejected = LoginSession('anyrouter', 'github', '不应保存', account_id, interval_hours=24)
    await flow._persist(rejected, {'id': 8}, 'rejected')
    row = db.account(account_id)
    assert rejected.status == 'failed'
    assert row['name'] == '主账号'
    assert row['interval_hours'] == 6
    accepted = LoginSession('anyrouter', 'github', '新备注', account_id, interval_hours=12)
    await flow._persist(accepted, {'id': 7}, 'accepted')
    row = db.account(account_id)
    assert accepted.status == 'success'
    assert row['name'] == '新备注'
    assert row['interval_hours'] == 12
    assert row['enabled'] == 0
    assert row['next_run'] > iso(utcnow() + timedelta(hours=11))
    assert db.credentials(row)['session'] == 'accepted'
    assert '新备注' in accepted.message


@pytest.mark.parametrize('method', ['linuxdo', 'github'])
@pytest.mark.asyncio
async def test_anyrouter_oauth_navigates_register_then_login_when_missing(db, monkeypatch, method):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import LoginFlow, LoginSession
    from backend.providers import PROVIDERS
    flow = LoginFlow(db)
    provider = PROVIDERS['anyrouter']
    page = SimpleNamespace(url=provider.domain + '/login')
    session = LoginSession('anyrouter', method, '导航测试')
    click = AsyncMock(side_effect=[False, True])
    nav = AsyncMock(return_value=True)
    monkeypatch.setattr(flow, '_click_oauth', click)
    monkeypatch.setattr(flow, '_navigate_login_link', nav)
    assert await flow._open_oauth(None, page, session, provider)
    assert click.await_count == 2
    assert [call.args[2:] for call in nav.await_args_list] == [('/login', '/register'), ('/register', '/login')]
    assert db.accounts() == []


@pytest.mark.parametrize('provider_id', ['anyrouter', 'agentrouter'])
@pytest.mark.asyncio
async def test_visible_oauth_does_not_navigate_registration(db, monkeypatch, provider_id):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import LoginFlow, LoginSession
    from backend.providers import PROVIDERS
    flow = LoginFlow(db)
    provider = PROVIDERS[provider_id]
    click = AsyncMock(return_value=True)
    nav = AsyncMock()
    monkeypatch.setattr(flow, '_click_oauth', click)
    monkeypatch.setattr(flow, '_navigate_login_link', nav)
    assert await flow._open_oauth(None, SimpleNamespace(url=provider.domain + '/login'),
                                LoginSession(provider_id, 'linuxdo', '测试'), provider)
    click.assert_awaited_once()
    nav.assert_not_awaited()


@pytest.mark.asyncio
async def test_agentrouter_missing_entry_never_uses_anyrouter_navigation(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import LoginFlow, LoginSession
    from backend.providers import PROVIDERS
    flow = LoginFlow(db)
    monkeypatch.setattr(flow, '_click_oauth', AsyncMock(return_value=False))
    nav = AsyncMock()
    monkeypatch.setattr(flow, '_navigate_login_link', nav)
    assert not await flow._open_oauth(None, SimpleNamespace(url='https://agentrouter.org/login'),
                                    LoginSession('agentrouter', 'linuxdo', '测试'), PROVIDERS['agentrouter'])
    nav.assert_not_awaited()


@pytest.mark.asyncio
async def test_anyrouter_cancel_after_register_stops_navigation(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import LoginFlow, LoginSession
    from backend.providers import PROVIDERS
    flow = LoginFlow(db)
    session = LoginSession('anyrouter', 'linuxdo', '测试')
    click = AsyncMock(return_value=False)
    async def navigate(*args):
        session.cancelled = True
        return True
    nav = AsyncMock(side_effect=navigate)
    monkeypatch.setattr(flow, '_click_oauth', click)
    monkeypatch.setattr(flow, '_navigate_login_link', nav)
    assert not await flow._open_oauth(None, SimpleNamespace(url='https://anyrouter.top/login'), session, PROVIDERS['anyrouter'])
    click.assert_awaited_once()
    nav.assert_awaited_once()
    assert db.accounts() == []


@pytest.mark.asyncio
async def test_registration_navigation_follows_only_existing_site_link(db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from backend.login_flow import LoginFlow
    from backend.providers import PROVIDERS
    page = SimpleNamespace(url='https://anyrouter.top/login')
    async def attribute(name):
        return '/register?from=login' if name == 'href' else None
    async def click(**kwargs):
        page.url = 'https://anyrouter.top/register?from=login'
    link = SimpleNamespace(is_visible=AsyncMock(return_value=True), is_enabled=AsyncMock(return_value=True),
                           get_attribute=AsyncMock(side_effect=attribute), click=AsyncMock(side_effect=click))
    page.get_by_role = Mock(return_value=SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=link)))
    async def wait_for_url(predicate, **kwargs):
        assert predicate(page.url)
    page.wait_for_url = AsyncMock(side_effect=wait_for_url)
    assert await LoginFlow(db)._navigate_login_link(page, PROVIDERS['anyrouter'], '/login', '/register')
    page.get_by_role.assert_called_once_with('link')
    link.click.assert_awaited_once()


@pytest.mark.parametrize('href', ['https://other.test/register', '/help', 'javascript:void(0)', ''])
@pytest.mark.asyncio
async def test_unrelated_links_are_not_registration_navigation(db, monkeypatch, href):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import backend.login_flow as login
    ticks = iter([0, 0, login.OAUTH_ENTRY_TIMEOUT + 1])
    monkeypatch.setattr(login, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    link = SimpleNamespace(is_visible=AsyncMock(return_value=True), is_enabled=AsyncMock(return_value=True),
                           get_attribute=AsyncMock(return_value=href), click=AsyncMock())
    links = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=link))
    page = SimpleNamespace(url='https://anyrouter.top/login', get_by_role=Mock(return_value=links))
    assert not await login.LoginFlow(db)._navigate_login_link(page, login.PROVIDERS['anyrouter'], '/login', '/register')
    link.click.assert_not_awaited()






@pytest.mark.asyncio
async def test_agentrouter_missing_waf_cookie_reports_manual_required(db, monkeypatch):
    import backend.engine as adapter

    from types import SimpleNamespace
    account_id = db.add_account(account(provider='agentrouter', auth_mode='password',
                                       email='u@example.test', password='p'))

    class MissingWAFSession:
        async def __aenter__(self):
            raise adapter.WAFCookiesUnavailable()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(adapter, 'login_with_credentials', AsyncMock(return_value=SimpleNamespace(
        cookies={'session': 's'}, api_user='7')))
    monkeypatch.setattr(adapter, 'BrowserSession', lambda *_args: MissingWAFSession())

    result = await adapter.CheckinEngine().run(db.account(account_id), db.credentials(db.account(account_id)))

    assert result['status'] == 'manual_required'
    assert 'acw_tc' in result['message']
    assert '未发送账号请求' in result['message']
    assert result.get('reward') is None












@pytest.mark.asyncio
async def test_execute_saves_reauthenticated_session_after_checkin(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    account_id = db.add_account(account(provider='agentrouter', auth_mode='session',
                                       email=None, password=None, session='old-execute-cookie', api_user='7'))
    result = {'status': 'success', 'message': '隔离回归结果', 'duration_ms': 1,
              'before_balance': 100, 'after_balance': 125, 'total_quota': 125,
              'used_quota': 0, 'reward': 25,
              '_session_update': {'session': 'fresh-execute-cookie', 'api_user': '7'}}
    run = AsyncMock(return_value=result)
    monkeypatch.setattr(main, 'store', db)
    monkeypatch.setattr(main, 'engine', SimpleNamespace(run=run))
    monkeypatch.setattr(main, 'queued', {account_id})
    monkeypatch.setattr(main, 'active', set())
    finish = Mock(wraps=db.finish_run)
    monkeypatch.setattr(db, 'finish_run', finish)

    await main.execute(account_id, 'manual')

    run.assert_awaited_once()
    assert db.credentials(db.account(account_id)) == {'session': 'fresh-execute-cookie', 'api_user': '7'}
    assert '_session_update' not in finish.call_args.args[2]
    assert db.account(account_id)['status'] == 'success'
    assert db.runs()[0]['reward'] == 25
    assert db.dashboard()['stats']['today_reward'] == 25
    assert 'fresh-execute-cookie' not in json.dumps(db.dashboard())
    assert not main.queued
    assert not main.active


@pytest.fixture
def isolated_profile_window(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import backend.login_flow as login

    response_url = 'https://anyrouter.top/api/user/self'
    page = SimpleNamespace(url='https://anyrouter.top/console', goto=AsyncMock(), evaluate=AsyncMock())
    context = FakeContext([page], cookies=AsyncMock(return_value=[
        {'name': 'session', 'domain': '.anyrouter.top', 'path': '/', 'value': 'isolated-profile-cookie'},
    ]))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(login, 'launch_async', AsyncMock(return_value=browser))
    monkeypatch.setattr(login, 'prepare_browser_page', AsyncMock())
    monkeypatch.setattr(login, 'wait_for_waf_ready', AsyncMock())
    async def yield_loop(*args):
        await asyncio.sleep(0)
    delay = AsyncMock(side_effect=yield_loop)
    monkeypatch.setattr(login, 'asyncio', SimpleNamespace(**{**vars(asyncio), 'sleep': delay}))
    flow = login.LoginFlow(db)
    session = login.LoginSession('anyrouter', 'manual', '隔离身份校验')
    flow.sessions[session.id] = session
    flow.active_id = session.id
    pauses = asyncio.Queue()
    wait_resume = session.resume_event.wait
    async def observed_wait():
        pauses.put_nowait(None)
        return await wait_resume()
    session.resume_event.wait = AsyncMock(side_effect=observed_wait)
    persist = AsyncMock(wraps=flow._persist)
    monkeypatch.setattr(flow, '_persist', persist)
    return SimpleNamespace(flow=flow, session=session, page=page, context=context, browser=browser,
                           response_url=response_url, pauses=pauses, persist=persist, delay=delay,
                           prepare_page=login.prepare_browser_page, wait_ready=login.wait_for_waf_ready)


@pytest.mark.parametrize('method', ['manual', 'github', 'linuxdo'])
@pytest.mark.asyncio
async def test_interactive_authorization_does_not_dismiss_dialogs(isolated_profile_window, method, monkeypatch):
    from unittest.mock import AsyncMock

    window = isolated_profile_window
    window.session.method = method
    window.page.evaluate.return_value = {
        'status': 200, 'url': window.response_url, 'body': {'success': True, 'data': {'id': 31}},
    }
    monkeypatch.setattr(window.flow, '_open_oauth', AsyncMock(return_value=True))

    await asyncio.wait_for(window.flow._run(window.session), 2)

    assert window.session.status == 'success'
    window.page.goto.assert_awaited_once()
    window.prepare_page.assert_not_called()
    window.wait_ready.assert_not_called()
    window.browser.close.assert_awaited_once()


@pytest.mark.parametrize('status,non_json', [(200, True), (403, False), (429, False)])
@pytest.mark.asyncio
async def test_profile_response_requiring_manual_attention_is_not_success(status, non_json):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import ProfileVerificationBlocked, verify_target_profile
    from backend.providers import PROVIDERS

    page = SimpleNamespace(url='https://anyrouter.top/console', evaluate=AsyncMock(return_value={
        'status': status, 'url': 'https://anyrouter.top/api/user/self', 'nonJson': non_json,
        'body': {'success': True, 'data': {'id': 7}},
    }))
    with pytest.raises(ProfileVerificationBlocked, match='暂停自动校验'):
        await verify_target_profile(page, PROVIDERS['anyrouter'])
    page.evaluate.assert_awaited_once()


@pytest.mark.parametrize('status,body,expected', [
    (200, {'success': True, 'data': {'id': 7, 'username': 'isolated-user'}},
     {'id': 7, 'username': 'isolated-user'}),
    (200, {'success': True, 'data': {'id': '7'}}, {'id': '7'}),
    (200, {'success': False, 'data': {'id': 7}}, None),
    (401, {'success': False}, None),
])
@pytest.mark.asyncio
async def test_profile_normal_json_response_remains_compatible(status, body, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.login_flow import verify_target_profile
    from backend.providers import PROVIDERS

    page = SimpleNamespace(url='https://anyrouter.top/console', evaluate=AsyncMock(return_value={
        'status': status, 'url': 'https://anyrouter.top/api/user/self', 'nonJson': False, 'body': body,
    }))
    assert await verify_target_profile(page, PROVIDERS['anyrouter']) == expected
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_json_pause_only_saves_after_resumed_fresh_verification(db, isolated_profile_window):
    window = isolated_profile_window
    fresh_started = asyncio.Event()
    fresh_result = asyncio.Event()
    async def evaluate(script, args):
        if window.page.evaluate.await_count == 1:
            return {'status': 200, 'url': window.response_url, 'nonJson': True,
                    'body': {'success': True, 'data': {'id': 99}}}
        fresh_started.set()
        await fresh_result.wait()
        return {'status': 200, 'url': window.response_url, 'body': {'success': True, 'data': {'id': 17}}}
    window.page.evaluate.side_effect = evaluate
    window.session.task = asyncio.create_task(window.flow._run(window.session))
    try:
        await asyncio.wait_for(window.pauses.get(), 2)
        view = window.flow.view(window.session.id)
        assert view['status'] == 'waiting'
        assert view['stage'] == 'verification_blocked'
        assert view['verification_paused'] is True
        assert view['account_id'] is None
        assert '暂停自动请求' in view['message']
        assert 'isolated-profile-cookie' not in json.dumps(view)
        for _ in range(3):
            await asyncio.sleep(0)
            assert window.flow.view(window.session.id)['verification_paused'] is True
        window.page.evaluate.assert_awaited_once()
        window.persist.assert_not_awaited()
        window.browser.close.assert_not_awaited()
        assert db.accounts() == []

        assert window.flow.resume_verification(window.session.id) is True
        assert window.flow.resume_verification(window.session.id) is False
        await asyncio.wait_for(fresh_started.wait(), 2)
        assert window.session.status == 'verifying'
        assert window.session.stage == 'verifying_target'
        assert window.session.verification_paused is False
        window.persist.assert_not_awaited()
        assert db.accounts() == []
        nonces = [call.args[1]['nonce'] for call in window.page.evaluate.await_args_list]
        assert len(nonces) == len(set(nonces)) == 2

        fresh_result.set()
        await asyncio.wait_for(window.session.task, 2)
        assert window.session.status == 'success'
        window.persist.assert_awaited_once_with(window.session, {'id': 17}, 'isolated-profile-cookie')
        assert len(db.accounts()) == 1
        assert db.credentials(db.account(window.session.account)) == {
            'session': 'isolated-profile-cookie', 'api_user': '17',
        }
        window.browser.close.assert_awaited_once()
    finally:
        await window.flow.close()


@pytest.mark.asyncio
async def test_resume_with_non_json_response_pauses_again_without_polling(db, isolated_profile_window):
    window = isolated_profile_window
    window.page.evaluate.return_value = {'status': 200, 'url': window.response_url, 'nonJson': True}
    window.session.task = asyncio.create_task(window.flow._run(window.session))
    try:
        await asyncio.wait_for(window.pauses.get(), 2)
        assert window.flow.resume_verification(window.session.id)
        await asyncio.wait_for(window.pauses.get(), 2)
        for _ in range(3):
            await asyncio.sleep(0)
        assert window.page.evaluate.await_count == 2
        assert window.session.status == 'waiting'
        assert window.session.stage == 'verification_blocked'
        assert window.session.verification_paused is True
        window.persist.assert_not_awaited()
        assert db.accounts() == []
    finally:
        await window.flow.close()
    window.browser.close.assert_awaited_once()


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.asyncio
async def test_cancel_paused_verification_never_saves(db, isolated_profile_window, existing):
    window = isolated_profile_window
    if existing:
        account_id = db.add_account(account(auth_mode='session', session='original-isolated-cookie', api_user='7'))
        window.session.account_id = window.session.account = account_id
    before = [dict(row) for row in db.accounts()]
    window.page.evaluate.return_value = {'status': 200, 'url': window.response_url, 'nonJson': True}
    window.session.task = asyncio.create_task(window.flow._run(window.session))
    try:
        await asyncio.wait_for(window.pauses.get(), 2)
        assert window.flow.cancel(window.session.id)
        await asyncio.wait_for(asyncio.gather(window.session.task, return_exceptions=True), 2)
        assert window.session.status == 'cancelled'
        assert window.flow.resume_verification(window.session.id) is False
        window.page.evaluate.assert_awaited_once()
        window.persist.assert_not_awaited()
        window.browser.close.assert_awaited_once()
        assert [dict(row) for row in db.accounts()] == before
        if existing:
            assert db.credentials(db.account(account_id))['session'] == 'original-isolated-cookie'
    finally:
        await window.flow.close()


def test_resume_api_reports_stage_and_accepts_each_pause_only_once(client):
    from backend.login_flow import LoginSession

    session = LoginSession('anyrouter', 'manual', '隔离恢复接口')
    session.status = 'waiting'
    session.stage = 'verification_blocked'
    session.verification_paused = True
    main.login_flow.sessions[session.id] = session
    assert client.get(f'/api/login/{session.id}').json()['verification_paused'] is True

    response = client.post(f'/api/login/{session.id}/resume', json={}, headers=client.write_headers)
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['status'] == 'waiting'
    assert body['stage'] == 'verification_blocked'
    assert body['verification_paused'] is False
    assert body['account_id'] is None
    assert session.resume_event.is_set()
    assert not {'resume_event', 'session', 'profile', 'encrypted'} & body.keys()
    assert client.post(f'/api/login/{session.id}/resume', json={}, headers=client.write_headers).status_code == 409
    assert main.store.accounts() == []


@pytest.mark.parametrize('status,paused', [
    ('waiting', False), ('verifying', False), ('success', True), ('cancelled', True), ('expired', True),
])
def test_resume_api_rejects_sessions_not_waiting_for_manual_action(client, status, paused):
    from backend.login_flow import LoginSession

    session = LoginSession('anyrouter', 'manual', '隔离无效恢复')
    session.status = status
    session.verification_paused = paused
    main.login_flow.sessions[session.id] = session
    response = client.post(f'/api/login/{session.id}/resume', json={}, headers=client.write_headers)
    assert response.status_code == 409
    assert session.status == status
    assert session.verification_paused is paused
    assert not session.resume_event.is_set()
    assert main.store.accounts() == []


def test_resume_api_rejects_unknown_session(client):
    response = client.post('/api/login/unknown-isolated-session/resume', json={}, headers=client.write_headers)
    assert response.status_code == 409
    assert main.store.accounts() == []


@pytest.mark.asyncio
async def test_profile_verification_checks_each_target_tab_before_waiting(db, isolated_profile_window):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    window = isolated_profile_window
    checked = []
    async def older_response(*args):
        checked.append('older')
        return {'status': 200, 'url': window.response_url, 'body': {'success': True, 'data': {'id': 23}}}
    async def newer_response(*args):
        checked.append('newer')
        return {'status': 401, 'url': window.response_url, 'body': {'success': False}}
    window.page.evaluate.side_effect = older_response
    newer_page = SimpleNamespace(url='https://anyrouter.top/login', evaluate=AsyncMock(side_effect=newer_response))
    window.context.pages.append(newer_page)

    await asyncio.wait_for(window.flow._run(window.session), 2)

    assert checked == ['newer', 'older']
    assert window.session.status == 'success'
    window.page.evaluate.assert_awaited_once()
    newer_page.evaluate.assert_awaited_once()
    window.delay.assert_not_awaited()
    window.persist.assert_awaited_once_with(window.session, {'id': 23}, 'isolated-profile-cookie')
    assert db.credentials(db.account(window.session.account))['api_user'] == '23'
    window.browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_spa_context_navigation_waits_then_verifies_again(db, isolated_profile_window):
    from playwright.async_api import Error as PlaywrightError

    window = isolated_profile_window
    waiting = asyncio.Event()
    continue_navigation = asyncio.Event()
    window.page.evaluate.side_effect = [
        PlaywrightError('Page.evaluate: Execution context was destroyed, most likely because of a navigation'),
        {'status': 200, 'url': window.response_url, 'body': {'success': True, 'data': {'id': 29}}},
    ]
    async def wait_navigation(*args):
        waiting.set()
        await continue_navigation.wait()
    window.delay.side_effect = wait_navigation
    window.session.task = asyncio.create_task(window.flow._run(window.session))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        view = window.flow.view(window.session.id)
        assert view['status'] == 'waiting'
        assert view['stage'] == 'returning_to_target'
        assert view['verification_paused'] is False
        window.page.evaluate.assert_awaited_once()
        window.persist.assert_not_awaited()
        assert db.accounts() == []

        continue_navigation.set()
        await asyncio.wait_for(window.session.task, 2)
        assert window.session.status == 'success'
        assert window.page.evaluate.await_count == 2
        window.persist.assert_awaited_once_with(window.session, {'id': 29}, 'isolated-profile-cookie')
        assert db.credentials(db.account(window.session.account))['api_user'] == '29'
        window.browser.close.assert_awaited_once()
    finally:
        await window.flow.close()


# --- LoginEvidence：只认本次独立 context 内、同源、JSON、success 且会话一致的正常响应 ---

class FakeResponse:
    def __init__(self, url, request, status=200, content_type='application/json', body=None,
                 set_cookie=(), from_service_worker=False, finished=None):
        self.url = url
        self.request = request
        self.status = status
        self._content_type = content_type
        self._body = {'success': True, 'data': {'id': 7}} if body is None else body
        self._set_cookie = list(set_cookie)
        self.from_service_worker = from_service_worker
        self._finished = finished

    async def all_headers(self):
        return {'content-type': self._content_type}

    async def header_values(self, name):
        return list(self._set_cookie) if name == 'set-cookie' else []

    async def finished(self):
        return self._finished

    async def json(self):
        if self._finished:
            from playwright.async_api import Error
            raise Error('Response body was aborted')
        return self._body


class FakeRequest:
    def __init__(self, url, page, frame_url=None, headers=None):
        self.url = url
        self.frame = SimpleNamespace(page=page, url=frame_url or url)
        self._headers = headers or {}

    async def all_headers(self):
        return self._headers


def make_page(context, url='https://agentrouter.org/console'):
    return SimpleNamespace(url=url, context=context)


async def settle():
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [
    ('github', '/api/oauth/github?code=abc&state=xyz&mode=login'),
    ('linuxdo', '/api/oauth/linuxdo?code=abc&state=xyz&mode=login'),
    ('manual', '/api/oauth/github?code=abc&state=xyz&mode=login'),
    ('manual', '/api/oauth/linuxdo?code=abc&state=xyz&mode=login'),
    ('manual', '/api/user/login'),
])
async def test_login_evidence_accepts_own_json_login_response(method, path):
    from backend.login_flow import LoginEvidence
    from backend.providers import PROVIDERS

    provider = PROVIDERS['agentrouter']
    context = FakeContext()
    page = make_page(context)
    context.pages = [page]
    evidence = LoginEvidence(context, provider, method, AsyncMock(return_value='site-cookie'))
    evidence.start()
    assert context.listeners['response']

    request = FakeRequest(provider.domain + path, page)
    response = FakeResponse(provider.domain + path, request,
                            body={'success': True, 'data': {'id': 7, 'username': 'u', 'quota': 5000000,
                                                            'used_quota': 0, 'token': 'must-not-leak'}},
                            set_cookie=['session=site-cookie; Path=/; HttpOnly'])
    context.listeners['response'][0](response)
    await evidence.changed.wait()
    await settle()

    captured = await evidence.get()
    assert captured == ({'id': 7, 'username': 'u', '_login_response': True}, 'site-cookie')
    await evidence.close()
    assert context.listeners['response'] == []


@pytest.mark.asyncio
async def test_checkin_login_evidence_rejects_old_session_self_and_accepts_fresh_login():
    """The old /console profile is not proof; a post-logout login response is required."""
    from backend.login_flow import LoginEvidence
    from backend.providers import PROVIDERS

    provider = PROVIDERS['agentrouter']
    context = FakeContext()
    page = make_page(context)
    context.pages = [page]
    read_cookie = AsyncMock(return_value='fresh-session-cookie')
    evidence = LoginEvidence(context, provider, 'github', read_cookie, require_login_response=True)
    evidence.start()
    handler = context.listeners['response'][0]

    self_url = provider.domain + provider.user_info_path
    handler(FakeResponse(self_url, FakeRequest(self_url, page,
        headers={'cookie': 'session=old-session-cookie'}),
        body={'success': True, 'data': {'id': 7, 'quota': 50000000, 'used_quota': 0}}))
    await settle()
    assert await evidence.get() is None
    read_cookie.assert_not_awaited()

    evidence.invalidate()
    login_path = provider.domain + '/api/oauth/github?code=abc&state=xyz&mode=login'
    handler(FakeResponse(login_path, FakeRequest(login_path, page),
        body={'success': True, 'data': {'id': 7, 'quota': 57500000, 'used_quota': 0}},
        set_cookie=['session=fresh-session-cookie; Path=/; HttpOnly']))
    await settle()

    assert await evidence.get() == ({'id': 7, '_login_response': True}, 'fresh-session-cookie')
    assert read_cookie.await_count == 2
    assert all(c.args == (context, provider) for c in read_cookie.await_args_list)
    await evidence.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', [
    'cross_origin', 'wrong_path', 'not_json', 'failed_status', 'success_false',
    'bind_message', 'bad_id', 'no_code', 'wrong_mode', 'cookie_mismatch',
    'service_worker', 'aborted', 'foreign_context', 'frame_offsite',
])
async def test_login_evidence_rejects_untrusted_responses(case):
    from backend.login_flow import LoginEvidence
    from backend.providers import PROVIDERS

    provider = PROVIDERS['agentrouter']
    context = FakeContext()
    page = make_page(context)
    other_page = make_page(FakeContext())
    context.pages = [page]
    evidence = LoginEvidence(context, provider, 'github', AsyncMock(return_value='site-cookie'))
    evidence.start()

    path = '/api/oauth/github?code=abc&state=xyz&mode=login'
    if case == 'wrong_path':
        path = '/api/user/login/other'
    if case == 'no_code':
        path = '/api/oauth/github?state=xyz&mode=login'
    if case == 'wrong_mode':
        path = '/api/oauth/github?code=abc&state=xyz&mode=register'
    url = ('https://evil.example' + path) if case == 'cross_origin' else provider.domain + path
    owner = other_page if case == 'foreign_context' else page
    request = FakeRequest(url, owner, frame_url='https://evil.example/frame' if case == 'frame_offsite' else provider.domain + path)
    response = FakeResponse(
        url, request,
        status=500 if case == 'failed_status' else 200,
        content_type='text/html' if case == 'not_json' else 'application/json',
        body={'success': case != 'success_false', 'message': 'bind' if case == 'bind_message' else None,
              'data': {'id': 'abc' if case == 'bad_id' else 7}},
        set_cookie=['session=other-cookie'] if case == 'cookie_mismatch' else ['session=site-cookie; Path=/'],
        from_service_worker=case == 'service_worker',
        finished='net::ERR_ABORTED' if case == 'aborted' else None,
    )
    context.listeners['response'][0](response)
    await settle()

    assert await evidence.get() is None
    await evidence.close()


@pytest.mark.asyncio
async def test_login_evidence_invalidates_earlier_login_on_second_attempt():
    """同一窗口内第二次登录请求必须让上一次的登录证据作废。"""
    from backend.login_flow import LoginEvidence
    from backend.providers import PROVIDERS

    provider = PROVIDERS['agentrouter']
    context = FakeContext()
    page = make_page(context)
    context.pages = [page]
    evidence = LoginEvidence(context, provider, 'github', AsyncMock(return_value='site-cookie'))
    evidence.start()
    path = provider.domain + '/api/oauth/github?code=abc&state=xyz&mode=login'
    handler = context.listeners['response'][0]

    handler(FakeResponse(path, FakeRequest(path, page),
                         set_cookie=['session=site-cookie; Path=/']))
    await settle()
    assert await evidence.get() is not None

    handler(FakeResponse(path, FakeRequest(path, page), status=500, body={'success': False}))
    await settle()
    assert await evidence.get() is None
    await evidence.close()


@pytest.mark.asyncio
async def test_login_evidence_accepts_self_response_matching_current_cookie():
    from backend.login_flow import LoginEvidence
    from backend.providers import PROVIDERS

    provider = PROVIDERS['agentrouter']
    context = FakeContext()
    page = make_page(context)
    context.pages = [page]
    evidence = LoginEvidence(context, provider, 'manual', AsyncMock(return_value='site-cookie'))
    evidence.start()
    url = provider.domain + provider.user_info_path
    request = FakeRequest(url, page, headers={'cookie': 'acw_tc=x; session=site-cookie'})
    context.listeners['response'][0](FakeResponse(url, request, body={'success': True, 'data': {'id': 9}}))
    await settle()

    assert await evidence.get() == ({'id': 9}, 'site-cookie')
    await evidence.close()


@pytest.mark.asyncio
async def test_login_evidence_rejects_self_response_with_stale_cookie():
    from backend.login_flow import LoginEvidence
    from backend.providers import PROVIDERS

    provider = PROVIDERS['agentrouter']
    context = FakeContext()
    page = make_page(context)
    context.pages = [page]
    read_cookie = AsyncMock(return_value='rotated-cookie')
    evidence = LoginEvidence(context, provider, 'manual', read_cookie)
    evidence.start()
    url = provider.domain + provider.user_info_path
    request = FakeRequest(url, page, headers={'cookie': 'session=old-cookie'})
    context.listeners['response'][0](FakeResponse(url, request, body={'success': True, 'data': {'id': 9}}))
    await settle()

    assert await evidence.get() is None
    await evidence.close()


@pytest.mark.asyncio
async def test_login_evidence_expires_and_requires_target_page():
    from backend.login_flow import LoginEvidence
    from backend.providers import PROVIDERS

    provider = PROVIDERS['agentrouter']
    context = FakeContext()
    page = make_page(context)
    context.pages = [page]
    evidence = LoginEvidence(context, provider, 'manual', AsyncMock(return_value='site-cookie'))
    evidence.start()
    url = provider.domain + provider.user_info_path
    context.listeners['response'][0](FakeResponse(url, FakeRequest(url, page, headers={'cookie': 'session=site-cookie'})))
    await settle()
    assert await evidence.get() is not None

    page.url = 'https://github.com/login'
    assert await evidence.get() is None
    page.url = provider.domain + '/console'
    context.pages = []
    assert await evidence.get() is None
    await evidence.close()


@pytest.mark.asyncio
async def test_login_evidence_never_retains_raw_auth_payload():
    from backend.login_flow import LoginEvidence
    from backend.providers import PROVIDERS

    provider = PROVIDERS['agentrouter']
    context = FakeContext()
    page = make_page(context)
    context.pages = [page]
    evidence = LoginEvidence(context, provider, 'github', AsyncMock(return_value='site-cookie'))
    evidence.start()
    path = provider.domain + '/api/oauth/github?code=secret-code&state=s&mode=login'
    context.listeners['response'][0](FakeResponse(path, FakeRequest(path, page),
        body={'success': True, 'data': {'id': 7, 'access_token': 'sk-secret', 'password': 'p'},
              'session': 'raw-session'},
        set_cookie=['session=site-cookie; Path=/']))
    await settle()

    profile, cookie = await evidence.get()
    assert cookie == 'site-cookie'
    assert set(profile) <= {'id', 'username', 'quota', 'used_quota', 'checked_in', '_login_response'}
    await evidence.close()








@pytest.mark.asyncio
async def test_window_refreshes_login_placeholder_money_from_self(db, monkeypatch):
    """登录回包只确认身份，余额必须来自用户信息接口。"""
    from unittest.mock import AsyncMock
    import backend.login_flow as login

    provider = login.PROVIDERS['agentrouter']
    page = SimpleNamespace(url=provider.domain + '/console', goto=AsyncMock(), evaluate=AsyncMock())
    context = FakeContext([page], cookies=AsyncMock(return_value=[
        {'name': 'session', 'domain': '.agentrouter.org', 'path': '/', 'value': 'evidence-cookie'},
    ]))
    page.context = context
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(login, 'launch_async', AsyncMock(return_value=browser))
    verify = AsyncMock(return_value={'id': 7, 'quota': 45000000, 'used_quota': 5000000})
    monkeypatch.setattr(login, 'verify_target_profile', verify)
    flow = login.LoginFlow(db)
    path = provider.domain + '/api/oauth/linuxdo?code=abc&state=xyz&mode=login'

    async def open_oauth(context_, page_, session_, provider_):
        # 模拟站点自身完成授权并返回登录成功响应
        context_.listeners['response'][0](FakeResponse(
            path, FakeRequest(path, page_, frame_url=path),
            body={'success': True, 'data': {'id': 7, 'username': 'linuxdo-user',
                                            'quota': 0, 'used_quota': 0}},
            set_cookie=['session=evidence-cookie; Path=/; HttpOnly']))
        await settle()
        return True
    monkeypatch.setattr(flow, '_open_oauth', open_oauth)

    session = login.LoginSession('agentrouter', 'linuxdo', '证据窗口')
    await asyncio.wait_for(flow._run(session), 5)

    assert session.status == 'success'
    assert session.stage == 'identity_confirmed'
    verify.assert_awaited_once_with(page, provider, 7)
    account = db.account(session.account)
    assert db.credentials(account) == {'session': 'evidence-cookie', 'api_user': '7'}
    assert account['name'] == '证据窗口'
    assert account['login_via'] == 'linuxdo'
    assert account['balance'] == 90
    assert account['used_quota'] == 10
    assert account['total_quota'] == 100
    assert account['has_snapshot'] == 1
    browser.close.assert_awaited_once()






@pytest.fixture(autouse=True)
def forbid_unmocked_unit_test_browsers(monkeypatch):
    # Unit tests must fail locally instead of opening real login windows or sites.
    import backend.engine as engine
    import backend.login_flow as login
    forbidden = AsyncMock(side_effect=AssertionError('Test must provide an isolated browser'))
    monkeypatch.setattr(engine, 'launch_async', forbidden)
    monkeypatch.setattr(login, 'launch_async', forbidden)
    monkeypatch.setattr(login, 'setup_notice_guard', AsyncMock())
    monkeypatch.setattr(login.LoginFlow, '_wait_oauth_config', AsyncMock(return_value='fixture-client'))
    monkeypatch.setattr(login.LoginFlow, '_minimize_page', AsyncMock())
    monkeypatch.setattr(login, 'UPSTREAM_VERIFICATION_GRACE', 0)


@pytest.fixture
def engine_responses(monkeypatch):
    import backend.engine as engine
    def install(responses):
        calls = []
        class Session:
            def __init__(self, provider, cookies):
                self.cookies = cookies
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def request(self, path, user, method='GET', payload=None):
                calls.append((path, method, dict(self.cookies)))
                response = responses.pop(0)
                if isinstance(response, Exception): raise response
                return response
        monkeypatch.setattr(engine, 'BrowserSession', Session)
        return calls
    return install


def profile_response(quota=50000000, used=0):
    return {'status': 200, 'body': {'success': True, 'data': {'id': 7, 'quota': quota, 'used_quota': used}}}


@pytest.mark.asyncio
async def test_repeated_legacy_checkin_reuses_cookie_and_repairs_balance_without_popup(db, engine_responses):
    from backend.engine import CheckinEngine
    account_id = db.add_account(account(provider='agentrouter', auth_mode='session',
                                       session='saved', api_user='7'))
    db.patch(account_id, balance=0, used_quota=0, total_quota=0, has_snapshot=1)
    calls = engine_responses([profile_response(909122424, 640877576)] * 2)
    login = AsyncMock(side_effect=AssertionError('No cache: never open an authorization window'))
    for _ in range(2):
        row = db.account(account_id)
        result = await CheckinEngine(login).run(row, db.credentials(row), True)
        db.finish_run(db.begin_run(row, 'manual'), account_id, result, 'manual')
        assert result['status'] == 'synced'
        assert result['reward'] is None
        assert result['after_balance'] == 1818.24
        assert result['total_quota'] == 3100
    login.assert_not_awaited()
    assert calls == [('/api/user/self', 'GET', {'session': 'saved'})] * 2
    assert db.account(account_id)['balance'] == 1818.24
    assert db.dashboard()['stats']['today_reward'] == 0


@pytest.mark.asyncio
async def test_expired_site_cookie_never_opens_login(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='old', api_user='7'))
    engine_responses([{'status': 401, 'body': {'success': False}}])
    login = AsyncMock()
    r = await CheckinEngine(login).run(db.account(i), db.credentials(db.account(i)))
    assert r['status'] == 'manual_required'
    assert '重新授权' in r['message']
    login.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('checked,expected,reward', [(True, 'success', 25), (False, 'synced', 0), (None, 'synced', None)])
async def test_agentrouter_reward_requires_login_evidence_and_fresh_baseline(db, engine_responses, checked, expected, reward):
    from backend.engine import CheckinEngine
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='saved', api_user='7',
                               oauth_cookies=[{'name': 'upstream', 'value': 'cached'}]))
    db.patch(i, balance=0, total_quota=0, has_snapshot=1)
    engine_responses([profile_response(50000000, 12500000)])
    login = AsyncMock(return_value={'session': 'new', 'profile': {
        'id': 7, 'quota': 61000000, 'used_quota': 14000000, 'checked_in': checked}})
    row = db.account(i)
    r = await CheckinEngine(login).run(row, db.credentials(row), True)
    assert r['status'] == expected
    assert r['reward'] == reward
    assert r['before_balance'] == 100
    assert r['after_balance'] == 122
    assert r['total_quota'] == 150
    assert r['_session_update']['session'] == 'new'
    login.assert_awaited_once_with(row, '7')


@pytest.mark.asyncio
async def test_cached_oauth_needing_manual_attention_preserves_site_session(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='saved', api_user='7',
                               oauth_cookies=[{'name': 'upstream', 'value': 'cached'}]))
    calls = engine_responses([profile_response()])
    login = AsyncMock(return_value=None)
    r = await CheckinEngine(login).run(db.account(i), db.credentials(db.account(i)))
    assert r['status'] == 'manual_required'
    assert r['after_balance'] == 100
    assert r['reward'] is None
    assert '_session_update' not in r
    assert len(calls) == 1
    assert db.credentials(db.account(i))['session'] == 'saved'


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [None, {}, {'quota': 0}, {'quota': True, 'used_quota': 0},
                                 {'quota': 'bad', 'used_quota': 0}])
async def test_anyrouter_success_with_missing_balance_does_not_write_zero(db, engine_responses, body):
    from backend.engine import CheckinEngine
    i = db.add_account(account(auth_mode='session', session='saved', api_user='7'))
    db.patch(i, balance=100, used_quota=0, total_quota=100)
    engine_responses([profile_response(), {'status': 200, 'body': {'success': True}},
                      {'status': 200, 'body': {'success': True, 'data': body}}])
    row = db.account(i)
    r = await CheckinEngine().run(row, db.credentials(row))
    db.finish_run(db.begin_run(row, 'manual'), i, r, 'manual')
    assert r['status'] == 'success'
    assert r['reward'] is None
    assert 'after_balance' not in r
    assert '暂未确认' in r['message']
    assert db.account(i)['balance'] == 100


@pytest.mark.asyncio
async def test_anyrouter_reward_uses_total_quota_and_repeat_adds_nothing(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(auth_mode='session', session='saved', api_user='7'))
    row = db.account(i)
    engine_responses([profile_response(), {'status': 200, 'body': {'success': True}},
                      profile_response(62000000, 500000), profile_response(62000000, 500000),
                      {'status': 200, 'body': {'success': False, 'message': '今日已签到'}},
                      profile_response(62000000, 500000)])
    first = await CheckinEngine().run(row, db.credentials(row))
    second = await CheckinEngine().run(row, db.credentials(row))
    assert (first['status'], first['reward'], first['after_balance']) == ('success', 25, 124)
    assert (second['status'], second['reward']) == ('already_checked', 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('method,domain', [('github', '.github.com'), ('linuxdo', '.linux.do')])
async def test_background_oauth_reuses_only_its_cache_and_refreshes_placeholder_money(db, monkeypatch, method, domain):
    import backend.login_flow as login
    upstream = {'name': 'upstream', 'value': 'cached', 'domain': domain, 'path': '/'}
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='old-site', api_user='7',
                               login_via=method, oauth_cookies=[upstream]))
    provider = login.PROVIDERS['agentrouter']
    page = SimpleNamespace(url=provider.domain + '/console', goto=AsyncMock())
    context = FakeContext([page], cookies=AsyncMock(return_value=[upstream, {
        'name': 'session', 'value': 'new-site', 'domain': 'agentrouter.org', 'path': '/'}]))
    context.add_cookies = AsyncMock()
    page.context = context
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    launch = AsyncMock(return_value=browser)
    monkeypatch.setattr(login, 'launch_async', launch)
    verify = AsyncMock(return_value={'id': 7, 'quota': 61000000, 'used_quota': 14000000})
    monkeypatch.setattr(login, 'verify_target_profile', verify)
    flow = login.LoginFlow(db)
    path = provider.domain + f'/api/oauth/{method}?code=fixture&state=fixture&mode=login'
    async def oauth(context_, page_, session_, provider_):
        context_.listeners['response'][0](FakeResponse(path, FakeRequest(path, page_, frame_url=path),
            body={'success': True, 'data': {'id': 7, 'quota': 0, 'used_quota': 0, 'checked_in': True}},
            set_cookie=['session=new-site; Path=/']))
        await settle()
        return True
    monkeypatch.setattr(flow, '_open_oauth', oauth)
    r = await flow.authenticate_for_checkin(db.account(i), '7')
    assert r['profile']['quota'] == 61000000
    assert r['profile']['checked_in'] is True
    assert r['session'] == 'new-site'
    assert r['oauth_cookies'] == [upstream]
    assert launch.call_args.kwargs['headless'] is (method != 'linuxdo')
    context.add_cookies.assert_awaited_once_with([upstream])
    assert all('/logout' not in call.args[0] for call in page.goto.await_args_list)
    verify.assert_awaited_once_with(page, provider, 7)
    assert db.credentials(db.account(i))['session'] == 'old-site'
    assert flow.sessions == {}
    await flow.close()
    browser.close.assert_awaited_once()


def test_oauth_cookie_cache_is_domain_limited_and_ignores_expired_cookies():
    from backend.login_flow import LoginFlow
    cookies = [{'name': 'c', 'value': 'fixture', 'domain': d, 'path': '/'} for d in
               ['.linux.do', 'connect.linux.do', 'linux.do.evil.test', '.github.com', 'agentrouter.org']]
    cookies.append({'name': 'expired', 'value': 'fixture', 'domain': '.linux.do', 'expires': 1})
    assert [c['domain'] for c in LoginFlow._oauth_cookies(cookies, 'linuxdo')] == ['.linux.do', 'connect.linux.do']


@pytest.mark.asyncio
async def test_anyrouter_balance_timeout_preserves_acknowledged_success(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(auth_mode='session', session='saved', api_user='7'))
    engine_responses([profile_response(), {'status': 200, 'body': {'success': True}}, TimeoutError()])
    r = await CheckinEngine().run(db.account(i), db.credentials(db.account(i)))
    assert r['status'] == 'success'
    assert r['reward'] is None
    assert 'after_balance' not in r
    assert '保留上次余额' in r['message']


@pytest.mark.asyncio
async def test_agentrouter_confirmed_checkin_without_money_keeps_reward_unknown(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='saved', api_user='7',
                               oauth_cookies=[{'name': 'upstream', 'value': 'cached'}]))
    engine_responses([profile_response()])
    login = AsyncMock(return_value={'session': 'new', 'profile': {'id': 7, 'checked_in': True}})
    r = await CheckinEngine(login).run(db.account(i), db.credentials(db.account(i)))
    assert r['status'] == 'success'
    assert r['reward'] is None
    assert 'after_balance' not in r
    assert r['_session_update']['session'] == 'new'


@pytest.mark.asyncio
@pytest.mark.parametrize('refresh', [None, {'id': 8, 'quota': 50000000, 'used_quota': 0}])
async def test_login_placeholder_never_overwrites_money_when_refresh_fails(db, monkeypatch, refresh):
    import backend.login_flow as login
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='old', api_user='7'))
    db.patch(i, balance=100, used_quota=5, total_quota=105)
    flow = login.LoginFlow(db)
    session = login.LoginSession('agentrouter', 'github', 'fixture', i)
    page = SimpleNamespace(url='https://agentrouter.org/console')
    context = FakeContext([page], cookies=AsyncMock(return_value=[{
        'name': 'session', 'value': 'new', 'domain': 'agentrouter.org', 'path': '/'}]))
    monkeypatch.setattr(login, 'verify_target_profile', AsyncMock(return_value=refresh))
    await flow._complete_login(session, {'id': 7, '_login_response': True}, 'new', context)
    row = db.account(i)
    assert row['balance'] == 100
    assert row['total_quota'] == 105
    assert session.status == ('failed' if refresh else 'success')
    assert db.credentials(row)['session'] == ('old' if refresh else 'new')


@pytest.mark.asyncio
async def test_expired_site_session_can_refresh_with_cached_oauth_without_stale_reward(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='expired', api_user='7',
                               oauth_cookies=[{'name': 'upstream', 'value': 'cached'}]))
    db.patch(i, balance=0, total_quota=0, has_snapshot=1)
    engine_responses([{'status': 401, 'body': {'success': False}}])
    login = AsyncMock(return_value={'session': 'new', 'profile': {
        'id': 7, 'quota': 61000000, 'used_quota': 14000000, 'checked_in': True}})
    r = await CheckinEngine(login).run(db.account(i), db.credentials(db.account(i)), True)
    login.assert_awaited_once()
    assert r['status'] == 'success'
    assert r['after_balance'] == 122
    assert r['reward'] is None
    assert '签到前实时余额' in r['message']
    assert r['_session_update']['session'] == 'new'


@pytest.mark.asyncio
async def test_background_oauth_timeout_is_actionable_without_popup(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='saved', api_user='7',
                               oauth_cookies=[{'name': 'upstream', 'value': 'cached'}]))
    engine_responses([profile_response()])
    r = await CheckinEngine(AsyncMock(side_effect=TimeoutError())).run(db.account(i), db.credentials(db.account(i)))
    assert r['status'] == 'manual_required'
    assert r['after_balance'] == 100
    assert '尚不能判断登录会话是否过期' in r['message']
    assert '_session_update' not in r


def test_site_cookie_cache_excludes_other_accounts_domains_and_expired_values():
    from backend.cookies import site_cookies
    from backend.providers import PROVIDERS
    cookies = [{'name': 'acw_tc', 'value': 'fixture', 'domain': d, 'path': '/'} for d in
               ['.agentrouter.org', '.org', '.github.com', '.anyrouter.top', 'evil-agentrouter.org']]
    cookies.extend([{'name': 'session', 'value': 'old', 'domain': 'agentrouter.org'},
                    {'name': 'acw_tc', 'value': 'expired', 'domain': 'agentrouter.org', 'expires': 1}])
    assert site_cookies(cookies, PROVIDERS['agentrouter']) == [cookies[0]]


@pytest.mark.asyncio
async def test_browser_restores_first_party_cookie_metadata_before_bootstrap(monkeypatch):
    import backend.engine as engine
    from backend.providers import PROVIDERS
    events = []
    saved = {'name': 'acw_tc', 'value': 'fixture', 'domain': '.agentrouter.org', 'path': '/',
             'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}
    page = SimpleNamespace(url='https://agentrouter.org/login')
    context = SimpleNamespace(new_page=AsyncMock(return_value=page),
                              cookies=AsyncMock(return_value=[saved]),
                              add_cookies=AsyncMock(side_effect=lambda cookies: events.append(('cookies', cookies))))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(engine, 'launch_async', AsyncMock(return_value=browser))
    monkeypatch.setattr(engine, 'prepare_browser_page', AsyncMock())
    monkeypatch.setattr(engine, 'wait_for_waf_ready', AsyncMock())
    monkeypatch.setattr(engine.BrowserSession, '_goto', AsyncMock(side_effect=lambda url: events.append(('goto', url))))
    async with engine.BrowserSession(PROVIDERS['agentrouter'], {'session': 'saved-site'}, [saved]) as client:
        assert await client.export_site_cookies() == [saved]
    assert events[0] == ('cookies', [saved])
    assert events[1] == ('goto', 'https://agentrouter.org/login')
    assert events[2][1][0]['name'] == 'session'


@pytest.mark.asyncio
async def test_login_success_with_guarded_balance_keeps_window_until_user_verifies(db, monkeypatch):
    import backend.login_flow as login
    flow = login.LoginFlow(db)
    session = login.LoginSession('agentrouter', 'linuxdo', 'fixture')
    flow.sessions[session.id] = session
    page = SimpleNamespace(url='https://agentrouter.org/console')
    cookies = [{'name': 'session', 'value': 'verified', 'domain': 'agentrouter.org', 'path': '/'},
               {'name': 'acw_tc', 'value': 'site-verified', 'domain': '.agentrouter.org', 'path': '/'},
               {'name': 'upstream', 'value': 'upstream-verified', 'domain': '.linux.do', 'path': '/'}]
    context = FakeContext([page], cookies=AsyncMock(return_value=cookies))
    evidence = SimpleNamespace(changed=asyncio.Event(), get=AsyncMock(return_value=None))
    verify = AsyncMock(side_effect=[login.ProfileVerificationBlocked(),
                                   {'id': 7, 'quota': 60000000, 'used_quota': 2500000}])
    monkeypatch.setattr(login, 'verify_target_profile', verify)
    task = asyncio.create_task(flow._complete_login(session,
        {'id': 7, '_login_response': True, 'checked_in': True}, 'verified', context, evidence))
    try:
        for _ in range(80):
            if session.verification_paused: break
            await asyncio.sleep(.02)
        assert session.verification_paused
        assert not task.done()
        assert db.accounts() == []
        assert verify.await_count == 1
        assert flow.resume_verification(session.id)
        await asyncio.wait_for(task, 2)
        row = db.account(session.account)
        assert session.status == 'success'
        assert row['balance'] == 120
        assert db.credentials(row)['site_cookies'] == [cookies[1]]
        assert db.credentials(row)['oauth_cookies'] == [cookies[2]]
        assert 'site-verified' not in json.dumps(db.dashboard())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_explicit_reauthorization_restores_saved_oauth_instead_of_empty_window(db, monkeypatch):
    import backend.login_flow as login
    upstream = {'name': 'upstream', 'value': 'cached', 'domain': '.linux.do', 'path': '/'}
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='old', api_user='7',
                               login_via='linuxdo', oauth_cookies=[upstream]))
    page = SimpleNamespace(url='https://agentrouter.org/login', goto=AsyncMock())
    context = FakeContext([page])
    context.add_cookies = AsyncMock()
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    launch = AsyncMock(return_value=browser)
    monkeypatch.setattr(login, 'launch_async', launch)
    flow = login.LoginFlow(db)
    async def cancel_after_entry(*args):
        session.cancelled = True
        return False
    monkeypatch.setattr(flow, '_open_oauth', cancel_after_entry)
    session = login.LoginSession('agentrouter', 'linuxdo', 'fixture', i)
    await flow._run_window(session)
    context.add_cookies.assert_awaited_once_with([upstream])
    assert launch.call_args.kwargs['headless'] is False
    assert db.credentials(db.account(i))['session'] == 'old'


@pytest.mark.asyncio
@pytest.mark.parametrize('http_status', [403, 429])
async def test_upstream_denial_keeps_cached_session_and_reports_http_cause(db, monkeypatch, http_status):
    import backend.login_flow as login
    cache = [{'name': 'upstream', 'value': 'cached', 'domain': '.linux.do', 'path': '/'}]
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='old-site', api_user='7',
                               login_via='linuxdo', oauth_cookies=cache))
    page = SimpleNamespace(url='https://agentrouter.org/login', goto=AsyncMock())
    upstream = SimpleNamespace(url='https://connect.linux.do/oauth2/authorize?state=private-state')
    context = FakeContext([page])
    context.add_cookies = AsyncMock()
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    monkeypatch.setattr(login, 'launch_async', AsyncMock(return_value=browser))
    flow = login.LoginFlow(db)
    async def oauth(*args):
        context.pages.append(upstream)
        response = SimpleNamespace(url=upstream.url, status=http_status,
                                   request=SimpleNamespace(resource_type='document'))
        for callback in context.listeners['response']:
            callback(response)
        return True
    monkeypatch.setattr(flow, '_open_oauth', oauth)
    result = await flow.authenticate_for_checkin(db.account(i), '7')
    assert result['error_kind'] == 'upstream_blocked'
    assert f'HTTP {http_status}' in result['message']
    assert '未判定凭据失效' in result['message']
    assert 'private-state' not in json.dumps(result)
    assert db.credentials(db.account(i))['oauth_cookies'] == cache
    assert db.credentials(db.account(i))['session'] == 'old-site'
    assert flow.sessions == {}
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('visible,expected', [(True, True), (False, False)])
async def test_only_visible_upstream_login_form_indicates_reauthentication(db, visible, expected):
    from unittest.mock import Mock
    from backend.login_flow import LoginFlow, LoginSession
    password = SimpleNamespace(first=SimpleNamespace(is_visible=AsyncMock(return_value=visible)))
    page = SimpleNamespace(url='https://linux.do/login', locator=Mock(return_value=password))
    session = LoginSession('agentrouter', 'linuxdo', 'fixture')
    result = await LoginFlow(db)._background_auth_blocked(session, SimpleNamespace(pages=[page]), {})
    assert result is expected
    if expected:
        assert session.stage == 'login_required'
    else:
        assert session.stage != 'login_required'


@pytest.mark.asyncio
async def test_engine_preserves_upstream_block_diagnostic_without_replacing_credentials(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='saved', api_user='7',
                               oauth_cookies=[{'name': 'upstream', 'value': 'cached'}]))
    engine_responses([profile_response()])
    failure = {'error_kind': 'upstream_blocked',
               'message': 'connect.linux.do 授权页面返回 HTTP 403；未判定凭据失效。'}
    result = await CheckinEngine(AsyncMock(return_value=failure)).run(db.account(i), db.credentials(db.account(i)))
    assert result['status'] == 'manual_required'
    assert 'HTTP 403' in result['message']
    assert result['after_balance'] == 100
    assert result['reward'] is None
    assert '_session_update' not in result


@pytest.mark.asyncio
async def test_repeated_confirmed_login_reports_zero_when_fresh_totals_are_unchanged(db, engine_responses):
    from backend.engine import CheckinEngine
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='saved', api_user='7',
                               oauth_cookies=[{'name': 'upstream', 'value': 'cached'}]))
    engine_responses([profile_response(50000000, 12500000)])
    login = AsyncMock(return_value={'session': 'new', 'profile': {
        'id': 7, 'quota': 49000000, 'used_quota': 13500000, 'checked_in': True}})
    result = await CheckinEngine(login).run(db.account(i), db.credentials(db.account(i)))
    assert result['status'] == 'success'
    assert result['reward'] == 0
    assert result['after_balance'] == 98
    assert '$0.00' in result['message']


@pytest.mark.asyncio
async def test_linuxdo_repeated_checkin_reuses_normal_browser_and_keeps_upstream_state(db, monkeypatch):
    import backend.login_flow as login
    upstream = {'name': '_t', 'value': 'cached', 'domain': '.linux.do', 'path': '/'}
    i = db.add_account(account(provider='agentrouter', auth_mode='session', session='old', api_user='7',
                               login_via='linuxdo', oauth_cookies=[upstream]))
    provider = login.PROVIDERS['agentrouter']
    page = SimpleNamespace(url=provider.domain + '/login', goto=AsyncMock(), evaluate=AsyncMock(), is_closed=lambda: False)
    context = FakeContext([page], cookies=AsyncMock(return_value=[upstream, {
        'name': 'session', 'value': 'fresh', 'domain': 'agentrouter.org', 'path': '/'}]))
    context.add_cookies = AsyncMock()
    context.clear_cookies = AsyncMock()
    page.context = context
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock(), is_connected=lambda: True)
    launch = AsyncMock(return_value=browser)
    monkeypatch.setattr(login, 'launch_async', launch)
    monkeypatch.setattr(login, 'verify_target_profile', AsyncMock(return_value={
        'id': 7, 'quota': 60000000, 'used_quota': 0}))
    flow = login.LoginFlow(db)
    path = provider.domain + '/api/oauth/linuxdo?code=fixture&state=fixture&mode=login'
    async def oauth(*args):
        for callback in list(context.listeners['response']):
            callback(FakeResponse(path, FakeRequest(path, page, frame_url=path),
                body={'success': True, 'data': {'id': 7, 'quota': 0, 'used_quota': 0, 'checked_in': True}},
                set_cookie=['session=fresh; Path=/']))
        await settle()
        return True
    monkeypatch.setattr(flow, '_open_oauth', oauth)
    for _ in range(2):
        result = await flow.authenticate_for_checkin(db.account(i), '7')
        assert result['profile']['quota'] == 60000000
        assert context.listeners['response'] == []
    launch.assert_awaited_once()
    assert launch.call_args.kwargs['headless'] is False
    assert '--start-minimized' in launch.call_args.kwargs['args']
    context.add_cookies.assert_awaited_once_with([upstream])
    context.clear_cookies.assert_awaited_once()
    clear = context.clear_cookies.call_args.kwargs
    assert clear['name'] == 'session'
    assert clear['domain'].fullmatch('.agentrouter.org')
    assert not clear['domain'].fullmatch('.linux.do')
    browser.close.assert_not_awaited()
    await flow.discard_browser_sessions(i)
    assert flow.browser_sessions == {}
    browser.close.assert_awaited_once()
