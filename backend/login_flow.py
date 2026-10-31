"""Visible-browser authorization for GitHub / LinuxDO sign-in.

The site's own OAuth flow is driven in a real window: nothing is forged, no
captcha is bypassed, and no credentials are typed on the user's behalf.
"""
import asyncio
import secrets
import time
from urllib.parse import urlparse

from cloakbrowser import launch_async

from .engine import PROVIDERS
from .upstream_imports import has_session_cookie, prepare_browser_page, verify_browser_login, wait_for_waf_ready

WINDOW_TIMEOUT = 300
OAUTH_LABELS = {'github': 'GitHub', 'linuxdo': 'LinuxDO'}
OAUTH_BUTTONS = {
    'github': ('使用 GitHub 继续', '使用GitHub继续', 'Continue with GitHub', 'Sign in with GitHub', 'GitHub'),
    'linuxdo': ('使用 LinuxDO 继续', '使用LinuxDO继续', 'Continue with LinuxDO', 'Sign in with LinuxDO', 'LinuxDO'),
}
ACTIVE_STATUSES = ('starting', 'waiting', 'verifying')


class LoginSession:
    def __init__(self, provider, method, name, account_id=None, interval_hours=6):
        self.id = secrets.token_urlsafe(9)
        self.provider = provider
        self.method = method
        self.name = name
        self.account_id = account_id
        self.interval_hours = interval_hours
        self.status = 'starting'
        self.message = '正在打开登录窗口…'
        self.created = time.time()
        self.account = account_id
        self.cancelled = False


class LoginFlow:
    """One visible authorization window at a time; sessions are cancelled on demand."""

    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.active_id = None

    def start(self, provider, method, name, account_id=None, interval_hours=6):
        if provider not in PROVIDERS:
            raise ValueError('未支持的平台')
        if method not in ('github', 'linuxdo', 'manual'):
            raise ValueError('未支持的登录方式')
        if method in OAUTH_LABELS and not name and not account_id:
            raise ValueError('请先填写账号备注')
        if self.active_id in self.sessions:
            current = self.sessions[self.active_id]
            if current.status in ACTIVE_STATUSES:
                if time.time() - current.created < WINDOW_TIMEOUT + 30:
                    raise RuntimeError('已有登录窗口正在等待完成，请先完成或取消它。')
                # A window that never reported back must not block the app forever.
                current.status, current.message = 'expired', '登录窗口已超时关闭。'
        session = LoginSession(provider, method, name, account_id, interval_hours)
        self.sessions[session.id] = session
        self.active_id = session.id
        self._prune()
        asyncio.create_task(self._run(session))
        return session

    def view(self, session_id):
        session = self.sessions.get(session_id)
        if not session:
            return None
        return {'id': session.id, 'provider': session.provider, 'method': session.method,
                'status': session.status, 'message': session.message,
                'account_id': session.account, 'elapsed': int(time.time() - session.created)}

    def cancel(self, session_id):
        session = self.sessions.get(session_id)
        if not session:
            return False
        session.cancelled = True
        if session.status in ACTIVE_STATUSES:
            session.status = 'cancelled'
            session.message = '已取消授权。'
        return True

    def _prune(self):
        keep = sorted(self.sessions.values(), key=lambda s: s.created, reverse=True)[:12]
        self.sessions = {s.id: s for s in keep}

    async def _run(self, session):
        provider = PROVIDERS[session.provider]
        browser = None
        try:
            browser = await asyncio.wait_for(launch_async(headless=False), timeout=90)
            context = await browser.new_context(viewport=None)
            page = await context.new_page()
            await prepare_browser_page(page)
            await page.goto(provider.domain + provider.login_path, wait_until='domcontentloaded', timeout=60000)
            await wait_for_waf_ready(page)
            if session.cancelled:
                session.status, session.message = 'cancelled', '已取消授权。'
                return
            session.status = 'waiting'
            if session.method in OAUTH_LABELS:
                label = OAUTH_LABELS[session.method]
                clicked = await self._click_oauth(context, page, session.method)
                session.message = (
                    f'请在打开的 {label} 窗口中完成授权；完成后方可关闭窗口。' if clicked else
                    f'当前站点未提供 {label} 一键授权，已打开登录页：请直接在该窗口登录，'
                    '授权完成时会自动保存登录状态。'
                )
            else:
                session.message = '请在打开的浏览器窗口中完成登录，登录成功后会自动保存。'
            deadline = time.monotonic() + WINDOW_TIMEOUT
            while time.monotonic() < deadline:
                if session.cancelled:
                    session.status, session.message = 'cancelled', '已取消授权。'
                    return
                try:
                    logged_in = await has_session_cookie(page)
                except Exception:
                    session.status = 'error'
                    session.message = '登录窗口已关闭，本次授权未完成。'
                    return
                if logged_in:
                    session.status = 'verifying'
                    session.message = '已检测到登录状态，正在校验账号…'
                    profile = await verify_browser_login(page, f'{provider.domain}/console', 45000)
                    if profile and profile.get('id') is not None:
                        await self._persist(session, profile, await self._session_cookie(context, provider))
                        return
                    session.message = f'已登录但账号校验未通过，请在窗口中打开控制台后稍候。'
                await asyncio.sleep(1.5)
            session.status = 'expired'
            session.message = '等待超时，请重新发起授权。'
        except asyncio.CancelledError:
            session.status, session.message = 'cancelled', '已取消授权。'
            raise
        except Exception:
            if session.status in ACTIVE_STATUSES:
                session.status = 'error'
                session.message = '登录窗口未能完成，请重试，或改用账号密码 / Session 方式。'
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _click_oauth(self, context, page, method):
        """Prefer the site's own button, so its state/nonce handling stays intact."""
        host = urlparse(page.url).hostname
        for text in OAUTH_BUTTONS[method]:
            locator = page.get_by_role('button', name=text)
            try:
                if not await locator.count():
                    continue
                pages_before = len(context.pages)
                await locator.first.click(timeout=6000)
                await page.wait_for_timeout(2500)
                if len(context.pages) > pages_before:
                    popup = context.pages[-1]
                    await popup.wait_for_load_state('domcontentloaded', timeout=30000)
                    return True
                if urlparse(page.url).hostname != host:
                    return True
            except Exception:
                continue
        return False

    async def _session_cookie(self, context, provider):
        host = urlparse(provider.domain).hostname
        for cookie in await context.cookies():
            if cookie.get('name') == 'session' and cookie.get('value'):
                domain = (cookie.get('domain') or '').lstrip('.')
                if not domain or host.endswith(domain):
                    return cookie['value']
        return None

    async def _persist(self, session, profile, session_cookie):
        if not session_cookie:
            session.status = 'error'
            session.message = '未能读取登录凭据，请重新发起授权。'
            return
        name = session.name or str(profile.get('username') or OAUTH_LABELS.get(session.method, '账号'))
        quota = round((profile.get('quota') or 0) / 500000, 2)
        used = round((profile.get('used_quota') or 0) / 500000, 2)
        login_via = session.method if session.method in OAUTH_LABELS else 'manual'
        if session.account_id:
            updated = self.store.apply_login(session.account_id, session_cookie, profile['id'], login_via, quota, used)
            if not updated:
                session.status = 'error'
                session.message = '账号已不存在，登录状态未保存。'
                return
            session.account = session.account_id
        else:
            session.account = self.store.add_account({
                'name': name[:40], 'provider': session.provider, 'auth_mode': 'session',
                'interval_hours': session.interval_hours, 'enabled': True,
                'session': session_cookie, 'api_user': str(profile['id']), 'login_via': login_via,
            })
            self.store.patch(session.account, balance=quota, used_quota=used, has_snapshot=1,
                             total_quota=round(quota + used, 2), last_message='已通过浏览器授权关联账号')
        session.status = 'success'
        session.message = f'已保存「{name}」的登录状态。'
