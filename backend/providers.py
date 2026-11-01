"""已实现适配器及其登录能力的唯一注册表。"""
from dataclasses import dataclass

from .upstream_imports import ProviderConfig


BROWSER_METHODS = ('github', 'linuxdo', 'manual')
CREDENTIAL_METHODS = ('password', 'session')


@dataclass
class Provider(ProviderConfig):
    display_name: str = ''
    login_methods: tuple[str, ...] = BROWSER_METHODS + CREDENTIAL_METHODS
    oauth_availability: str = 'detect_on_login'
    # 仅导航页面已有的链接，不提交注册表单或拼接 OAuth 请求。
    oauth_navigation_paths: tuple[str, ...] = ()
    logout_path: str | None = None


PROVIDERS = {
    'anyrouter': Provider(name='anyrouter', display_name='AnyRouter', domain='https://anyrouter.top',
        bypass_method='waf_cookies', waf_cookie_names=['acw_tc', 'cdn_sec_tc', 'acw_sc__v2'], persist_profile=False,
        oauth_navigation_paths=('/register', '/login')),
    'agentrouter': Provider(name='agentrouter', display_name='AgentRouter', domain='https://agentrouter.org',
        sign_in_path=None, logout_path='/api/user/logout', bypass_method='waf_cookies', waf_cookie_names=['acw_tc'], use_proxy=True, persist_profile=False),
}


def provider_catalog():
    return [{'id': key, 'name': provider.display_name or provider.name,
             'login_methods': list(provider.login_methods),
             'oauth_availability': provider.oauth_availability}
            for key, provider in PROVIDERS.items()]


def supports(provider_id, method):
    provider = PROVIDERS.get(provider_id)
    return provider is not None and method in provider.login_methods
