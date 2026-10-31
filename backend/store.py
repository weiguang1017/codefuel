"""Encrypted credentials and persistent local state."""
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from .paths import DB_NAME, data_dir

DATA_DIR = data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)
except OSError:
    pass


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or utcnow()).isoformat(timespec='seconds')


class Store:
    def __init__(self, db_path=None, key_path=None):
        self.db_path = Path(db_path or DATA_DIR / DB_NAME)
        self.key_path = Path(key_path or DATA_DIR / 'secret.key')
        self.lock = threading.RLock()
        if not self.key_path.exists():
            key = Fernet.generate_key()
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(key)
        elif os.name != 'nt' and self.key_path.stat().st_mode & 0o077:
            raise RuntimeError('数据目录的密钥权限过宽，请将该文件权限设为 600。')
        self.fernet = Fernet(self.key_path.read_bytes())
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self._migrate()
        if os.name != 'nt':
            os.chmod(self.db_path, 0o600)

    def _migrate(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            provider TEXT NOT NULL, auth_mode TEXT NOT NULL,
            encrypted TEXT NOT NULL, enabled INTEGER DEFAULT 1,
            status TEXT DEFAULT 'idle', last_run TEXT, next_run TEXT,
            last_message TEXT DEFAULT '等待首次签到', balance REAL, used_quota REAL,
            total_quota REAL, has_snapshot INTEGER DEFAULT 0,
            interval_hours INTEGER DEFAULT 6, created_at TEXT NOT NULL,
            login_via TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER,
            account_name TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL,
            trigger_type TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            before_balance REAL, after_balance REAL, reward REAL,
            message TEXT, duration_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(accounts)').fetchall()}
        if 'login_via' not in columns:
            self.db.execute('ALTER TABLE accounts ADD COLUMN login_via TEXT')
        if not self.db.execute('SELECT 1 FROM settings WHERE key="scheduler"').fetchone():
            self.db.execute('INSERT INTO settings VALUES("scheduler",?)', (json.dumps({'enabled': True, 'interval_hours': 6}),))
        self.db.commit()

    def close(self):
        self.db.close()

    def recover_interrupted(self):
        """Mark unfinished work honestly on restart; never claim it completed."""
        with self.lock:
            interrupted = self.db.execute("SELECT id,account_id FROM runs WHERE status='running'").fetchall()
            now = utcnow()
            for run in interrupted:
                self.db.execute("UPDATE runs SET status='interrupted',finished_at=?,message='程序退出，任务未完成。' WHERE id=?", (iso(now), run['id']))
                row = self.account(run['account_id'])
                if row:
                    hours = row['interval_hours']
                    self.patch(row['id'], status='interrupted', last_message='上次任务被中断，等待下次调度。',
                               next_run=iso(now + timedelta(hours=hours)))
            self.db.commit()
            return len(interrupted)

    def settings(self):
        row = self.db.execute('SELECT value FROM settings WHERE key="scheduler"').fetchone()
        data = json.loads(row['value'])
        return {'scheduler_enabled': data['enabled'], 'interval_hours': data['interval_hours']}

    def update_settings(self, enabled, hours):
        with self.lock:
            self.db.execute('UPDATE settings SET value=? WHERE key="scheduler"', (json.dumps({'enabled': enabled, 'interval_hours': hours}),))
            self.db.execute('UPDATE accounts SET interval_hours=?, next_run=?', (hours, iso(utcnow() + timedelta(hours=hours))))
            self.db.commit()

    def encrypt_credentials(self, data):
        return self.fernet.encrypt(json.dumps(data).encode()).decode()

    def credentials(self, row):
        try:
            return json.loads(self.fernet.decrypt(row['encrypted']).decode())
        except (InvalidToken, ValueError):
            raise RuntimeError('凭据无法解密，请确认原 secret.key 与数据库放在一起。')

    def public_account(self, row):
        data = {k: row[k] for k in ('id', 'name', 'provider', 'auth_mode', 'enabled', 'status',
                'last_run', 'next_run', 'last_message', 'balance', 'used_quota', 'interval_hours')}
        data['enabled'] = bool(row['enabled'])
        data['login_via'] = row['login_via'] if 'login_via' in row.keys() else None
        return data

    def accounts(self, enabled_only=False):
        sql = 'SELECT * FROM accounts' + (' WHERE enabled=1' if enabled_only else '') + ' ORDER BY id'
        return self.db.execute(sql).fetchall()

    def account(self, account_id):
        return self.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()

    def add_account(self, data):
        encrypted = self.encrypt_credentials({k: data[k] for k in ('email', 'password', 'session', 'api_user') if data.get(k)})
        now = utcnow()
        with self.lock:
            cur = self.db.execute('INSERT INTO accounts (name,provider,auth_mode,encrypted,enabled,interval_hours,next_run,created_at,login_via) VALUES(?,?,?,?,?,?,?,?,?)',
                (data['name'], data['provider'], data['auth_mode'], encrypted, int(data.get('enabled', True)),
                 data['interval_hours'], iso(now + timedelta(hours=data['interval_hours'])), iso(now), data.get('login_via')))
            self.db.commit()
            return cur.lastrowid

    def apply_login(self, account_id, session, api_user, login_via, balance=None, used_quota=None):
        """Store a freshly authorized browser session without exposing it anywhere else."""
        with self.lock:
            if not self.account(account_id):
                return False
            updates = {'auth_mode': 'session',
                       'encrypted': self.encrypt_credentials({'session': session, 'api_user': str(api_user)}),
                       'login_via': login_via, 'status': 'idle', 'last_message': '登录状态已更新'}
            if balance is not None:
                updates.update(balance=balance, used_quota=used_quota, has_snapshot=1,
                               total_quota=round(balance + (used_quota or 0), 2))
            self.patch(account_id, **updates)
            return True

    def update_account(self, account_id, data):
        row = self.account(account_id)
        if not row:
            return False
        old = self.credentials(row)
        for key in ('email', 'password', 'session', 'api_user'):
            if data.get(key):
                old[key] = data[key]
        if data.get('auth_mode') == 'session':
            old = {'session': old['session'], 'api_user': old['api_user']}
        elif data.get('auth_mode') == 'password':
            old = {k: old[k] for k in ('email', 'password') if old.get(k)}
        login_via = data.get('login_via', row['login_via'])
        if data.get('auth_mode') == 'password':
            login_via = None
        with self.lock:
            self.db.execute('UPDATE accounts SET name=?,provider=?,auth_mode=?,encrypted=?,enabled=?,interval_hours=?,next_run=?,login_via=? WHERE id=?',
                (data['name'], data['provider'], data['auth_mode'], self.encrypt_credentials(old), int(data['enabled']),
                 data['interval_hours'], iso(utcnow() + timedelta(hours=data['interval_hours'])), login_via, account_id))
            self.db.commit()
            return True

    def delete_account(self, account_id):
        with self.lock:
            cur = self.db.execute('DELETE FROM accounts WHERE id=? AND status NOT IN ("running","queued")', (account_id,))
            self.db.commit()
            return cur.rowcount > 0

    def patch(self, account_id, **fields):
        with self.lock:
            self.db.execute(f'UPDATE accounts SET {",".join(k+"=?" for k in fields)} WHERE id=?', (*fields.values(), account_id))
            self.db.commit()

    def begin_run(self, row, trigger):
        with self.lock:
            self.patch(row['id'], status='running', last_message='正在验证账号并执行签到…')
            cur = self.db.execute('INSERT INTO runs (account_id,account_name,provider,status,trigger_type,started_at) VALUES(?,?,?,?,?,?)',
                (row['id'], row['name'], row['provider'], 'running', trigger, iso()))
            self.db.commit()
            return cur.lastrowid

    def finish_run(self, run_id, account_id, result, trigger):
        with self.lock:
            self.db.execute('UPDATE runs SET status=?,finished_at=?,before_balance=?,after_balance=?,reward=?,message=?,duration_ms=? WHERE id=?',
                (result['status'], iso(), result.get('before_balance'), result.get('after_balance'), result.get('reward'), result['message'], result.get('duration_ms', 0), run_id))
            hours = self.account(account_id)['interval_hours'] if self.account(account_id) else 6
            updates = {'status': result['status'], 'last_run': iso(), 'last_message': result['message'],
                       'next_run': iso(utcnow() + timedelta(hours=hours))}
            if result.get('after_balance') is not None:
                updates.update(balance=result['after_balance'], used_quota=result.get('used_quota'),
                               total_quota=result.get('total_quota'), has_snapshot=1)
            self.patch(account_id, **updates)
            self.db.commit()

    def runs(self, limit=100):
        return [dict(r) | {'trigger': r['trigger_type']} for r in self.db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]

    def dashboard(self):
        accounts = [self.public_account(r) for r in self.accounts()]
        runs = self.runs()
        known = [a['balance'] for a in accounts if a['balance'] is not None]
        today = utcnow().date().isoformat()
        today_runs = [r for r in runs if r['started_at'].startswith(today)]
        trend = []
        for i in range(6, -1, -1):
            day = (utcnow() - timedelta(days=i)).date().isoformat()
            rs = [r for r in runs if r['started_at'].startswith(day)]
            trend.append({'date': day[5:], 'reward': round(sum(r['reward'] or 0 for r in rs), 2), 'runs': len(rs)})
        return {'accounts': accounts, 'runs': runs, 'settings': self.settings(),
                'stats': {'balance': round(sum(known), 2) if known else None,
                          'today_reward': round(sum(r['reward'] or 0 for r in today_runs), 2),
                          'today_completed': len([r for r in today_runs if r['status'] in ('success', 'already_checked', 'synced')]),
                          'total_accounts': len(accounts)}, 'trend': trend}
