"""Check-in adapters derived from millylee/anyrouter-check-in (BSD-2-Clause).
No browser profile is persisted: account secrets stay in the encrypted database.
"""
import asyncio
import os
import sys
import time
from pathlib import Path
import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .providers import PROVIDERS
from .quota import quota_snapshot
from .cookies import site_cookies
from .retry import RETRY_DELAYS, transient_error, transient_status
from .proxy import browser_proxy, proxy_failure_message
from .upstream_imports import (get_playwright_proxy, login_with_credentials, prepare_browser_page,
                               wait_for_waf_ready)

# The sites are SPAs whose own redirects continue after `goto` resolves. Any browser
# call issued in that window dies with a destroyed execution context, so settle first.
SETTLE_TIMEOUT = 10.0
SETTLE_STABLE = 0.6
CONTEXT_RETRIES = 3
# 阿里云 WAF 对同一会话的密集接口请求会升级为滑动验证；
# 保持人类节奏的间隔能显著降低触发率（实测连发 4 个后几乎必触发）。
REQUEST_SPACING = 1.1
# 命中防护页后，等待站点风控窗口衰减再重试一次；不无限重试。
WAF_COOLDOWN = 8.0
WAF_RETRIES = 1


class WAFCookiesUnavailable(RuntimeError):
    """The provider's required first-party WAF cookies were not issued."""


def _local_chrome():
    if sys.platform == 'darwin':
        candidates = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    elif os.name == 'nt':
        roots = [os.getenv('PROGRAMFILES'), os.getenv('PROGRAMFILES(X86)'), os.getenv('LOCALAPPDATA')]
        candidates = [str(Path(root) / 'Google/Chrome/Application/chrome.exe') for root in roots if root]
    else:
        candidates = ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser']
    return next((path for path in candidates if Path(path).is_file()), None)


from cloakbrowser import launch_async  # noqa: E402

LOCAL_CHROME = _local_chrome()
if LOCAL_CHROME and not os.getenv('CLOAKBROWSER_BINARY_PATH'):
    os.environ['CLOAKBROWSER_BINARY_PATH'] = LOCAL_CHROME


def browser_available():
    folder = Path.home() / '.cloakbrowser'
    return bool(LOCAL_CHROME or (folder.is_dir() and any(folder.iterdir())))


ALREADY_WORDS = ('已经签到', '已签到', '重复签到', 'already checked', 'already signed')


def classify_response(status_code, body):
    """Return success only for an explicit structured response, never HTML heuristics."""
    if status_code != 200:
        return 'manual_required' if status_code in (401, 403, 429) else 'failed', f'站点返回 HTTP {status_code}，请检查登录状态或稍后重试。'
    if not isinstance(body, dict):
        return 'failed', '站点返回了无法识别的内容，可能被防护页拦截。'
    message = str(body.get('msg', body.get('message', '')))
    if any(word in message.lower() for word in ALREADY_WORDS):
        return 'already_checked', '站点确认今日已签到。'
    if body.get('success') is True or body.get('ret') == 1 or body.get('code') == 0:
        return 'success', '签到接口返回成功。'
    return 'failed', f'签到未成功：{message[:100] or "站点未确认成功"}'


class BrowserSession:
    """Same-origin browser fetch; avoids TLS fingerprint rejection by the site WAF."""
    def __init__(self, provider, cookies, site_cookie_cache=None, proxy=None):
        self.provider = provider
        self.cookies = cookies
        self.site_cookie_cache = site_cookies(site_cookie_cache or [], provider)
        self.browser = None
        self.proxy = proxy
        self._last_request = 0.0

    async def __aenter__(self):
        from urllib.parse import urlparse
        launch_kwargs = {'headless': True}
        direct = self.proxy is not None and self.proxy['server'] == 'direct://'
        if direct:
            launch_kwargs['args'] = ['--no-proxy-server']
        proxy = get_playwright_proxy(use_proxy=self.provider.use_proxy) if self.proxy is None else None
        if proxy:
            launch_kwargs['proxy'] = proxy
        self.browser = await launch_async(**launch_kwargs)
        try:
            context = await self.browser.new_context(**({'proxy': self.proxy} if self.proxy is not None and not direct else {}))
            self.context = context
            if self.site_cookie_cache:
                await context.add_cookies(self.site_cookie_cache)
            host = urlparse(self.provider.domain).hostname
            self.page = await context.new_page()
            await prepare_browser_page(self.page)
            if self.provider.sign_in_path is None and self.provider.waf_cookie_names:
                # Match the upstream AgentRouter flow: complete the site's WAF bootstrap
                # before attaching the account session, then require its acw_tc cookie.
                await self._goto(self.provider.domain + self.provider.login_path)
                await wait_for_waf_ready(self.page)
                cookies = await context.cookies([self.provider.domain])
                waf_cookies = {cookie.get('name'): cookie.get('value') for cookie in cookies
                               if cookie.get('name') in self.provider.waf_cookie_names and cookie.get('value')}
                missing = [name for name in self.provider.waf_cookie_names if name not in waf_cookies]
                if missing:
                    raise WAFCookiesUnavailable()
            else:
                # Let the site issue its own WAF cookies before the account session is added.
                await self._goto(self.provider.domain)
            await context.add_cookies([
                {'name': name, 'value': value, 'domain': host, 'path': '/'}
                for name, value in self.cookies.items()
            ])
            # For providers with an explicit sign-in endpoint, restore the usual login SPA.
            if self.provider.sign_in_path:
                await self._goto(self.provider.domain + self.provider.login_path)
        except BaseException:
            await self.browser.close()
            self.browser = None
            raise
        return self

    async def _goto(self, url):
        """Navigate without waiting on site UI.

        The sites are SPAs that redirect on their own after load; waiting for a
        rendered login shell here costs a full timeout and then aborts our next
        navigation, so only the document itself is awaited. A run that loses the
        race to the site's own redirect is fine as long as we stay on the site.
        """
        try:
            await self.page.goto(url, wait_until='domcontentloaded', timeout=60_000)
        except PlaywrightError as exc:
            if 'ERR_ABORTED' not in str(exc) or not self.page.url.startswith(self.provider.domain):
                raise
        await self._settle()

    async def _settle(self):
        """Wait until the site stops redirecting, so evaluate() keeps a live context."""
        deadline = time.monotonic() + SETTLE_TIMEOUT
        previous = None
        stable_since = None
        while time.monotonic() < deadline:
            try:
                current = self.page.url
            except PlaywrightError:
                current = None
            if current and current == previous:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= SETTLE_STABLE:
                    break
            else:
                stable_since = None
            previous = current
            await asyncio.sleep(0.1)
        try:
            await self.page.wait_for_load_state('domcontentloaded', timeout=3_000)
        except PlaywrightError:
            pass

    async def request(self, path, api_user, method='GET', payload=None):
        script = '''async (o) => {
          const target = new URL(o.path, o.origin);
          if (location.origin !== o.origin || target.origin !== o.origin) throw new Error('Unexpected site origin');
          const headers = {[o.userHeader]: o.user, 'X-Requested-With': 'XMLHttpRequest'};
          const options = {method: o.method, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error'};
          if (o.payload !== null) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(o.payload); }
          const response = await fetch(target.href, options);
          const text = await response.text();
          let body = null;
          try { body = JSON.parse(text); } catch (e) {}
          const type = response.headers.get('content-type') || '';
          // 阿里云 WAF 会把接口改投「滑动验证」HTML 页，status 仍是 200。
          const guarded = /text\\/html/i.test(type) || /aliyun_waf|滑动验证|访问验证/.test(text.slice(0, 4000));
          return {status: response.status, body, guarded, bytes: text.length};
        }'''
        args = {'path': path, 'user': str(api_user), 'method': method, 'payload': payload,
                'userHeader': self.provider.api_user_key, 'origin': self.provider.domain}
        for attempt in range(CONTEXT_RETRIES):
            # 间隔发送，避免短时间内连发接口被判定为自动化流量。
            gap = REQUEST_SPACING - (time.monotonic() - self._last_request)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                response = await self.page.evaluate(script, args)
                self._last_request = time.monotonic()
                return response
            except PlaywrightError as exc:
                # Only a navigation teardown is retried; a real API error must surface.
                if method != 'GET' or attempt == CONTEXT_RETRIES - 1 or 'Execution context was destroyed' not in str(exc):
                    raise
                await self._settle()

    async def __aexit__(self, *args):
        if self.browser:
            await self.browser.close()

    async def export_site_cookies(self):
        return site_cookies(await self.context.cookies([self.provider.domain]), self.provider)


class CheckinEngine:
    def __init__(self, reauthenticate=None):
        self.reauthenticate = reauthenticate

    async def run(self, row, credentials, previous_snapshot=False):
        start = time.monotonic()
        for attempt in range(len(RETRY_DELAYS) + 1):
            result = await self._run_once(row, credentials, previous_snapshot)
            retryable = result.pop('_retryable', False)
            if not retryable or attempt == len(RETRY_DELAYS):
                if attempt:
                    result['message'] += f' 连接阶段已自动重试 {attempt} 次。'
                result['duration_ms'] = int((time.monotonic() - start) * 1000)
                return result
            await asyncio.sleep(RETRY_DELAYS[attempt])

    async def _run_once(self, row, credentials, previous_snapshot=False):
        start = time.monotonic()
        result = {'status': 'failed', 'message': '签到失败。', 'duration_ms': 0, 'reward': None}
        provider = PROVIDERS.get(row['provider'])
        if not provider or not provider.active:
            return {**result, 'message': '站点配置不存在或已移除，请检查站点管理。'}
        # Only the read-only preparation may be restarted. OAuth has its own
        # retry boundary, so never repeat a login because a later read failed.
        may_retry = row['provider'] == 'agentrouter' and row['auth_mode'] == 'session'
        try:
            proxy = browser_proxy(credentials.get('proxy'))
            if row['auth_mode'] == 'password':
                login = await login_with_credentials(row['name'], provider, row['provider'], credentials['email'], credentials['password'],
                                                      **({'proxy': proxy} if proxy is not None else {}))
                if not login:
                    result.update(status='manual_required', message=('代理连接或网站登录未通过，请检查该账号的代理配置、账号密码或网站验证。'
                                  if proxy is not None and proxy['server'] != 'direct://' else
                                  '浏览器登录未通过，请检查账号密码或前往官网处理人机验证。'))
                    return result
                cookies, api_user = login.cookies, login.api_user
                if not api_user:
                    result.update(status='manual_required', message='登录后未获取到账号标识，请前往官网检查账号状态。')
                    return result
            else:
                cookies, api_user = {'session': credentials['session']}, credentials['api_user']
            options = {'site_cookie_cache': credentials['site_cookies']} if credentials.get('site_cookies') else {}
            if proxy is not None:
                options['proxy'] = proxy
            async with BrowserSession(provider, cookies, **options) as client:
                before = await self.user_info(client, provider, api_user)
                if before.get('waf'):
                    # 站点对自动化请求升级为滑块验证；重试不会更快通过，只会加深风控。
                    result.update(status='manual_required', message=(
                        '站点对本次后台请求弹出了「滑动验证」，未取得账号数据，本次签到未完成。'
                        '请稍后重试；若反复出现，请检查网络或代理，并在官网完成验证。'))
                    return result
                if before.get('error'):
                    if (before.get('status') == 401 and row['provider'] == 'agentrouter'
                            and row['auth_mode'] == 'session' and credentials.get('oauth_cookies')
                            and self.reauthenticate):
                        may_retry = False
                        await self._run_agentrouter_relogin(row, api_user, result, None, credentials)
                        return result
                    if before.get('status') in (401, 403):
                        hint = ('登录状态已失效，请点击账号行的「重新授权」重新登录。'
                                if row['auth_mode'] == 'session' else '账号验证未通过，请检查邮箱与密码后重试。')
                        result.update(status='manual_required', message=hint)
                    else:
                        result.update(status='failed', message=before['error'],
                                      _retryable=may_retry and transient_status(before.get('status')))
                    return result
                before_balance, before_total = before['balance'], before['total']
                if row['auth_mode'] == 'session' and hasattr(client, 'export_site_cookies'):
                    result['_session_update'] = {'site_cookies': await client.export_site_cookies()}
                if provider.sign_in_path:
                    response = await client.request(provider.sign_in_path, api_user, method='POST', payload={})
                    status, message = classify_response(response['status'], response['body'])
                    if provider.custom:
                        body = response.get('body')
                        if response.get('guarded'):
                            status, message = 'manual_required', '站点要求人工验证，请在浏览器中处理后重试。'
                        elif status == 'success':
                            confirmed = body.get('success') is True
                            if provider.template == 'sign_in' and 'success' not in body:
                                confirmed = ((type(body.get('ret')) is int and body['ret'] == 1)
                                             or (type(body.get('code')) is int and body['code'] == 0))
                            if not confirmed:
                                status, message = 'failed', '签到接口未明确确认成功，请检查接口配置。'
                        elif status == 'failed':
                            # Do not persist arbitrary responses that might echo credentials.
                            message = '签到未获确认，请检查签到路径、登录状态或站点兼容性。'
                    # Preserve the explicit check-in acknowledgement even when
                    # the subsequent balance request times out.
                    result.update(status=status, message=message, before_balance=before_balance)
                    try:
                        after = await self.user_info(client, provider, api_user)
                    except (httpx.HTTPError, TimeoutError, PlaywrightError):
                        after = {'error': '余额查询失败'}
                else:
                    # The deployed site's login response, not GET /user/self,
                    # carries checked_in. A successful GET only confirms a sync.
                    result.update(status='synced', before_balance=before_balance,
                                  after_balance=before_balance, used_quota=before['used_quota'],
                                  total_quota=before_total,
                                  message=('余额已同步，尚无本次签到凭据。AgentRouter 通过登录领取奖励。'
                                           if row['provider'] == 'agentrouter' else '余额已同步；此站点配置为仅同步余额，未执行签到。'))
                    if row['provider'] == 'agentrouter' and row['auth_mode'] == 'session':
                        may_retry = False
                        await self._run_agentrouter_relogin(row, api_user, result, before, credentials)
                    return result
                result.update(status=status, message=message, before_balance=before_balance)
                if after.get('error'):
                    result['message'] += ' 签到后余额未能读取，金额暂未确认；保留上次余额。'
                    return result
                result.update(after_balance=after['balance'], used_quota=after['used_quota'],
                              total_quota=after['total'])
                if status == 'success':
                    result['reward'] = max(0, round(after['total'] - before_total, 2))
                elif status == 'already_checked':
                    result['reward'] = 0
                if result['reward'] is not None and result['reward'] > 0:
                    unit = {'USD': '$', 'CNY': '¥', 'credits': '积分 '}.get(provider.currency, '')
                    result['message'] += f' 本次额度增加 {unit}{result["reward"]:.2f}。'
        except WAFCookiesUnavailable:
            result.update(status='manual_required', message=(
                'AgentRouter 未能取得站点所需的防护 Cookie（acw_tc），本次未发送账号请求。'
                '请检查代理和网络，或稍后重试。'))
        except (httpx.HTTPError, TimeoutError, PlaywrightTimeoutError) as exc:
            result.update(message='网络连接失败或站点页面加载超时，未确认签到结果；请检查网络或代理设置后重试。',
                          _retryable=may_retry and transient_error(exc))
        except Exception as exc:
            result.update(status='failed', _retryable=may_retry and transient_error(exc), message=proxy_failure_message(exc) or (
                f'浏览器执行异常（{type(exc).__name__}），未确认签到结果；'
                '请重试，若持续发生请导出日志反馈。'
            ))
        finally:
            result['duration_ms'] = int((time.monotonic() - start) * 1000)
        return result

    async def _run_agentrouter_relogin(self, row, api_user, result, before, credentials):
        """Reuse encrypted OAuth cookies in a hidden context; never log out a valid session."""
        if not self.reauthenticate or not credentials.get('oauth_cookies'):
            result['message'] += ' 请主动重新授权一次以保存自动登录会话；签到不会自动弹出登录窗口。'
            return
        try:
            fresh = await self.reauthenticate(row, api_user)
        except TimeoutError:
            fresh = None
        if not fresh or not isinstance(fresh.get('profile'), dict):
            result.update(status='failed' if fresh and fresh.get('retryable') else 'manual_required', message=(
                ('余额已同步，但' if before else '') +
                (fresh.get('message') if isinstance(fresh, dict) and fresh.get('message') else
                 '后台授权未完成，已保留缓存；尚不能判断登录会话是否过期。请检查站点状态或网络。')))
            return
        profile = fresh['profile']
        if str(profile.get('id')) != str(api_user):
            result.update(status='manual_required', message='重新登录的 AgentRouter 身份与原账号不同，未更新凭据。')
            return
        checked = profile.get('checked_in')
        result.update(status='success' if checked is True else 'synced',
                      message='AgentRouter 确认签到成功。' if checked is True else
                              'AgentRouter 本次登录未发放签到奖励。' if checked is False else
                              'AgentRouter 登录完成，但未返回签到确认标记。',
                      _session_update={**result.get('_session_update', {}),
                                       **({'site_cookies': fresh['site_cookies']} if fresh.get('site_cookies') else {}),
                                       **({'oauth_user_agent': fresh['oauth_user_agent']} if fresh.get('oauth_user_agent') else {}),
                                       'session': fresh['session'], 'api_user': str(api_user),
                                       'oauth_cookies': fresh.get('oauth_cookies', credentials['oauth_cookies'])})
        if fresh.get('attempts', 1) > 1:
            result['message'] += f' 授权阶段已自动重试 {fresh["attempts"] - 1} 次。'
        after = quota_snapshot(profile)
        if after is None:
            # Do not label the pre-login snapshot as the post-login balance.
            for key in ('after_balance', 'used_quota', 'total_quota'):
                result.pop(key, None)
            result['message'] += ' 签到后余额未能读取，金额暂未确认；保留上次余额。'
            return
        result.update(after_balance=after['balance'], used_quota=after['used_quota'], total_quota=after['total'])
        if checked is True:
            if before is None:
                result['message'] += ' 缺少签到前实时余额，奖励金额暂未确认。'
                return
            increase = round(after['total'] - before['total'], 2)
            result['reward'] = increase if increase >= 0 else None
            if increase > 0:
                result['message'] += f' 本次总额度增加 ${increase:.2f}。'
            elif increase == 0:
                result['message'] += ' 两次实时查询的总额度未增加，本次新增 $0.00（可能今日已领取）。'
            else:
                result['message'] += ' 总额度减少，本次奖励金额暂未确认。'
        elif checked is False:
            result['reward'] = 0

    async def user_info(self, client, provider, api_user):
        try:
            response = await client.request(provider.user_info_path, api_user)
            for retry in range(WAF_RETRIES):
                if not response.get('guarded'):
                    break
                # 风控窗口有衰减期，先静默等待再试一次；仍被拦就如实报告。
                await asyncio.sleep(WAF_COOLDOWN)
                response = await client.request(provider.user_info_path, api_user)
            if response.get('guarded'):
                # 200 + HTML 是 WAF 滑块挑战页，不是账号问题，也不是未登录。
                return {'status': 200, 'waf': True,
                        'error': '站点触发人机验证（滑动验证），未取得账号数据，未能执行签到。'}
            if response['status'] != 200:
                hint = '请稍后重试。' if transient_status(response['status']) else '请检查登录状态或站点访问限制。'
                return {'status': response['status'], 'error': f'无法读取账号信息（HTTP {response["status"]}），{hint}'}
            body = response['body']
            if not isinstance(body, dict) or body.get('success') is not True or not isinstance(body.get('data'), dict):
                return {'status': 200, 'error': '账号信息验证未通过，请检查凭据是否有效。'}
            data = body['data']
            snapshot = quota_snapshot(data, getattr(provider, 'quota_per_unit', 500000))
            if snapshot is None:
                return {'error': '账号余额字段缺失或格式异常，保留上次余额。'}
            if data.get('id') is not None and str(data['id']) != str(api_user):
                return {'error': '站点返回身份与保存账号不一致，未更新余额。'}
            return {**snapshot, 'api_user': data.get('id')}
        except (ValueError, KeyError, TypeError):
            return {'error': '账号信息格式异常，未执行重复请求。'}
