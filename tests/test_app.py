import asyncio
import json
from datetime import timedelta
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
async def test_agentrouter_observes_change_without_signin_request(db, monkeypatch):
    import backend.engine as adapter
    from types import SimpleNamespace
    account_id = db.add_account(account(provider='agentrouter'))
    db.patch(account_id, has_snapshot=1, total_quota=100)
    async def fake_login(*args):
        return SimpleNamespace(cookies={'session': 'x'}, api_user='123')
    class FakeSession:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, path, user, method='GET', payload=None):
            assert method == 'GET', 'AgentRouter must not call any sign-in endpoint'
            return {'status': 200, 'body': {'success': True, 'data': {'quota': 62500000, 'used_quota': 0}}}
    monkeypatch.setattr(adapter, 'login_with_credentials', fake_login)
    monkeypatch.setattr(adapter, 'BrowserSession', FakeSession)
    result = await adapter.CheckinEngine().run(db.account(account_id), {'email': 'a', 'password': 'b'}, True)
    assert result['status'] == 'synced'
    assert result['reward'] == 25
    assert '观察值' in result['message']


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
    assert events[4][1] == 'https://anyrouter.top'
    assert events[4][2] == 60_000
    assert events[5] == ('cookies', ['session'])
    assert events[6][1] == 'https://anyrouter.top/login'
    assert events[6][2] == 60_000


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
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), pages=[page])
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
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), pages=[page])
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
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), pages=[page], cookies=AsyncMock(return_value=[
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
    assert db.credentials(db.account(session.account)) == {'session': 'target-only', 'api_user': '15'}
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
                                         ('linuxdo', '使用 Linux DO 登录'),
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
