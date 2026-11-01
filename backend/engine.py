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
from .upstream_imports import get_playwright_proxy, login_with_credentials, prepare_browser_page

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
    def __init__(self, provider, cookies):
        self.provider = provider
        self.cookies = cookies
        self.browser = None
        self._last_request = 0.0

    async def __aenter__(self):
        from urllib.parse import urlparse
        launch_kwargs = {'headless': True}
        proxy = get_playwright_proxy(use_proxy=self.provider.use_proxy)
        if proxy:
            launch_kwargs['proxy'] = proxy
        self.browser = await launch_async(**launch_kwargs)
        context = await self.browser.new_context()
        host = urlparse(self.provider.domain).hostname
        self.page = await context.new_page()
        await prepare_browser_page(self.page)
        # Let the site issue its own WAF cookies before the account session is added.
        await self._goto(self.provider.domain)
        await context.add_cookies([
            {'name': name, 'value': value, 'domain': host, 'path': '/'}
            for name, value in self.cookies.items()
        ])
        # No dedicated sign-in endpoint: avoid loading AgentRouter's authenticated SPA,
        # which would issue several background API calls before the single self query.
        if self.provider.sign_in_path:
            await self._goto(self.provider.domain + self.provider.login_path)
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
          const headers = {'new-api-user': o.user, 'X-Requested-With': 'XMLHttpRequest'};
          const options = {method: o.method, headers, credentials: 'include'};
          if (o.payload !== null) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(o.payload); }
          const response = await fetch(o.path, options);
          const text = await response.text();
          let body = null;
          try { body = JSON.parse(text); } catch (e) {}
          const type = response.headers.get('content-type') || '';
          // 阿里云 WAF 会把接口改投「滑动验证」HTML 页，status 仍是 200。
          const guarded = /text\\/html/i.test(type) || /aliyun_waf|滑动验证|访问验证/.test(text.slice(0, 4000));
          return {status: response.status, body, guarded, bytes: text.length};
        }'''
        args = {'path': path, 'user': str(api_user), 'method': method, 'payload': payload}
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
                if attempt == CONTEXT_RETRIES - 1 or 'Execution context was destroyed' not in str(exc):
                    raise
                await self._settle()

    async def __aexit__(self, *args):
        if self.browser:
            await self.browser.close()


class CheckinEngine:
    async def run(self, row, credentials, previous_snapshot=False):
        start = time.monotonic()
        provider = PROVIDERS[row['provider']]
        result = {'status': 'failed', 'message': '签到失败。', 'duration_ms': 0}
        try:
            if row['auth_mode'] == 'password':
                login = await login_with_credentials(row['name'], provider, row['provider'], credentials['email'], credentials['password'])
                if not login:
                    result.update(status='manual_required', message='浏览器登录未通过，请检查账号密码或前往官网处理人机验证。')
                    return result
                cookies, api_user = login.cookies, login.api_user
                if not api_user:
                    result.update(status='manual_required', message='登录后未获取到账号标识，请前往官网检查账号状态。')
                    return result
            else:
                cookies, api_user = {'session': credentials['session']}, credentials['api_user']
            async with BrowserSession(provider, cookies) as client:
                before = await self.user_info(client, provider, api_user)
                if before.get('waf'):
                    # 站点对自动化请求升级为滑块验证；重试不会更快通过，只会加深风控。
                    result.update(status='manual_required', message=(
                        '站点对本次后台请求弹出了「滑动验证」，未取得账号数据，本次签到未完成。'
                        '请稍后重试；若反复出现，请检查网络或代理，并在官网完成验证。'))
                    return result
                if before.get('error'):
                    if before.get('status') in (401, 403):
                        hint = ('登录状态已失效，请点击账号行的「重新授权」重新登录。'
                                if row['auth_mode'] == 'session' else '账号验证未通过，请检查邮箱与密码后重试。')
                        result.update(status='manual_required', message=hint)
                    else:
                        result.update(status='failed', message=before['error'])
                    return result
                before_balance, before_total = before['balance'], before['total']
                if provider.sign_in_path:
                    response = await client.request(provider.sign_in_path, api_user, method='POST', payload={})
                    status, message = classify_response(response['status'], response['body'])
                    after = await self.user_info(client, provider, api_user)
                else:
                    after = before
                    observed = round(before_total - row['total_quota'], 2) if previous_snapshot and row['total_quota'] is not None else None
                    status = 'synced'
                    message = ('AgentRouter 已通过用户信息接口同步签到状态；该站点没有独立签到接口，'
                               '额度变化仅作为观察值，不记为签到奖励。')
                    if observed is not None and observed > 0:
                        message += f' 较上次快照增加 ${observed:.2f}。'
                result.update(status=status, message=message, before_balance=before_balance,
                    after_balance=after.get('balance', before_balance), used_quota=after.get('used_quota', before.get('used_quota')))
                after_total = after.get('total', before_total)
                result['total_quota'] = after_total
                result['reward'] = round(after_total - before_total, 2) if provider.sign_in_path else None  # noqa: F821
                if result['reward'] is not None and result['reward'] > 0 and provider.sign_in_path:
                    result['message'] += f' 本次额度增加 ${result["reward"]:.2f}。'
        except (httpx.HTTPError, TimeoutError, PlaywrightTimeoutError):
            result.update(message='网络连接失败或站点页面加载超时，未确认签到结果；请检查网络或代理设置后重试。')
        except Exception as exc:
            result.update(status='failed', message=(
                f'浏览器执行异常（{type(exc).__name__}），未确认签到结果；'
                '请重试，若持续发生请导出日志反馈。'
            ))
        finally:
            result['duration_ms'] = int((time.monotonic() - start) * 1000)
        return result

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
                return {'status': response['status'], 'error': f'无法读取账号信息（HTTP {response["status"]}），请重新登录。'}
            body = response['body']
            if not isinstance(body, dict) or body.get('success') is not True or not isinstance(body.get('data'), dict):
                return {'status': 200, 'error': '账号信息验证未通过，请检查凭据是否有效。'}
            data = body['data']
            quota = round(data.get('quota', 0) / 500000, 2)
            used = round(data.get('used_quota', 0) / 500000, 2)
            return {'balance': quota, 'used_quota': used, 'total': round(quota + used, 2), 'api_user': data.get('id')}
        except (ValueError, KeyError, TypeError):
            return {'error': '账号信息格式异常，未执行重复请求。'}
