"""Check-in adapters derived from millylee/anyrouter-check-in (BSD-2-Clause).
No browser profile is persisted: account secrets stay in the encrypted database.
"""
import os
import sys
import time
from pathlib import Path
import httpx

from .upstream_imports import ProviderConfig, login_with_credentials, prepare_browser_page, wait_for_waf_ready


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

PROVIDERS = {
    'anyrouter': ProviderConfig(name='anyrouter', domain='https://anyrouter.top',
        bypass_method='waf_cookies', waf_cookie_names=['acw_tc', 'cdn_sec_tc', 'acw_sc__v2'], persist_profile=False),
    'agentrouter': ProviderConfig(name='agentrouter', domain='https://agentrouter.org',
        sign_in_path=None, bypass_method='waf_cookies', waf_cookie_names=['acw_tc'], use_proxy=False, persist_profile=False),
}
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

    async def __aenter__(self):
        from urllib.parse import urlparse
        self.browser = await launch_async(headless=True)
        context = await self.browser.new_context()
        host = urlparse(self.provider.domain).hostname
        await context.add_cookies([{'name': name, 'value': value, 'domain': host, 'path': '/'} for name, value in self.cookies.items()])
        self.page = await context.new_page()
        await prepare_browser_page(self.page)
        await self.page.goto(self.provider.domain + self.provider.login_path, wait_until='domcontentloaded')
        await wait_for_waf_ready(self.page)
        return self

    async def request(self, path, api_user, method='GET', payload=None):
        script = '''async (o) => {
          const headers = {'new-api-user': o.user, 'X-Requested-With': 'XMLHttpRequest'};
          const options = {method: o.method, headers, credentials: 'include'};
          if (o.payload !== null) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(o.payload); }
          const response = await fetch(o.path, options);
          const text = await response.text();
          let body = null;
          try { body = JSON.parse(text); } catch (e) {}
          return {status: response.status, body};
        }'''
        return await self.page.evaluate(script, {'path': path, 'user': str(api_user), 'method': method, 'payload': payload})

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
                    # AgentRouter has no sign-in endpoint: the first self query may credit it.
                    after = before
                    observed = round(before_total - row['total_quota'], 2) if previous_snapshot and row['total_quota'] is not None else None
                    if observed is not None and observed > 0:
                        status, message = 'synced', f'AgentRouter 状态已同步，较上次快照增加 ${observed:.2f}；该变化仅作为额度观察值。'
                    else:
                        status, message = 'synced', 'AgentRouter 状态已同步；查询成功不等于本次获得额度。'
                result.update(status=status, message=message, before_balance=before_balance,
                    after_balance=after.get('balance', before_balance), used_quota=after.get('used_quota', before.get('used_quota')))
                after_total = after.get('total', before_total)
                result['total_quota'] = after_total
                result['reward'] = round(after_total - before_total, 2) if provider.sign_in_path else (observed if observed is not None and observed > 0 else None)  # noqa: F821
                if result['reward'] is not None and result['reward'] > 0 and provider.sign_in_path:
                    result['message'] += f' 本次额度增加 ${result["reward"]:.2f}。'
        except (httpx.HTTPError, TimeoutError):
            result.update(message='网络连接失败，请检查网络或代理设置后重试。')
        except Exception:
            result.update(status='manual_required', message='浏览器或站点验证未完成，请检查凭据与官网人机验证。')
        finally:
            result['duration_ms'] = int((time.monotonic() - start) * 1000)
        return result

    async def user_info(self, client, provider, api_user):
        try:
            response = await client.request(provider.user_info_path, api_user)
            if response['status'] != 200:
                return {'status': response['status'], 'error': f'无法读取账号信息（HTTP {response["status"]}），请重新登录。'}
            body = response['body']
            if body.get('success') is not True or not isinstance(body.get('data'), dict):
                return {'error': '账号信息验证未通过，请检查凭据是否有效。'}
            data = body['data']
            quota = round(data.get('quota', 0) / 500000, 2)
            used = round(data.get('used_quota', 0) / 500000, 2)
            return {'balance': quota, 'used_quota': used, 'total': round(quota + used, 2)}
        except (ValueError, KeyError, TypeError):
            return {'error': '账号信息格式异常，未执行重复请求。'}
