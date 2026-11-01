"""使用临时浏览器窗口完成站点自身的授权，不保存上游身份会话。"""
import asyncio
import re
import secrets
import time
from playwright.async_api import Error as PlaywrightError
from urllib.parse import urljoin, urlparse, parse_qs
from http.cookies import SimpleCookie

from cloakbrowser import launch_async

from .providers import PROVIDERS, BROWSER_METHODS, supports
from .upstream_imports import prepare_browser_page, wait_for_waf_ready

WINDOW_TIMEOUT = 900
OAUTH_LABELS = {'github': 'GitHub', 'linuxdo': 'LinuxDO'}
OAUTH_BUTTONS = {
    method: re.compile(r'^\s*(?:' + (r'github(?:[_\s-]*logo)?' if method == 'github' else r'linux\s*do(?:[_\s-]*logo)?')
                       + r'\s+)?(?:(?:使用|通过)\s*)?(?:(?:continue|sign\s*in|log\s*in|login)\s+with\s+)?'
                       + label + r'\s*(?:(?:继续|登录|授权|登入))?\s*$', re.IGNORECASE)
    for method, label in (('github', r'git\s*hub'), ('linuxdo', r'linux\s*do'))
}
OAUTH_ENTRY_TIMEOUT = 12
ACTIVE_STATUSES = ('starting', 'waiting', 'verifying')


class LoginConflict(RuntimeError):
    pass


class ProfileVerificationBlocked(RuntimeError):
    """A non-JSON or denied identity response needs user attention, not polling."""
    pass


def _origin(url):
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


async def verify_target_profile(page, provider):
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
        if (hint && hint.id != null) headers[o.userHeader] = String(hint.id);
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
    }''', {'url': target, 'userHeader': provider.api_user_key, 'nonce': secrets.token_urlsafe(12)})
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
    def __init__(self, context, provider, method, read_cookie):
        self.context, self.provider, self.method = context, provider, method
        self.read_cookie = read_cookie
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
        if path not in login_paths | {self.provider.user_info_path}:
            return
        if path in login_paths:
            self.sequence += 1
            self.latest = None
        sequence = self.sequence
        task = asyncio.create_task(self._capture(response, sequence, path in login_paths))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

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
            if await response.finished() is not None:
                return
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
        self.resume_event = asyncio.Event()


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
                'account_id': session.account, 'elapsed': int(time.time() - session.created),
                'stage': session.stage, 'verification_paused': session.verification_paused}

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

    async def authenticate_for_checkin(self, row, api_user):
        """Fresh, interactive site login; never import the old site session."""
        session = LoginSession(row['provider'], row['login_via'] or 'manual', row['name'], row['id'])
        if session.method not in BROWSER_METHODS:
            session.method = 'manual'
        session.checkin_identity = str(api_user)
        # 签到语义：先退出旧会话，再由用户重新登录。
        session.checkin_logout = bool(PROVIDERS[row['provider']].logout_path)
        session.checkin_result = None
        self.sessions[session.id] = session
        session.task = asyncio.current_task()
        try:
            async with self.window_lock:
                self.active_id = session.id
                session.created = time.time()
                await self._run_window(session)
            return session.checkin_result
        except asyncio.CancelledError:
            if session.cancelled:
                return None
            raise
        finally:
            session.checkin_result = None
            self.sessions.pop(session.id, None)

    async def _run(self, session):
        async with self.window_lock:
            await self._run_window(session)

    async def _run_window(self, session):
        provider = PROVIDERS[session.provider]
        browser = None
        evidence = None
        try:
            if session.cancelled or session.status not in ACTIVE_STATUSES:
                return
            browser = await asyncio.wait_for(launch_async(headless=False), timeout=90)
            context = await browser.new_context(viewport=None)
            evidence = LoginEvidence(context, provider, session.method, self._session_cookie)
            evidence.start()
            page = await context.new_page()
            # Interactive authorization must not auto-dismiss consent/verification dialogs.
            await page.goto(provider.domain + provider.login_path, wait_until='domcontentloaded', timeout=60000)
            if session.cancelled:
                return
            if getattr(session, 'checkin_logout', False) and provider.logout_path:
                # 签到语义要求先退出旧会话；这一步也必须在真人窗口内完成，
                # 避免无头会话的连续接口请求触发站点滑动验证。
                await self._logout_in_window(page, session, provider)
                if session.cancelled:
                    return
                # 退出后当前页面可能仍是控制台（站点不做前端跳转），而登录入口只渲染在
                # 登录页；不重新打开登录页就会误判为「未提供授权入口」。
                if urlparse(page.url).path.rstrip('/') != provider.login_path.rstrip('/'):
                    try:
                        await page.goto(provider.domain + provider.login_path,
                                        wait_until='domcontentloaded', timeout=60000)
                    except PlaywrightError:
                        pass
            session.status = 'waiting'
            if session.method in OAUTH_LABELS:
                label = OAUTH_LABELS[session.method]
                if _origin(page.url) != _origin(provider.domain):
                    session.status, session.message = 'failed', '未能打开目标平台登录页，授权未保存。'
                    return
                session.message = f'正在查找 {label} 登录入口；请保持窗口打开。'
                clicked = await self._open_oauth(context, page, session, provider)
                if session.cancelled:
                    return
                if not clicked:
                    session.status = 'unavailable'
                    session.message = f'当前页面未提供可识别的 {label} 授权入口，未保存账号；这不代表平台一定不支持。可重试或选择浏览器手动登录。'
                    return
                session.message = (f'请在本次打开的浏览器中登录 {label} 并确认授权，返回目标平台后自动校验；'
                                   '此窗口不共享日常浏览器登录状态，遇网站验证请手动完成。')
            else:
                session.message = '请在打开的浏览器窗口中完成登录，登录成功后会自动保存。'
            deadline = time.monotonic() + WINDOW_TIMEOUT
            while time.monotonic() < deadline:
                if session.cancelled or session.status not in ACTIVE_STATUSES:
                    return
                if not context.pages:
                    session.status, session.message = 'cancelled', '登录窗口已关闭，未保存账号。'
                    return
                captured = await evidence.get()
                if captured:
                    profile, cookie = captured
                    await self._complete_login(session, profile, cookie)
                    return
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
                            session.status = 'waiting'
                            session.stage = 'verification_blocked'
                            session.verification_paused = True
                            session.resume_event.clear()
                            session.message = ('目标平台账号接口返回防护页面或拒绝访问，尚未确认账号身份；已暂停自动请求。'
                                               '请在当前窗口手动处理网站验证，再点击「我已处理，重新校验」。'
                                               '仅登录 LinuxDO/GitHub 不代表已完成目标平台授权。')
                            while True:
                                resume = asyncio.create_task(session.resume_event.wait())
                                observed = asyncio.create_task(evidence.changed.wait())
                                try:
                                    done, _ = await asyncio.wait({resume, observed},
                                        timeout=max(0.1, deadline - time.monotonic()),
                                        return_when=asyncio.FIRST_COMPLETED)
                                    if not done:
                                        session.status, session.message = 'expired', '等待人工处理已超时；未取得目标平台有效授权响应，未保存账号。'
                                        return
                                    evidence.changed.clear()
                                    captured = await evidence.get()
                                    if captured:
                                        await self._complete_login(session, *captured)
                                        return
                                    if resume in done:
                                        break
                                finally:
                                    for waiter in (resume, observed):
                                        if not waiter.done():
                                            waiter.cancel()
                                    await asyncio.gather(resume, observed, return_exceptions=True)
                            break
                        except PlaywrightError as exc:
                            if 'Execution context was destroyed' not in str(exc):
                                raise
                            session.stage = 'returning_to_target'
                            session.message = '目标平台仍在跳转，等待页面稳定后再校验。'
                            continue
                        if profile:
                            break
                    if profile and cookie == await self._session_cookie(context, provider):
                        await self._complete_login(session, profile, cookie)
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
        except asyncio.CancelledError:
            if session.status in ACTIVE_STATUSES:
                session.status, session.message = 'cancelled', '已取消授权。'
            raise
        except Exception:
            if session.status in ACTIVE_STATUSES:
                session.status = 'failed'
                session.message = '登录窗口未能完成，请重试，或改用账号密码 / Session 方式。'
        finally:
            if evidence:
                await evidence.close()
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _logout_in_window(self, page, session, provider):
        """在真人窗口内退出旧会话；失败不阻断后续登录，只记录提示。"""
        session.stage = 'logging_out'
        session.message = '正在退出该平台旧会话，随后请重新登录以完成签到…'
        try:
            result = await page.evaluate(r'''async (o) => {
              const response = await fetch(o.url, {
                credentials: 'include', cache: 'no-store',
                headers: {'X-Requested-With': 'XMLHttpRequest'}
              });
              const text = await response.text();
              let body = null;
              try { body = JSON.parse(text); } catch (e) {}
              return {status: response.status, body,
                      guarded: /text\/html/i.test(response.headers.get('content-type') || '')};
            }''', {'url': provider.domain + provider.logout_path})
        except PlaywrightError:
            return
        if not isinstance(result, dict) or result.get('guarded'):
            return
        body = result.get('body')
        # 站点在未登录时也返回 success，因此这里只用于触发退出动作，不做断言。
        if result.get('status') == 200 and isinstance(body, dict) and body.get('success') is True:
            session.stage = 'logged_out'
        # 给站点风控窗口留出间隔，避免紧接着的登录请求被判为自动化流量。
        await asyncio.sleep(1.2)

    async def _complete_login(self, session, profile, cookie):
        if session.cancelled or session.status not in ACTIVE_STATUSES:
            return
        session.verification_paused = False
        session.stage = 'identity_confirmed'
        if hasattr(session, 'checkin_identity'):
            if str(profile['id']) != session.checkin_identity:
                session.status, session.message = 'failed', '本次登录身份与原账号不同，未更新账号。'
                return
            session.checkin_result = {'profile': profile, 'session': cookie}
            session.status = 'success'
        else:
            await self._persist(session, profile, cookie)

    async def _open_oauth(self, context, page, session, provider):
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
            quota = round(profile['quota'] / 500000, 2) if isinstance(profile.get('quota'), (int, float)) else None
            used = round(profile['used_quota'] / 500000, 2) if isinstance(profile.get('used_quota'), (int, float)) else None
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
                known_balance = quota is not None and used is not None
                self.store.patch(session.account, balance=quota, used_quota=used, has_snapshot=int(known_balance),
                                 total_quota=round(quota + used, 2) if known_balance else None,
                                 last_message='已通过浏览器授权关联账号')
            session.status, session.message = 'success', f'已保存「{name}」的登录状态。'
