"""使用临时浏览器窗口完成站点自身的授权，不保存上游身份会话。"""
import asyncio
import re
import secrets
import time
from urllib.parse import urljoin, urlparse

from cloakbrowser import launch_async

from .providers import PROVIDERS, BROWSER_METHODS, supports
from .upstream_imports import prepare_browser_page, wait_for_waf_ready

WINDOW_TIMEOUT = 300
OAUTH_LABELS = {'github': 'GitHub', 'linuxdo': 'LinuxDO'}
OAUTH_BUTTONS = {
    method: re.compile(r'^\s*(?:(?:使用|通过)\s*)?(?:(?:continue|sign\s*in|log\s*in|login)\s+with\s+)?'
                       + label + r'\s*(?:(?:继续|登录|授权|登入))?\s*$', re.IGNORECASE)
    for method, label in (('github', r'git\s*hub'), ('linuxdo', r'linux\s*do'))
}
OAUTH_ENTRY_TIMEOUT = 12
ACTIVE_STATUSES = ('starting', 'waiting', 'verifying')


class LoginConflict(RuntimeError):
    pass


def _origin(url):
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


async def verify_target_profile(page, provider):
    """只接受本次同源、禁缓存请求的结果，不使用响应监听器或旧用户信息。"""
    if _origin(page.url) != _origin(provider.domain):
        return None
    target = provider.domain + provider.user_info_path
    result = await page.evaluate('''async (o) => {
      const target = new URL(o.url);
      if (location.origin !== target.origin) return null;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      try {
        const headers = {'X-Requested-With': 'XMLHttpRequest'};
        let hint = null;
        try { hint = JSON.parse(localStorage.getItem('user')); } catch (e) {}
        if (hint && hint.id != null) headers[o.userHeader] = String(hint.id);
        target.searchParams.set('_codefuel_verify', o.nonce);
        const response = await fetch(target.href, {
          credentials: 'same-origin', cache: 'no-store', redirect: 'error',
          headers, signal: controller.signal
        });
        return {url: response.url, status: response.status, body: await response.json()};
      } catch (e) { return null; }
      finally { clearTimeout(timer); }
    }''', {'url': target, 'userHeader': provider.api_user_key, 'nonce': secrets.token_urlsafe(12)})
    if not isinstance(result, dict) or result.get('status') != 200:
        return None
    response_url = result.get('url', '')
    if (_origin(page.url) != _origin(provider.domain) or _origin(response_url) != _origin(target)
            or urlparse(response_url).path != urlparse(target).path):
        return None
    body = result.get('body')
    if not isinstance(body, dict) or body.get('success') is not True:
        return None
    profile = body.get('data')
    if not isinstance(profile, dict) or isinstance(profile.get('id'), bool):
        return None
    if not str(profile.get('id', '')).isdigit() or int(profile['id']) <= 0:
        return None
    return profile


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
        self.task = None


class LoginFlow:
    """每次只处理一个平台，账号写入与运行状态检查共享存储锁。"""

    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.active_id = None
        self.window_lock = asyncio.Lock()

    def is_authorizing(self, account_id):
        return any(s.account_id == account_id and s.status in ACTIVE_STATUSES
                   for s in self.sessions.values())

    def start(self, provider, method, name, account_id=None, interval_hours=6):
        if provider not in PROVIDERS:
            raise ValueError('未支持的平台')
        if method not in BROWSER_METHODS or not supports(provider, method):
            raise ValueError('未支持的登录方式')
        if method in OAUTH_LABELS and not name and account_id is None:
            raise ValueError('请先填写账号备注')
        with self.store.lock:
            current = self.sessions.get(self.active_id)
            if current and current.status in ACTIVE_STATUSES and time.time() - current.created >= WINDOW_TIMEOUT + 30:
                current.status, current.message = 'expired', '登录窗口已超时关闭。'
                if current.task:
                    current.task.cancel()
            if account_id is not None:
                row = self.store.account(account_id)
                if not row:
                    raise LookupError('账号不存在')
                if row['provider'] != provider:
                    raise LoginConflict('重新授权的平台必须与原账号一致')
                if row['status'] in ('queued', 'running') or self.is_authorizing(account_id):
                    raise LoginConflict('账号正在排队、运行或授权，暂不能重新授权')
            if current and current.status in ACTIVE_STATUSES:
                raise LoginConflict('已有登录窗口正在等待完成，请先完成或取消它。')
            session = LoginSession(provider, method, name, account_id, interval_hours)
            self.sessions[session.id] = session
            self.active_id = session.id
            self._prune()
            session.task = asyncio.create_task(self._run(session))
            return session

    def view(self, session_id):
        session = self.sessions.get(session_id)
        if not session:
            return None
        return {'id': session.id, 'provider': session.provider, 'method': session.method,
                'status': session.status, 'message': session.message,
                'account_id': session.account, 'elapsed': int(time.time() - session.created)}

    def cancel(self, session_id):
        with self.store.lock:
            session = self.sessions.get(session_id)
            if not session:
                return False
            if session.status in ACTIVE_STATUSES:
                session.cancelled = True
                session.status, session.message = 'cancelled', '已取消授权。'
                if session.task:
                    session.task.get_loop().call_soon_threadsafe(session.task.cancel)
            return True

    async def close(self):
        tasks = []
        for session in self.sessions.values():
            if session.task and not session.task.done():
                self.cancel(session.id)
                tasks.append(session.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _prune(self):
        keep = sorted(self.sessions.values(), key=lambda s: s.created, reverse=True)[:12]
        pending = [s for s in self.sessions.values() if s.task and not s.task.done()]
        self.sessions = {s.id: s for s in keep + pending}

    async def _run(self, session):
        async with self.window_lock:
            await self._run_window(session)

    async def _run_window(self, session):
        provider = PROVIDERS[session.provider]
        browser = None
        try:
            if session.cancelled or session.status not in ACTIVE_STATUSES:
                return
            browser = await asyncio.wait_for(launch_async(headless=False), timeout=90)
            context = await browser.new_context(viewport=None)
            page = await context.new_page()
            await prepare_browser_page(page)
            await page.goto(provider.domain + provider.login_path, wait_until='domcontentloaded', timeout=60000)
            await wait_for_waf_ready(page)
            if session.cancelled:
                return
            session.status = 'waiting'
            if session.method in OAUTH_LABELS:
                label = OAUTH_LABELS[session.method]
                if _origin(page.url) != _origin(provider.domain):
                    session.status, session.message = 'failed', '未能打开目标平台登录页，授权未保存。'
                    return
                clicked = await self._click_oauth(context, page, session.method, provider)
                if session.cancelled:
                    return
                if not clicked:
                    session.status = 'unavailable'
                    session.message = f'该平台登录页未提供可用的 {label} 授权入口；未保存账号。可另行选择手动登录。'
                    return
                session.message = f'请在打开的 {label} 窗口中完成授权；完成后方可关闭窗口。'
            else:
                session.message = '请在打开的浏览器窗口中完成登录，登录成功后会自动保存。'
            deadline = time.monotonic() + WINDOW_TIMEOUT
            while time.monotonic() < deadline:
                if session.cancelled or session.status not in ACTIVE_STATUSES:
                    return
                cookie = await self._session_cookie(context, provider)
                target_pages = [p for p in context.pages if _origin(p.url) == _origin(provider.domain)]
                if cookie and target_pages:
                    session.status = 'verifying'
                    session.message = '已检测到登录状态，正在校验账号…'
                    profile = await verify_target_profile(target_pages[-1], provider)
                    if profile and cookie == await self._session_cookie(context, provider):
                        await self._persist(session, profile, cookie)
                        return
                    if session.cancelled or session.status not in ACTIVE_STATUSES:
                        return
                    session.status = 'waiting'
                    session.message = '已登录但账号校验未通过，请在窗口中打开目标平台控制台后稍候。'
                await asyncio.sleep(1.5)
            session.status, session.message = 'expired', '等待超时，请重新发起授权。'
        except asyncio.CancelledError:
            if session.status in ACTIVE_STATUSES:
                session.status, session.message = 'cancelled', '已取消授权。'
            raise
        except Exception:
            if session.status in ACTIVE_STATUSES:
                session.status = 'failed'
                session.message = '登录窗口未能完成，请重试，或改用账号密码 / Session 方式。'
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _click_oauth(self, context, page, method, provider):
        """有界等待站点自身入口；点击一次后交还站点完成跳转。"""
        target_origin = _origin(provider.domain)
        deadline = time.monotonic() + OAUTH_ENTRY_TIMEOUT
        while time.monotonic() < deadline:
            if _origin(page.url) != target_origin:
                return False
            for role in ('button', 'link'):
                locator = page.get_by_role(role, name=OAUTH_BUTTONS[method])
                for index in range(await locator.count()):
                    candidate = locator.nth(index)
                    if not await candidate.is_visible() or not await candidate.is_enabled():
                        continue
                    if role == 'link':
                        href = await candidate.get_attribute('href')
                        target = urlparse(urljoin(page.url, href or ''))
                        path = target.path.lower().rstrip('/')
                        # 排除同名首页、项目及帮助链接，只接受站点入口或标准授权端点。
                        site_entry = (_origin(target.geturl()) == target_origin
                                      and path in (f'/api/oauth/{method}', f'/oauth/{method}',
                                                   f'/auth/{method}', f'/api/auth/{method}'))
                        upstream_entry = (target.scheme == 'https' and (
                            (method == 'github' and target.hostname == 'github.com' and path == '/login/oauth/authorize')
                            or (method == 'linuxdo' and target.hostname == 'connect.linux.do' and path == '/oauth2/authorize')))
                        if not site_entry and not upstream_entry:
                            continue
                    if _origin(page.url) != target_origin:
                        return False
                    await candidate.click(timeout=6000)
                    return True
            await asyncio.sleep(0.25)
        return False

    async def _session_cookie(self, context, provider):
        target = provider.domain + provider.user_info_path
        host = urlparse(target).hostname
        matches = []
        for cookie in await context.cookies([target]):
            if cookie.get('name') != 'session' or not cookie.get('value'):
                continue
            domain = (cookie.get('domain') or '').lower()
            bare_domain = domain.lstrip('.')
            if not bare_domain:
                continue
            if domain.startswith('.'):
                valid_domain = host == bare_domain or host.endswith('.' + bare_domain)
            else:
                valid_domain = host == bare_domain
            if valid_domain:
                matches.append(cookie)
        if not matches:
            return None
        matches.sort(key=lambda c: len(c.get('path', '/')), reverse=True)
        return matches[0]['value']

    async def _persist(self, session, profile, session_cookie):
        with self.store.lock:
            if session.cancelled or session.status not in ACTIVE_STATUSES:
                return
            if not session_cookie:
                session.status, session.message = 'error', '未能读取登录凭据，请重新发起授权。'
                return
            name = session.name or str(profile.get('username') or OAUTH_LABELS.get(session.method, '账号'))
            quota = round((profile.get('quota') or 0) / 500000, 2)
            used = round((profile.get('used_quota') or 0) / 500000, 2)
            login_via = session.method if session.method in OAUTH_LABELS else 'manual'
            if session.account_id is not None:
                try:
                    updated = self.store.apply_login(session.account_id, session_cookie, profile['id'], login_via,
                                                     quota, used, provider=session.provider,
                                                     name=session.name, interval_hours=session.interval_hours)
                except ValueError as error:
                    session.status, session.message = 'failed', str(error)
                    return
                if not updated:
                    session.status, session.message = 'failed', '账号已不存在，登录状态未保存。'
                    return
                session.account = session.account_id
                name = self.store.account(session.account_id)['name']
            else:
                session.account = self.store.add_account({
                    'name': name[:40], 'provider': session.provider, 'auth_mode': 'session',
                    'interval_hours': session.interval_hours, 'enabled': True,
                    'session': session_cookie, 'api_user': str(profile['id']), 'login_via': login_via,
                })
                self.store.patch(session.account, balance=quota, used_quota=used, has_snapshot=1,
                                 total_quota=round(quota + used, 2), last_message='已通过浏览器授权关联账号')
            session.status, session.message = 'success', f'已保存「{name}」的登录状态。'
