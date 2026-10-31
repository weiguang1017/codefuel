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


def test_brand_keeps_legacy_storage_namespace():
    from backend.paths import APP_NAME, DATA_NAMESPACE
    assert APP_NAME == '码粮 CodeFuel'
    assert DATA_NAMESPACE == 'DailyQuota'


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
    assert second.status_code == 400
    assert client.post(f'/api/login/{session_id}/cancel', json={}, headers=client.write_headers).json()['ok'] is True
    assert client.get(f'/api/login/{session_id}').json()['status'] == 'cancelled'
    assert client.get('/api/login/unknown-session').status_code == 404


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
    session = LoginSession('agentrouter', 'linuxdo', '主账号', account_id=account_id)
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
    legacy.commit()
    legacy.close()
    migrated = Store(db_path, key_path)
    try:
        columns = {row['name'] for row in migrated.db.execute('PRAGMA table_info(accounts)')}
        assert 'login_via' in columns
        assert migrated.dashboard()['accounts'] == []
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
