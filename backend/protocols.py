"""New API and Sub2API identity/amount contracts. Only same-site, verified profiles are accepted."""
import math

from playwright.async_api import Error as PlaywrightError

from .providers import PROVIDERS, CustomProviderIn
from .quota import quota_snapshot


class ProtocolMismatch(RuntimeError):
    pass


def is_sub2api(provider):
    return getattr(provider, 'template', '').startswith('sub2api')


def response_data(body, provider):
    if not isinstance(body, dict):
        return None
    success = (type(body.get('code')) is int and body['code'] == 0) if is_sub2api(provider) else body.get('success') is True
    return body.get('data') if success and isinstance(body.get('data'), dict) else None


def profile_snapshot(profile, provider):
    if not is_sub2api(provider):
        return quota_snapshot(profile, getattr(provider, 'quota_per_unit', 500000))
    balance = profile.get('balance') if isinstance(profile, dict) else None
    if isinstance(balance, bool) or not isinstance(balance, (int, float)) or not math.isfinite(balance):
        return None
    # Sub2API exposes a balance, not New API's cumulative quota/usage counters.
    return {'balance': round(balance, 2), 'used_quota': None, 'total': None}


async def resolve_custom_protocol(page, provider, store):
    if not provider.custom or is_sub2api(provider) or not hasattr(store, 'provider_configs'):
        return provider
    try:
        handle = await page.wait_for_function('''origin => {
          if (location.origin !== new URL(origin).origin) return false;
          const config = window.__APP_CONFIG__;
          if (config && typeof config.site_name === 'string' && typeof config.linuxdo_oauth_enabled === 'boolean') return 'sub2api';
          try { const s = JSON.parse(localStorage.getItem('status') || '{}');
            if (s.system_name || typeof s.linuxdo_oauth === 'boolean') return 'newapi';
          } catch (_) {}
          return false;
        }''', arg=provider.domain, timeout=12000)
        try:
            protocol = await handle.json_value()
        finally:
            await handle.dispose()
    except (PlaywrightError, AttributeError):
        return provider
    if protocol != 'sub2api':
        return provider
    with store.lock:
        record = next((r for r in store.provider_configs() if r['id'] == provider.name), None)
        if not record:
            return provider
        current = PROVIDERS[provider.name]
        if is_sub2api(current):
            return current
        customized = (provider.user_info_path != '/api/user/self' or provider.login_api_path != '/api/user/login'
                      or provider.sign_in_path not in (None, '/api/user/checkin', '/api/user/sign_in')
                      or provider.currency != 'USD' or provider.quota_per_unit != 500000)
        recorded_money = (any(r['provider'] == provider.name and r['has_snapshot'] for r in store.accounts())
                          or store.db.execute('SELECT 1 FROM runs WHERE provider=? AND (before_balance IS NOT NULL OR after_balance IS NOT NULL) LIMIT 1',
                                              (provider.name,)).fetchone())
        if customized or recorded_money:
            raise ProtocolMismatch('已识别为 Sub2API 站点；请在站点管理中选择 Sub2API 模板并核对接口，再重新添加或授权账号。')
        config = {**record['config'], 'template': 'sub2api_balance' if provider.sign_in_path is None else 'sub2api',
                  'user_info_path': '/api/v1/auth/me', 'login_api_path': '/api/v1/auth/login',
                  'sign_in_path': None if provider.sign_in_path is None else '/api/v1/user/check-in', 'quota_per_unit': 1}
        parsed = CustomProviderIn(**config)
        resolved = parsed.provider(provider.name)
        store.save_provider(provider.name, parsed.model_dump())
        PROVIDERS[provider.name] = resolved
        return resolved
