"""Reproduce verification and balance failures with real local browser responses."""
import asyncio
import json
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from backend.engine import LOCAL_CHROME
from backend.login_flow import LoginFlow
from backend.providers import PROVIDERS
from backend.store import Store


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            path = urlsplit(self.path).path
            status, kind, cookie = 200, 'text/html; charset=utf-8', None
            if path == '/login':
                if self.server.callback_mode:
                    body = '''<button onclick="window.open('/oauth/github')">GitHub</button>
                        <script>localStorage.setItem('status',JSON.stringify({github_client_id:'fixture'}))</script>'''
                else:
                    cookie = 'session=fixture-session; Path=/; HttpOnly'
                    followup = ".then(()=>fetch('/api/user/self'))" if self.server.signal_profile else ''
                    body = '<button onclick="fetch(\'/fixture/verified\')' + followup + '">完成测试验证</button>'
            elif path == '/fixture/verified':
                self.server.verified = True
                body = 'fixture verification completed'
            elif path == '/oauth/github':
                body = '<script>fetch("/api/oauth/github?code=fixture&state=fixture")</script>'
            elif path == '/api/oauth/github':
                kind, cookie = 'application/json', 'session=fixture-session; Path=/; HttpOnly'
                body = json.dumps({'success': True, 'data': {'id': 7, 'checked_in': True, 'quota': 0, 'used_quota': 0}})
            elif path == '/api/user/self':
                self.server.reads.append(time.monotonic())
                if self.server.verified:
                    kind = 'application/json'
                    body = json.dumps({'success': True, 'data': {'id': 7, 'quota': 60000000, 'used_quota': 5000000}})
                else:
                    status, body = 403, '<h1>测试验证尚未完成</h1>'
            else:
                status, body = 404, ''
            data = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            if cookie: self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.callback_mode, server.signal_profile, server.verified = False, True, False
    server.reads = []
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_id', ['anyrouter', 'agentrouter'])
@pytest.mark.parametrize('signal_profile', [True, False])
async def test_verification_completion_is_detected_without_manual_resume(site, tmp_path, monkeypatch, provider_id, signal_profile):
    import backend.login_flow as login
    if not LOCAL_CHROME: pytest.skip('Local Chrome is required')
    site.signal_profile = signal_profile
    monkeypatch.setitem(PROVIDERS, provider_id, replace(PROVIDERS[provider_id], domain=f'http://127.0.0.1:{site.server_port}', use_proxy=False))
    monkeypatch.setattr(login, 'VERIFICATION_RECHECK_DELAYS', (.3, .5, .8))
    db = Store(tmp_path / 'accounts.db', tmp_path / 'key')
    flow = LoginFlow(db)
    browsers = []
    async with async_playwright() as p:
        async def launch(**kwargs):
            browser = await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
            browsers.append(browser)
            return browser
        monkeypatch.setattr(login, 'launch_async', launch)
        try:
            session = flow.start(provider_id, 'manual', 'fixture')
            async def waiting():
                while not session.verification_auto and not session.task.done():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(waiting(), 20)
            assert session.verification_auto, (
                f'authorization ended before verification polling started: '
                f'status={session.status}, stage={session.stage}')
            assert db.accounts() == []
            assert session.verification_paused
            page = browsers[0].contexts[0].pages[0]
            await page.get_by_role('button', name='完成测试验证').click()
            await asyncio.wait_for(session.task, 15)
            assert session.status == 'success'
            row = db.account(session.account)
            assert row['balance'] == 120
            assert db.credentials(row)['session'] == 'fixture-session'
            assert len(db.accounts()) == 1
            assert not session.verification_auto
            assert not browsers[0].is_connected()
        finally:
            await flow.close()
            db.close()


@pytest.mark.asyncio
async def test_confirmed_oauth_is_saved_when_balance_remains_blocked(site, tmp_path, monkeypatch):
    import backend.login_flow as login
    if not LOCAL_CHROME: pytest.skip('Local Chrome is required')
    site.callback_mode = True
    monkeypatch.setitem(PROVIDERS, 'agentrouter', replace(PROVIDERS['agentrouter'], domain=f'http://127.0.0.1:{site.server_port}', use_proxy=False))
    db = Store(tmp_path / 'accounts.db', tmp_path / 'key')
    flow = LoginFlow(db)
    async with async_playwright() as p:
        async def launch(**kwargs):
            return await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        monkeypatch.setattr(login, 'launch_async', launch)
        try:
            session = flow.start('agentrouter', 'github', 'fixture')
            await asyncio.wait_for(session.task, 8)
            assert session.status == 'success'
            assert not site.verified
            row = db.account(session.account)
            assert row['balance'] is None and not row['has_snapshot']
            assert db.credentials(row)['api_user'] == '7'
            assert db.credentials(row)['session'] == 'fixture-session'
            assert not session.verification_paused
            assert len(site.reads) == 1
        finally:
            await flow.close()
            db.close()
