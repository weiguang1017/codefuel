"""Read-only gateway detection, independent of the user's current template selection."""
import asyncio
import json
import math
from urllib.parse import urlsplit

import httpx

from .engine import browser_available, launch_async
from .providers import CustomProviderIn

PUBLIC_PATHS = ('/api/status', '/api/v1/settings/public')
_browser_slots = asyncio.Semaphore(1)
UNKNOWN = {'recognized': False, 'message': '未能自动识别网站，请手动选择签到模式；也可以稍后重新检测。'}


def classify_config(domain, path, body, *, legacy_hint=False):
    if not isinstance(body, dict) or not isinstance(body.get('data'), dict):
        return None
    config = body['data']
    if path == '/api/status' and body.get('success') is True and isinstance(config.get('system_name'), str):
        protocol = 'newapi'
        enabled = config.get('checkin_enabled')
        legacy = config.get('sign_in_enabled') is True or config.get('sign_in_path') == '/api/user/sign_in' or legacy_hint
        template = 'newapi' if enabled is True else 'sign_in' if legacy else 'balance' if enabled is False else 'newapi'
    elif (path == '/api/v1/settings/public' and type(body.get('code')) is int and body['code'] == 0
          and isinstance(config.get('site_name'), str)
          and any(key in config for key in ('linuxdo_oauth_enabled', 'registration_enabled', 'check_in_enabled'))):
        protocol = 'sub2api'
        enabled = config.get('check_in_enabled')
        template = 'balance' if enabled is False else 'sub2api'
    else:
        return None
    internal = 'sub2api_balance' if template == 'balance' and protocol == 'sub2api' else template
    defaults = CustomProviderIn(name='探测', domain=domain, template=internal).model_dump()
    quota = config.get('quota_per_unit')
    if protocol == 'newapi' and isinstance(quota, (int, float)) and not isinstance(quota, bool) and math.isfinite(quota) and 1 <= quota <= 1e12:
        defaults['quota_per_unit'] = quota
    defaults = {key: defaults[key] for key in ('login_path', 'login_api_path', 'user_info_path', 'sign_in_path', 'api_user_key', 'quota_per_unit')}
    name = {'newapi': '标准 New API 签到', 'sub2api': 'Sub2API 签到', 'sign_in': 'AnyRouter 兼容签到', 'balance': '仅同步余额'}[template]
    note = '站点已关闭签到。' if enabled is False and template == 'balance' else '签到能力将在登录后验证。' if enabled is not True else '已读取到站点开放签到的配置。'
    return {'recognized': True, 'protocol': protocol, 'template': template,
            'checkin_enabled': enabled if isinstance(enabled, bool) else None, 'defaults': defaults,
            'message': f'已识别为{name}。{note}'}


def choose_result(results):
    matches = [result for result in results if result and result.get('recognized')]
    if len({result['protocol'] for result in matches}) > 1:
        return {'recognized': False, 'message': '此网址提供多种接口类型，请手动选择要使用的签到模式。'}
    return matches[0] if matches else None


async def _read_public(client, domain, path):
    try:
        async with client.stream('GET', domain + path) as response:
            if response.status_code != 200 or 'json' not in response.headers.get('content-type', '').lower():
                return None
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 262144:
                    return None
        return json.loads(raw)
    except (httpx.HTTPError, ValueError, UnicodeError):
        return None


async def _browser_config(domain):
    """Use a fresh, headless context; never restore account credentials or click login/check-in."""
    browser = None
    try:
        async with asyncio.timeout(20), _browser_slots:
            browser = await launch_async(headless=True, args=['--no-proxy-server'])
            context = await browser.new_context(service_workers='block')

            async def read_only(route):
                # Public configuration never needs a state-changing business API request.
                request = route.request
                if request.method not in ('GET', 'HEAD') and not urlsplit(request.url).path.startswith('/cdn-cgi/'):
                    await route.abort()
                else:
                    await route.fallback()

            await context.route('**/*', read_only)
            page = await context.new_page()
            await page.goto(domain + '/login', wait_until='domcontentloaded', timeout=12000)
            if urlsplit(page.url).netloc != urlsplit(domain).netloc or urlsplit(page.url).scheme != 'https':
                return []
            # Wait for the site's own configuration request instead of racing it during app startup.
            try:
                await page.wait_for_function('''() => {
                    if (window.__APP_CONFIG__?.site_name) return true;
                    try { return !!JSON.parse(localStorage.getItem('status') || '{}').system_name; } catch (_) { return false; }
                }''', timeout=6000)
            except Exception:
                pass
            data = await page.evaluate('''async origin => {
                if (location.origin !== origin) return null;
                let status = null;
                try { status = JSON.parse(localStorage.getItem('status') || 'null'); } catch (_) {}
                let settings = window.__APP_CONFIG__ || null;
                const read = async path => {
                    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 4000);
                    try {
                        const r = await fetch(path, {credentials:'same-origin', redirect:'error', signal:controller.signal});
                        if (!r.ok || !(r.headers.get('content-type') || '').includes('json')) return null;
                        const text = await r.text();return text.length <= 262144 ? JSON.parse(text) : null;
                    } catch (_) { return null; } finally { clearTimeout(timer); }
                };
                const responses = await Promise.all([
                    status ? Promise.resolve({success:true,data:status}) : read('/api/status'),
                    settings ? Promise.resolve({code:0,data:settings}) : read('/api/v1/settings/public')
                ]);
                let legacy = false;
                if (responses[0]?.success === true && responses[0]?.data?.checkin_enabled !== true) {
                    const scripts = [...document.scripts].map(s=>s.src).filter(src=>src && new URL(src).origin === origin).slice(0,3);
                    for (const src of scripts) {
                        const controller = new AbortController(), timer = setTimeout(()=>controller.abort(), 2500);
                        try {
                            const response = await fetch(src, {credentials:'same-origin',redirect:'error',signal:controller.signal});
                            const text = response.ok ? await response.text() : '';
                            if (text.length <= 4000000 && /['"`]\\/(?:api\\/)?user\\/sign_in['"`]/.test(text)) { legacy=true;break; }
                        } catch (_) {} finally { clearTimeout(timer); }
                    }
                }
                return location.origin === origin ? {responses,legacy} : null;
            }''', domain)
            if not data:
                return []
            return [classify_config(domain, path, body, legacy_hint=bool(data.get('legacy')))
                    for path, body in zip(PUBLIC_PATHS, data['responses'])]
    except Exception:
        return []
    finally:
        if browser:
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
            except Exception:
                pass


async def probe_site(domain):
    direct = []
    try:
        async with asyncio.timeout(8), httpx.AsyncClient(timeout=6, follow_redirects=False, trust_env=False) as client:
            bodies = await asyncio.gather(*(_read_public(client, domain, path) for path in PUBLIC_PATHS))
            direct = [classify_config(domain, path, body) for path, body in zip(PUBLIC_PATHS, bodies)]
    except (httpx.HTTPError, TimeoutError):
        pass
    result = choose_result(direct)
    if result and (not result['recognized'] or result['protocol'] == 'sub2api'
                   or result['checkin_enabled'] is not None or result['template'] == 'sign_in'):
        return result
    if browser_available():
        browser_result = choose_result(await _browser_config(domain))
        if browser_result:
            return browser_result
    return result or dict(UNKNOWN)
