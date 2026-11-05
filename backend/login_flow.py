"""显式授权使用窗口；自动签到仅复用本地加密保存的会话。"""
import asyncio
import re
import secrets
import hashlib
import json
import time
from playwright.async_api import Error as PlaywrightError
from urllib.parse import urljoin, urlparse, parse_qs
from http.cookies import SimpleCookie

from cloakbrowser import launch_async

from .providers import PROVIDERS, BROWSER_METHODS, supports
from .quota import quota_snapshot
from .cookies import site_cookies
from .browser_identity import chrome_user_agent
from .proxy import normalize_proxy, browser_proxy, proxy_failure_message
from .popups import setup_notice_guard
from .retry import RETRY_DELAYS, transient_error, transient_status
from .upstream_imports import prepare_browser_page, wait_for_waf_ready, get_playwright_proxy

WINDOW_TIMEOUT = 900
OAUTH_LABELS = {'github': 'GitHub', 'linuxdo': 'LinuxDO'}
OAUTH_BUTTONS = {
    method: re.compile(r'^\s*(?:' + (r'github(?:[_\s-]*logo)?' if method == 'github' else r'linux\s*do(?:[_\s-]*logo)?')
                       + r'\s+)?(?:(?:使用|通过)\s*)?(?:(?:continue|sign\s*in|log\s*in|login)\s+with\s+)?'
                       + label + r'\s*(?:(?:继续|登录|授权|登入))?\s*$', re.IGNORECASE)
    for method, label in (('github', r'git\s*hub'), ('linuxdo', r'linux\s*do'))
}
OAUTH_ENTRY_TIMEOUT = 12
UPSTREAM_VERIFICATION_GRACE = 20
VERIFICATION_RECHECK_DELAYS = (8.0, 15.0, 30.0)
ACTIVE_STATUSES = ('starting', 'waiting', 'verifying')


class LoginConflict(RuntimeError):
    pass


class ProfileVerificationBlocked(RuntimeError):
    """A non-JSON or denied identity response needs user attention, not polling."""
    pass


def _origin(url):
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


async def verify_target_profile(page, provider, api_user=None):
    """只接受本次同源、禁缓存请求的结果，不使用响应监听器或旧用户信息。"""
    if _origin(page.url) != _origin(provider.domain):
        return None
    target = provider.domain + provider.user_info_path
    result = await page.evaluate(r'''async (o) => {
      const target = new URL(o.url);
      if (location.origin !== target.origin) return null;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      try {
        const headers = {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'};
        let hint = null;
        try { hint = JSON.parse(localStorage.getItem('user')); } catch (e) {}
        if (o.user != null) headers[o.userHeader] = String(o.user);
        else if (hint && hint.id != null) headers[o.userHeader] = String(hint.id);
        target.searchParams.set('_codefuel_verify', o.nonce);
        const response = await fetch(target.href, {
          credentials: 'same-origin', cache: 'no-store', redirect: 'error',
          headers, signal: controller.signal
        });
        const contentType = response.headers.get('content-type') || '';
        if (!/application\/(?:[\w.-]+\+)?json\b/i.test(contentType)) {
          return {url: response.url, status: response.status, nonJson: true};
        }
        return {url: response.url, status: response.status, body: await response.json()};
      } catch (e) { return null; }
      finally { clearTimeout(timer); }
    }''', {'url': target, 'userHeader': provider.api_user_key, 'nonce': secrets.token_urlsafe(12), 'user': api_user})
    if not isinstance(result, dict):
        return None
    response_url = result.get('url', '')
    if (_origin(page.url) != _origin(provider.domain) or _origin(response_url) != _origin(target)
            or urlparse(response_url).path != urlparse(target).path):
        return None
    if result.get('nonJson') or result.get('status') in (403, 429):
        raise ProfileVerificationBlocked('账号校验接口返回非 JSON 或拒绝访问，已暂停自动校验。')
    if result.get('status') != 200:
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


class LoginEvidence:
    """Observe normal site responses inside this one authorization context.

    Never replay OAuth codes, inspect API-token endpoints, or trust DOM/storage as
    identity. Evidence is short-lived and tied to the exact site session cookie.
    """
    def __init__(self, context, provider, method, read_cookie, *, require_login_response=False):
        self.context, self.provider, self.method = context, provider, method
        self.read_cookie = read_cookie
        self.require_login_response = require_login_response
        self.changed = asyncio.Event()
        self.tasks = set()
        self.latest = None
        self.sequence = 0

    def start(self):
        self.context.on('response', self._on_response)

    def _on_response(self, response):
        path = urlparse(response.url).path
        if _origin(response.url) != _origin(self.provider.domain):
            return
        login_paths = {'/api/user/login'}
        if self.method in OAUTH_LABELS:
            login_paths.add(f'/api/oauth/{self.method}')
        elif self.method == 'manual':
            login_paths.update(f'/api/oauth/{method}' for method in OAUTH_LABELS)
        if self.require_login_response and path == self.provider.user_info_path:
            return
        if path not in login_paths | {self.provider.user_info_path}:
            return
        if path in login_paths:
            self.sequence += 1
            self.latest = None
        sequence = self.sequence
        task = asyncio.create_task(self._capture(response, sequence, path in login_paths))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def invalidate(self):
        self.sequence += 1
        self.latest = None
        self.changed.set()

    async def _capture(self, response, sequence, is_login):
        try:
            request = response.request
            page = request.frame.page
            if (page.context is not self.context
                    or _origin(request.url) != _origin(self.provider.domain)
                    or _origin(request.frame.url) != _origin(self.provider.domain)
                    or response.status != 200 or response.from_service_worker):
                return
            if '/api/oauth/' in urlparse(request.url).path:
                query = parse_qs(urlparse(request.url).query)
                if not query.get('code') or not query.get('state') or query.get('mode', ['login']) != ['login']:
                    return
            headers = await response.all_headers()
            if not re.search(r'application/(?:[\w.-]+\+)?json\b', headers.get('content-type', ''), re.I):
                return
            # json() awaits the response body and raises on an aborted transfer.
            # A separate finished() call leaks a target-close waiter in this
            # bundled Playwright version when its finished future wins the race.
            body = await response.json()
            if not isinstance(body, dict) or body.get('success') is not True or body.get('message') == 'bind':
                return
            data = body.get('data')
            if (not isinstance(data, dict) or isinstance(data.get('id'), bool)
                    or not str(data.get('id', '')).isdigit() or int(data['id']) <= 0):
                return
            cookie = await self.read_cookie(self.context, self.provider)
            if not cookie:
                return
            # A login response must itself issue the matching target-site session.
            values = await response.header_values('set-cookie') if is_login else []
            if not is_login:
                values = [(await request.all_headers()).get('cookie', '')]
            matches = False
            for value in values:
                parsed = SimpleCookie()
                parsed.load(value)
                if 'session' in parsed and parsed['session'].value == cookie:
                    matches = True
            if not matches or sequence != self.sequence:
                return
            profile = {key: data[key] for key in ('id', 'username', 'quota', 'used_quota', 'checked_in') if key in data}
            if is_login:
                # Login responses contain placeholder zeros on AgentRouter. Only
                # /user/self is authoritative for money, even when login succeeds.
                profile.pop('quota', None)
                profile.pop('used_quota', None)
                profile['_login_response'] = True
            self.latest = (profile, cookie, time.monotonic(), page)
            self.changed.set()
        except Exception:
            # Only allowlisted structured fields are retained; never log raw auth responses.
            return

    async def get(self):
        evidence = self.latest
        if not evidence:
            return None
        profile, cookie, received, page = evidence
        if (time.monotonic() - received > 120 or page not in self.context.pages
                or _origin(page.url) != _origin(self.provider.domain)
                or cookie != await self.read_cookie(self.context, self.provider)):
            return None
        return profile, cookie

    async def close(self):
        self.context.remove_listener('response', self._on_response)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.latest = None


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
        self.stage = 'opening'
        self.verification_paused = False
        self.verification_auto = False
        self.resume_event = asyncio.Event()


class LoginFlow:
    """每次只处理一个平台，账号写入与运行状态检查共享存储锁。"""

    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.active_id = None
        self.window_lock = asyncio.Lock()
        self.browser_sessions = {}
        self.background_tasks = set()

    def is_authorizing(self, account_id):
        return any(s.account_id == account_id and s.status in ACTIVE_STATUSES
                   for s in self.sessions.values())

    def start(self, provider, method, name, account_id=None, interval_hours=6, *, proxy=None):
        if provider not in PROVIDERS:
            raise ValueError('未支持的平台')
        if method not in BROWSER_METHODS or not supports(provider, method):
            raise ValueError('未支持的登录方式')
        if method in OAUTH_LABELS and not name and account_id is None:
            raise ValueError('请先填写账号备注')
        with self.store.lock:
            previous_proxy = None
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
                previous_proxy = self.store.credentials(row).get('proxy')
            if current and current.status in ACTIVE_STATUSES:
                raise LoginConflict('已有登录窗口正在等待完成，请先完成或取消它。')
            session = LoginSession(provider, method, name, account_id, interval_hours)
            session.account_proxy = normalize_proxy(proxy, previous_proxy)
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
                'account_id': session.account, 'elapsed': int(time.time() - session.created),
                'stage': session.stage, 'verification_paused': session.verification_paused,
                'verification_auto': session.verification_auto}

    def resume_verification(self, session_id):
        session = self.sessions.get(session_id)
        if not session or session.status not in ACTIVE_STATUSES or not session.verification_paused:
            return False
        session.verification_paused = False
        session.resume_event.set()
        return True

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
        tasks = set(self.background_tasks)
        for session in self.sessions.values():
            if session.task and not session.task.done():
                if session.status in ACTIVE_STATUSES:
                    session.cancelled = True
                    session.status, session.message = 'cancelled', '已取消授权。'
                tasks.add(session.task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.discard_browser_sessions()

    async def discard_browser_sessions(self, account_id=None):
        for key in list(self.browser_sessions):
            if account_id is None or key[0] == account_id:
                browser, _, _ = self.browser_sessions.pop(key)
                await browser.close()

    def _prune(self):
        keep = sorted(self.sessions.values(), key=lambda s: s.created, reverse=True)[:12]
        pending = [s for s in self.sessions.values() if s.task and not s.task.done()]
        self.sessions = {s.id: s for s in keep + pending}

    async def authenticate_for_checkin(self, row, api_user):
        task = asyncio.current_task()
        self.background_tasks.add(task)
        try:
            return await self._authenticate_with_retries(row, api_user)
        finally:
            self.background_tasks.discard(task)

    async def _authenticate_with_retries(self, row, api_user):
        for attempt in range(len(RETRY_DELAYS) + 1):
            result = await self._authenticate_for_checkin_once(row, api_user)
            if not result:
                return result
            result['attempts'] = attempt + 1
            if result.get('profile') or not result.get('retryable') or attempt == len(RETRY_DELAYS):
                if attempt and not result.get('profile'):
                    result['message'] += f' 授权阶段已自动重试 {attempt} 次。'
                return result
            await asyncio.sleep(RETRY_DELAYS[attempt])

    async def _authenticate_for_checkin_once(self, row, api_user):
        """Try a fresh login with cached upstream cookies, without invalidating the site session."""
        credentials = self.store.credentials(row)
        cached = self._oauth_cookies(credentials.get('oauth_cookies', []), row['login_via'])
        if row['login_via'] not in OAUTH_LABELS or not cached:
            return {'error_kind': 'login_required', 'message': '没有有效的上游登录缓存，请主动重新授权。'}
        session = LoginSession(row['provider'], row['login_via'], row['name'], row['id'])
        session.checkin_identity = str(api_user)
        session.checkin_background = True
        session.checkin_require_login_response = True
        session.oauth_cookies = cached
        session.account_proxy = credentials.get('proxy', {'mode': 'default'})
        session.oauth_user_agent = credentials.get('oauth_user_agent')
        session.site_cookies = site_cookies(credentials.get('site_cookies', []), PROVIDERS[row['provider']])
        session.checkin_result = None
        session.checkin_submission_started = False
        session.retryable = False
        self.sessions[session.id] = session
        session.task = asyncio.current_task()
        try:
            async with self.window_lock:
                self.active_id = session.id
                session.created = time.time()
                try:
                    await asyncio.wait_for(self._run_window(session), timeout=90)
                except TimeoutError:
                    session.stage = 'background_timeout'
                    session.retryable = True
                    session.message = '后台授权页面加载超时，已保留登录缓存，请稍后重试。'
            if session.checkin_result:
                return session.checkin_result
            # A callback may already have awarded credit. Never replay it or
            # begin another login when its outcome could not be confirmed.
            if session.checkin_submission_started and session.retryable:
                session.retryable = False
                session.stage = 'outcome_unknown'
                session.message = '登录请求已发送，但签到结果尚未确认；已停止自动重试，请稍后查询余额或到官网确认。'
            return {
                'error_kind': session.stage,
                'retryable': session.retryable,
                'message': session.message,
            }
        except asyncio.CancelledError:
            if session.cancelled:
                return None
            raise
        finally:
            session.checkin_result = None
            self.sessions.pop(session.id, None)

    async def _run(self, session):
        async with self.window_lock:
            if session.provider == 'agentrouter' and session.method == 'linuxdo':
                from .native_login import run_native_login
                await run_native_login(self, session)
            else:
                await self._run_window(session)

    async def _run_window(self, session):
        provider = PROVIDERS[session.provider]
        browser = None
        context = None
        evidence = None
        document_observer = None
        request_observer = None
        cache_key = None
        try:
            if session.cancelled or session.status not in ACTIVE_STATUSES:
                return
            background = getattr(session, 'checkin_background', False)
            account_proxy = browser_proxy(getattr(session, 'account_proxy', None))
            inherited_proxy = get_playwright_proxy(use_proxy=provider.use_proxy) if account_proxy is None else None
            # Both OAuth providers use a truly headless, account-isolated browser.
            # Minimizing a headed window still briefly exposes it at startup.
            if background and session.provider == 'agentrouter':
                routing = hashlib.sha256(json.dumps(account_proxy or inherited_proxy, sort_keys=True).encode()).hexdigest()
                cache_key = (session.account_id, session.provider, session.method, session.checkin_identity, routing)
            if not background and session.account_id is not None:
                await self.discard_browser_sessions(session.account_id)
                row = self.store.account(session.account_id)
                if row and row['provider'] == session.provider:
                    credentials = self.store.credentials(row)
                    session.oauth_cookies = self._oauth_cookies(credentials.get('oauth_cookies', []), session.method)
                    session.site_cookies = site_cookies(credentials.get('site_cookies', []), provider)
            launch_options = {'headless': background}
            direct = account_proxy is not None and account_proxy['server'] == 'direct://'
            if direct:
                launch_options['args'] = ['--no-proxy-server']
            if inherited_proxy:
                launch_options['proxy'] = inherited_proxy
            cached_browser = self.browser_sessions.pop(cache_key, None) if cache_key else None
            if cached_browser:
                browser, context, page = cached_browser
                if not browser.is_connected() or page.is_closed():
                    await browser.close()
                    browser = None
            if browser is None:
                browser = await asyncio.wait_for(launch_async(**launch_options), timeout=90)
                context_options = {'viewport': None}
                if account_proxy is not None and not direct:
                    context_options['proxy'] = account_proxy
                if session.provider == 'agentrouter' and session.method == 'linuxdo':
                    session.oauth_user_agent = await chrome_user_agent(browser, getattr(session, 'oauth_user_agent', None))
                    # Keep interactive Chrome native. Even an identical UA
                    # override makes Playwright synthesize incorrect Client Hints
                    # (e.g. x86 / 10_15_7 on an Apple Silicon Mac).
                    if background:
                        context_options['user_agent'] = session.oauth_user_agent
                context = await browser.new_context(**context_options)
                if session.method == 'linuxdo':
                    await setup_notice_guard(context, provider.domain,
                        additional_origins=('https://linux.do', 'https://connect.linux.do'))
                else:
                    await setup_notice_guard(context, provider.domain)
                if getattr(session, 'oauth_cookies', None):
                    await context.add_cookies(self._oauth_cookies(session.oauth_cookies, session.method))
                if getattr(session, 'site_cookies', None):
                    await context.add_cookies(session.site_cookies)
                page = await context.new_page()
            else:
                if session.provider == 'agentrouter' and session.method == 'linuxdo':
                    # The encrypted identity normally comes back on the next run.
                    session.oauth_user_agent = await chrome_user_agent(browser, getattr(session, 'oauth_user_agent', None))
                # Only this app-owned target-site state is reset. The upstream
                # login and its verification cookies remain in the same context.
                host = urlparse(provider.domain).hostname
                await context.clear_cookies(name='session', domain=re.compile(r'^\.?' + re.escape(host) + r'$'))
                if _origin(page.url) == _origin(provider.domain):
                    await page.evaluate("() => {localStorage.removeItem('user'); localStorage.removeItem('status'); sessionStorage.clear();}")
                for extra in list(context.pages):
                    if extra is not page:
                        await extra.close()
            evidence = LoginEvidence(
                context, provider, session.method, self._session_cookie,
                require_login_response=getattr(session, 'checkin_require_login_response', False))
            evidence.start()
            blocked_documents = {}
            session.blocked_since = {}

            def observe_document(response):
                # Retain only host/status, never OAuth URLs, state, codes or response bodies.
                host = urlparse(response.url).hostname
                if (getattr(response.request, 'resource_type', None) != 'document'
                        or host not in ('github.com', 'linux.do', 'connect.linux.do')):
                    return
                if response.status in (403, 429) or transient_status(response.status):
                    blocked_documents[host] = response.status
                    session.blocked_since.setdefault(host, time.monotonic())
                elif response.status == 200:
                    blocked_documents.pop(host, None)
                    session.blocked_since.pop(host, None)

            if background:
                def observe_request(request):
                    if (_origin(request.url) == _origin(provider.domain)
                            and urlparse(request.url).path in ('/api/user/login', f'/api/oauth/{session.method}')):
                        session.checkin_submission_started = True

                context.on('request', observe_request)
                request_observer = observe_request
                context.on('response', observe_document)
                document_observer = observe_document
            # Explicit authorization does not auto-dismiss consent or verification.
            response = await page.goto(provider.domain + provider.login_path, wait_until='domcontentloaded', timeout=60000)
            if background and response and (response.status in (403, 429) or transient_status(response.status)):
                session.status, session.stage = 'failed', 'provider_unavailable'
                session.retryable = transient_status(response.status)
                session.message = f'目标平台登录页返回 HTTP {response.status}，已保留登录缓存，请稍后重试或处理站点验证。'
                return
            if session.cancelled:
                return
            session.status = 'waiting'
            if session.method in OAUTH_LABELS:
                label = OAUTH_LABELS[session.method]
                if _origin(page.url) != _origin(provider.domain):
                    session.status, session.message = 'failed', '未能打开目标平台登录页，授权未保存。'
                    return
                session.message = f'正在查找 {label} 登录入口；请保持窗口打开。'
                clicked = await self._open_oauth(context, page, session, provider)
                if session.cancelled or session.status not in ACTIVE_STATUSES:
                    return
                if not clicked:
                    session.status = 'unavailable'
                    session.retryable = background
                    session.message = f'当前页面未提供可识别的 {label} 授权入口，未保存账号；这不代表平台一定不支持。可重试或选择浏览器手动登录。'
                    return
                session.message = (f'请在本次打开的浏览器中登录 {label} 并确认授权，返回目标平台后自动校验；'
                                   '此窗口不共享日常浏览器登录状态，遇网站验证请手动完成。')
            else:
                session.message = '请在打开的浏览器窗口中完成登录，登录成功后会自动保存。'
            deadline = time.monotonic() + (30 if background else WINDOW_TIMEOUT)
            while time.monotonic() < deadline:
                if session.cancelled or session.status not in ACTIVE_STATUSES:
                    return
                if not context.pages:
                    session.status, session.message = 'cancelled', '登录窗口已关闭，未保存账号。'
                    return
                captured = await evidence.get()
                if captured:
                    if background:
                        session.checkin_submission_started = True
                    profile, cookie = captured
                    await self._complete_login(session, profile, cookie, context, evidence)
                    return
                await self._approve_linuxdo(context, session, provider)
                if getattr(session, 'checkin_require_login_response', False):
                    if await self._background_auth_blocked(session, context, blocked_documents):
                        return
                    session.status = 'waiting'
                    session.stage = 'waiting_fresh_login'
                    session.message = '正在后台复用授权会话；需要手动登录或验证时将停止。'
                    await asyncio.sleep(1.5)
                    continue
                if (self._upstream_auth_pending(session, context)
                        or self._oauth_return_pending(session, context, provider)):
                    self._show_upstream_wait(session)
                    await asyncio.sleep(1.5)
                    continue
                cookie = await self._session_cookie(context, provider)
                target_pages = [p for p in context.pages if _origin(p.url) == _origin(provider.domain)]
                if cookie and target_pages:
                    session.status = 'verifying'
                    session.stage = 'verifying_target'
                    session.message = '已返回目标平台并检测到会话，正在通过本站接口确认身份…'
                    profile = None
                    for target_page in reversed(target_pages):
                        try:
                            profile = await verify_target_profile(target_page, provider)
                        except ProfileVerificationBlocked:
                            session.profile_recheck_after = time.monotonic() + VERIFICATION_RECHECK_DELAYS[0]
                            captured = await self._wait_verified_profile(session, context, evidence, deadline)
                            if captured:
                                await self._complete_login(session, *captured, context, evidence)
                            return
                        except PlaywrightError as exc:
                            if 'Execution context was destroyed' not in str(exc):
                                raise
                            session.stage = 'returning_to_target'
                            session.message = '目标平台仍在跳转，等待页面稳定后再校验。'
                            continue
                        if profile:
                            break
                    if profile and cookie == await self._session_cookie(context, provider):
                        await self._complete_login(session, profile, cookie, context, evidence)
                        return
                    if session.cancelled or session.status not in ACTIVE_STATUSES:
                        return
                    session.status = 'waiting'
                    session.message = '检测到站点会话，但账号接口尚未确认身份；请完成目标平台授权后稍候。'
                else:
                    session.stage = 'waiting_target_session' if target_pages else 'waiting_upstream'
                    session.message = ('尚未取得目标平台有效会话；请完成登录并返回目标平台控制台。'
                                       if target_pages else '仍在身份提供方登录或验证页面；请手动完成并确认授权，返回目标平台。')
                await asyncio.sleep(1.5)
            session.status, session.message = 'expired', f'等待授权超时（阶段：{session.stage}）；未确认目标平台身份，未保存账号。'
            session.retryable = background
        except asyncio.CancelledError:
            if session.status in ACTIVE_STATUSES:
                session.status, session.message = 'cancelled', '已取消授权。'
            raise
        except Exception as exc:
            if session.status in ACTIVE_STATUSES:
                session.status = 'failed'
                session.retryable = getattr(session, 'checkin_background', False) and transient_error(exc)
                session.message = proxy_failure_message(exc) or ('后台连接中断或页面加载超时，已保留登录缓存，请稍后重试。' if session.retryable else
                                   '登录未能完成，请重试，或主动重新授权。')
        finally:
            if evidence:
                await evidence.close()
            if context and document_observer:
                context.remove_listener('response', document_observer)
            if context and request_observer:
                context.remove_listener('request', request_observer)
            if browser:
                if cache_key and session.status == 'success':
                    self.browser_sessions[cache_key] = (browser, context, page)
                else:
                    try:
                        await browser.close()
                    except Exception:
                        pass

    async def _approve_linuxdo(self, context, session, provider):
        """Approve only the current AgentRouter request that the user authorized us to handle."""
        client_id = getattr(session, 'oauth_client_id', None)
        if (session.provider != 'agentrouter' or session.method != 'linuxdo'
                or not client_id or getattr(session, 'approval_clicked', False)):
            return False
        for page in context.pages:
            location = urlparse(page.url)
            query = parse_qs(location.query)
            if (location.scheme != 'https' or location.hostname != 'connect.linux.do'
                    or location.port not in (None, 443) or location.path != '/oauth2/authorize'
                    or query.get('client_id') != [client_id] or not query.get('state')):
                continue
            # The consent page must identify the same application origin, and
            # the visible action must remain on LinuxDO's official approve route.
            app_matches = await page.evaluate('''origin => [...document.querySelectorAll('a[href]')]
                .some(a => {try {return new URL(a.href).origin === new URL(origin).origin} catch {return false}})
            ''', provider.domain)
            if not app_matches:
                continue
            for role in ('link', 'button'):
                allow = page.get_by_role(role, name=re.compile(r'^(允许|Allow)$', re.I))
                for index in range(await allow.count()):
                    candidate = allow.nth(index)
                    if role == 'link':
                        href = await candidate.get_attribute('href')
                    else:
                        # Accept only a real submit control for the official
                        # approval route, never a generic permission button.
                        href = await candidate.evaluate('''el => {
                            const form = el.form;
                            if (!form || el.type !== 'submit') return null;
                            const method = (el.getAttribute('formmethod') || form.method).toLowerCase();
                            if (!['get', 'post'].includes(method)) return null;
                            return el.getAttribute('formaction') || form.getAttribute('action');
                        }''')
                    target = urlparse(urljoin(page.url, href or ''))
                    if (not href or _origin(target.geturl()) != _origin('https://connect.linux.do')
                            or not (target.path == '/oauth2/approve' or target.path.startswith('/oauth2/approve/'))
                            or not await candidate.is_visible() or not await candidate.is_enabled()):
                        continue
                    session.approval_clicked = True
                    session.stage = 'approving_oauth'
                    await candidate.click(timeout=6000)
                    return True
        return False

    @staticmethod
    def _oauth_cookies(cookies, method):
        # Each account owns its own encrypted cache. Never copy site sessions or
        # cookies from unrelated domains into another platform's authorization.
        roots = {'github': ('github.com',), 'linuxdo': ('linux.do',)}.get(method, ())
        allowed = []
        for cookie in cookies:
            domain = (cookie.get('domain') or '').lstrip('.').lower()
            if not any(domain == root or domain.endswith('.' + root) for root in roots):
                continue
            if cookie.get('expires', -1) > 0 and cookie['expires'] <= time.time():
                continue
            allowed.append({k: v for k, v in cookie.items() if k in
                            ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite',
                             'partitionKey', '_crHasCrossSiteAncestor')})
        return allowed

    @staticmethod
    def _upstream_auth_pending(session, context):
        """A target site's anonymous OAuth-state cookie is not a completed SSO login."""
        methods = OAUTH_LABELS if session.method == 'manual' else (session.method,)
        for page in context.pages:
            location = urlparse(page.url)
            if location.scheme != 'https' or location.port not in (None, 443):
                continue
            path = location.path.rstrip('/')
            if ('linuxdo' in methods and location.hostname in ('linux.do', 'connect.linux.do')
                    and any(path == route or path.startswith(route + '/')
                            for route in ('/login', '/session', '/sessions', '/oauth2', '/auth', '/challenge'))):
                return True
            if ('github' in methods and location.hostname == 'github.com'
                    and any(path == route or path.startswith(route + '/')
                            for route in ('/login', '/session', '/sessions', '/two-factor', '/sudo'))):
                return True
        return False

    @staticmethod
    def _oauth_return_pending(session, context, provider):
        if session.method not in OAUTH_LABELS:
            return False
        # The popup may still be about:blank before its first SSO navigation.
        # Do not probe the opener's anonymous OAuth-state session on /login.
        return not any(_origin(page.url) == _origin(provider.domain)
                       and urlparse(page.url).path.rstrip('/') not in (provider.login_path.rstrip('/'), '/register')
                       for page in context.pages)

    @staticmethod
    def _show_upstream_wait(session):
        label = OAUTH_LABELS.get(session.method, '身份提供方')
        session.status, session.stage = 'waiting', 'waiting_upstream'
        session.verification_paused = False
        session.verification_auto = True
        session.message = (f'正在等待 {label} 完成登录或真人验证并返回目标平台；回跳后会自动检测和保存。'
                           '若验证反复出现，请取消本次授权，检查该账号的代理或网络后重试。')

    async def _background_auth_blocked(self, session, context, blocked_documents):
        for page in context.pages:
            location = urlparse(page.url)
            if location.hostname in blocked_documents:
                status = blocked_documents[location.hostname]
                # The browser may complete the site's own interstitial.
                # Wait for that navigation; never click or solve a challenge.
                since = getattr(session, 'blocked_since', {}).get(location.hostname)
                if (status == 403 and since is not None
                        and time.monotonic() - since < UPSTREAM_VERIFICATION_GRACE):
                    continue
                if transient_status(status):
                    session.status, session.stage = 'failed', 'upstream_unavailable'
                    session.retryable = True
                    session.message = f'{location.hostname} 授权页面暂不可用（HTTP {status}），已保留登录缓存，请稍后重试。'
                    return True
                session.status, session.stage = 'manual_required', 'upstream_blocked'
                session.message = (f'{location.hostname} 授权页面返回 HTTP {status}，站点拒绝访问或要求验证；'
                                   '已保留登录缓存，未判定凭据失效。请检查站点验证或网络，无需反复重新输入账号密码。')
                return True
            if (location.hostname in ('github.com', 'linux.do') and location.path.rstrip('/') == '/login'
                    and await page.locator('input[type="password"]').first.is_visible()):
                session.status, session.stage = 'manual_required', 'login_required'
                session.message = '上游已显示账号密码登录表单，需要主动重新授权以更新登录缓存。'
                return True
        return False

    async def _complete_login(self, session, profile, cookie, context=None, evidence=None):
        if session.cancelled or session.status not in ACTIVE_STATUSES:
            return
        provider = PROVIDERS[session.provider]
        if context is not None:
            if profile.get('_login_response'):
                # Keep the explicit check-in flag, but fetch money from /user/self.
                # A failed balance refresh must not discard a valid new session.
                checked = profile.get('checked_in')
                profile = {key: value for key, value in profile.items() if key not in ('quota', 'used_quota')}
                for page in reversed(context.pages):
                    if time.monotonic() < getattr(session, 'profile_recheck_after', 0):
                        break
                    if _origin(page.url) != _origin(provider.domain):
                        continue
                    try:
                        await asyncio.sleep(1.2)
                        refreshed = await verify_target_profile(page, provider, profile['id'])
                    except ProfileVerificationBlocked:
                        # The login response already proves identity and issued
                        # this session cookie. Balance availability is independent:
                        # save the authorization without placeholder/stale money.
                        session.profile_recheck_after = time.monotonic() + VERIFICATION_RECHECK_DELAYS[0]
                        break
                    except PlaywrightError:
                        refreshed = None
                    if refreshed and str(refreshed['id']) != str(profile['id']):
                        session.status, session.message = 'failed', '余额查询身份与本次授权不一致，未保存账号。'
                        return
                    if refreshed and str(refreshed['id']) == str(profile['id']):
                        profile = {**refreshed, 'checked_in': checked}
                        break
                if cookie != await self._session_cookie(context, provider):
                    session.status, session.message = 'failed', '校验期间会话发生变化，未保存账号。'
                    return
            if session.provider == 'agentrouter' and session.method in OAUTH_LABELS:
                session.oauth_cookies = self._oauth_cookies(await context.cookies(), session.method)
            session.site_cookies = site_cookies(await context.cookies([provider.domain]), provider)
        session.verification_paused = False
        session.verification_auto = False
        session.stage = 'identity_confirmed'
        if hasattr(session, 'checkin_identity'):
            if str(profile['id']) != session.checkin_identity:
                session.status, session.message = 'failed', '本次登录身份与原账号不同，未更新账号。'
                return
            session.checkin_result = {'profile': profile, 'session': cookie,
                                      'site_cookies': getattr(session, 'site_cookies', []),
                                      'oauth_cookies': getattr(session, 'oauth_cookies', []),
                                      **({'oauth_user_agent': session.oauth_user_agent}
                                         if getattr(session, 'oauth_user_agent', None) else {})}
            session.status = 'success'
        else:
            await self._persist(session, profile, cookie)

    async def _wait_verified_profile(self, session, context, evidence, deadline):
        """Observe completion immediately, with spaced same-origin identity reads as a fallback."""
        provider = PROVIDERS[session.provider]
        session.status, session.stage = 'waiting', 'verification_blocked'
        session.verification_paused = True
        session.verification_auto = True
        session.resume_event.clear()
        session.message = ('请在授权窗口完成网站要求的验证；应用会自动检测并保存，无需返回点击确认。'
                           '当前仍需确认目标平台的账号身份。')
        attempts = 0
        next_check = time.monotonic() + VERIFICATION_RECHECK_DELAYS[0]
        while time.monotonic() < deadline:
            if session.cancelled or session.status not in ACTIVE_STATUSES or not context.pages:
                session.status, session.message = 'cancelled', '授权窗口已关闭，未保存本次登录。'
                return None
            captured = await evidence.get()
            if captured:
                return captured
            if (self._upstream_auth_pending(session, context)
                    or self._oauth_return_pending(session, context, provider)):
                self._show_upstream_wait(session)
                next_check = max(next_check, time.monotonic() + 1.5)
            resume = asyncio.create_task(session.resume_event.wait())
            changed = asyncio.create_task(evidence.changed.wait())
            try:
                await asyncio.wait({resume, changed}, timeout=min(1.0, max(.01, deadline - time.monotonic()),
                                                                 max(.01, next_check - time.monotonic())),
                                   return_when=asyncio.FIRST_COMPLETED)
                evidence.changed.clear()
                captured = await evidence.get()
                if captured:
                    return captured
                if (self._upstream_auth_pending(session, context)
                        or self._oauth_return_pending(session, context, provider)):
                    self._show_upstream_wait(session)
                    session.resume_event.clear()
                    continue
                if session.resume_event.is_set() or time.monotonic() >= next_check:
                    session.resume_event.clear()
                    session.verification_paused = False
                    session.status, session.stage = 'verifying', 'verifying_target'
                    for page in reversed(context.pages):
                        if _origin(page.url) != _origin(provider.domain):
                            continue
                        cookie = await self._session_cookie(context, provider)
                        if not cookie:
                            continue
                        try:
                            current = await verify_target_profile(page, provider)
                        except (PlaywrightError, ProfileVerificationBlocked):
                            break
                        if current and cookie == await self._session_cookie(context, provider):
                            return current, cookie
                    attempts += 1
                    next_check = time.monotonic() + VERIFICATION_RECHECK_DELAYS[min(attempts, len(VERIFICATION_RECHECK_DELAYS) - 1)]
                    session.status, session.stage = 'waiting', 'verification_blocked'
                    session.verification_paused = True
            finally:
                for task in (resume, changed):
                    task.cancel()
                await asyncio.gather(resume, changed, return_exceptions=True)
        session.status, session.message = 'expired', '等待网站验证超时，尚未确认目标平台身份，未保存本次登录。'
        return None

    async def _open_oauth(self, context, page, session, provider):
        if provider.name == 'agentrouter':
            session.stage = 'loading_provider_config'
            client_id = await self._wait_oauth_config(page, provider, session.method)
            if not client_id:
                session.status, session.stage = 'failed', 'provider_config_unavailable'
                session.retryable = getattr(session, 'checkin_background', False)
                session.message = '目标平台尚未加载有效的登录配置，未打开上游授权页。请稍后重试。'
                return False
            session.stage = 'opening_oauth'
            session.oauth_client_id = client_id
        if await self._click_oauth(context, page, session.method, provider):
            return True
        if session.cancelled:
            return False
        if urlparse(page.url).path.rstrip('/') != provider.login_path.rstrip('/'):
            # 页面上此刻没有授权入口：先回到站点的登录页再找一次。
            # 只做同源导航，不填表、不提交注册、不构造授权 URL。
            try:
                await page.goto(provider.domain + provider.login_path,
                                wait_until='domcontentloaded', timeout=60000)
            except PlaywrightError:
                return False
            if session.cancelled or _origin(page.url) != _origin(provider.domain):
                return False
            if await self._click_oauth(context, page, session.method, provider):
                return True
            if session.cancelled:
                return False
        paths = provider.oauth_navigation_paths
        if not paths or urlparse(page.url).path.rstrip('/') != provider.login_path.rstrip('/'):
            return False
        # AnyRouter 的入口可能只在站内「注册 → 登录」导航后渲染。
        # 只点击当前页面已有的同源链接，不填表、不提交注册、不构造授权 URL。
        previous_path = provider.login_path
        for path in paths:
            if session.cancelled:
                return False
            session.message = '正在通过站内「注册 → 登录」导航重新查找授权入口，不会提交注册信息。'
            if not await self._navigate_login_link(page, provider, previous_path, path):
                return False
            previous_path = path
        if session.cancelled:
            return False
        return await self._click_oauth(context, page, session.method, provider)

    async def _wait_oauth_config(self, page, provider, method):
        """The SPA renders enabled OAuth buttons before /api/status has supplied client IDs."""
        try:
            handle = await page.wait_for_function('''o => {
                if (location.origin !== new URL(o.origin).origin) return false;
                try {
                    const status = JSON.parse(localStorage.getItem('status') || '{}');
                    const client = status[o.key];
                    return typeof client === 'string' && client.trim()
                        && !['undefined', 'null'].includes(client.trim()) ? client.trim() : false;
                } catch (_) { return false; }
            }''', arg={'origin': provider.domain, 'key': f'{method}_client_id'},
                timeout=OAUTH_ENTRY_TIMEOUT * 1000)
            try:
                return await handle.json_value()
            finally:
                await handle.dispose()
        except PlaywrightError:
            return None

    async def _navigate_login_link(self, page, provider, source_path, target_path):
        """仅跟随当前页面可见的同源路径链接；绝不点击注册表单提交按钮。"""
        origin = _origin(provider.domain)
        deadline = time.monotonic() + OAUTH_ENTRY_TIMEOUT
        while time.monotonic() < deadline:
            if (_origin(page.url) != origin
                    or urlparse(page.url).path.rstrip('/') != source_path.rstrip('/')):
                return False
            links = page.get_by_role('link')
            for index in range(await links.count()):
                link = links.nth(index)
                if not await link.is_visible() or not await link.is_enabled():
                    continue
                href = await link.get_attribute('href')
                if not href:
                    continue
                target = urlparse(urljoin(page.url, href))
                if (_origin(target.geturl()) != origin
                        or target.path.rstrip('/') != target_path.rstrip('/')
                        or await link.get_attribute('target') not in (None, '', '_self')):
                    continue
                if (_origin(page.url) != origin
                        or urlparse(page.url).path.rstrip('/') != source_path.rstrip('/')):
                    return False
                await link.click(timeout=6000)
                await page.wait_for_url(
                    lambda url: _origin(str(url)) == origin
                    and urlparse(str(url)).path.rstrip('/') == target_path.rstrip('/'),
                    timeout=12000, wait_until='domcontentloaded')
                return _origin(page.url) == origin and urlparse(page.url).path.rstrip('/') == target_path.rstrip('/')
            await asyncio.sleep(0.25)
        return False

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
            snapshot = quota_snapshot(profile, PROVIDERS[session.provider].quota_per_unit)
            quota = snapshot['balance'] if snapshot else None
            used = snapshot['used_quota'] if snapshot else None
            login_via = session.method if session.method in OAUTH_LABELS else 'manual'
            if session.account_id is not None:
                try:
                    updated = self.store.apply_login(session.account_id, session_cookie, profile['id'], login_via,
                                                     quota, used, provider=session.provider,
                                                     name=session.name, interval_hours=session.interval_hours,
                                                     site_cookies=getattr(session, 'site_cookies', []),
                                                     oauth_cookies=getattr(session, 'oauth_cookies', []),
                                                     oauth_user_agent=getattr(session, 'oauth_user_agent', None),
                                                     proxy=getattr(session, 'account_proxy', None))
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
                    'oauth_cookies': getattr(session, 'oauth_cookies', []),
                    'oauth_user_agent': getattr(session, 'oauth_user_agent', None),
                    'proxy': getattr(session, 'account_proxy', None),
                    'site_cookies': getattr(session, 'site_cookies', []),
                })
                known_balance = quota is not None and used is not None
                self.store.patch(session.account, balance=quota, used_quota=used, has_snapshot=int(known_balance),
                                 total_quota=round(quota + used, 2) if known_balance else None,
                                 last_message='已通过浏览器授权关联账号')
            session.status, session.message = 'success', f'已保存「{name}」的登录状态。'
            if snapshot is None:
                session.message += ' 余额暂未取得，保留原余额或显示未知，后续签到时再同步。'
