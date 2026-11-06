"""已实现适配器及其登录能力的唯一注册表。"""
from dataclasses import dataclass
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    custom: bool = False
    active: bool = True
    template: str = 'sign_in'
    quota_per_unit: float = 500000
    currency: str = 'USD'
    login_api_path: str = '/api/user/login'


class CustomProviderIn(BaseModel):
    """Declarative New API compatibility only; never executable scripts or secrets."""
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    domain: str = Field(max_length=250)
    template: Literal['newapi', 'sign_in', 'balance', 'sub2api', 'sub2api_balance'] = 'newapi'
    login_path: str = Field(default='/login', max_length=200)
    sign_in_path: str | None = Field(default=None, max_length=200)
    user_info_path: str | None = Field(default=None, max_length=200)
    login_api_path: str | None = Field(default=None, max_length=200)
    api_user_key: str = Field(default='new-api-user', max_length=64)
    quota_per_unit: float = Field(default=500000, ge=1, le=1e12, allow_inf_nan=False)
    currency: Literal['USD', 'CNY', 'credits'] = 'USD'

    @field_validator('name')
    @classmethod
    def validate_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('请填写有效站点名称')
        return value

    @field_validator('domain')
    @classmethod
    def validate_domain(cls, value):
        value = value.strip()
        if re.search(r'[\s\\\x00-\x1f\x7f]', value):
            raise ValueError('网址包含无效字符')
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or '%' in parsed.netloc):
            raise ValueError('请填写 HTTPS 根网址，不含账号、路径、参数或片段')
        host = parsed.hostname.encode('idna').decode().lower()
        if not re.fullmatch(r'[a-z0-9.-]+', host) or '..' in host or host.startswith('.'):
            raise ValueError('域名无效')
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError('端口无效')
        return 'https://' + host.rstrip('.') + (f':{port}' if port and port != 443 else '')

    @field_validator('login_path', 'sign_in_path', 'user_info_path', 'login_api_path')
    @classmethod
    def validate_path(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not re.fullmatch(r'/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]*', value):
            raise ValueError('接口必须是以 / 开头的站内路径，不含参数或跳转地址')
        return value

    @field_validator('api_user_key')
    @classmethod
    def validate_header(cls, value):
        value = value.strip().lower()
        if (not re.fullmatch(r'[a-z][a-z0-9-]*', value)
                or value in {'cookie', 'authorization', 'host', 'origin', 'referer', 'content-type',
                             'content-length', 'connection', 'x-requested-with', 'accept', 'user-agent'}
                or value.startswith(('sec-', 'proxy-'))):
            raise ValueError('用户标识请求头无效')
        return value

    @model_validator(mode='after')
    def apply_template(self):
        sub2api = self.template.startswith('sub2api')
        self.user_info_path = self.user_info_path or ('/api/v1/auth/me' if sub2api else '/api/user/self')
        self.login_api_path = self.login_api_path or ('/api/v1/auth/login' if sub2api else '/api/user/login')
        if sub2api and 'quota_per_unit' not in self.model_fields_set:
            self.quota_per_unit = 1
        if self.template in ('balance', 'sub2api_balance'):
            self.sign_in_path = None
        elif self.sign_in_path is None:
            self.sign_in_path = ('/api/v1/user/check-in' if sub2api else
                                 '/api/user/checkin' if self.template == 'newapi' else '/api/user/sign_in')
        return self

    def provider(self, provider_id, *, active=True):
        values = self.model_dump(exclude={'name'})
        return Provider(name=provider_id, display_name=self.name, custom=True, active=active,
                        login_methods=BROWSER_METHODS + CREDENTIAL_METHODS, **values)


PROVIDERS = {
    'anyrouter': Provider(name='anyrouter', display_name='AnyRouter', domain='https://anyrouter.top',
        bypass_method='waf_cookies', waf_cookie_names=['acw_tc', 'cdn_sec_tc', 'acw_sc__v2'], persist_profile=False,
        oauth_navigation_paths=('/register', '/login')),
    'agentrouter': Provider(name='agentrouter', display_name='AgentRouter', domain='https://agentrouter.org',
        sign_in_path=None, logout_path='/api/user/logout', bypass_method='waf_cookies', waf_cookie_names=['acw_tc'], use_proxy=True, persist_profile=False),
}
BUILTIN_IDS = frozenset(PROVIDERS)


def load_custom_providers(configs):
    """Replace the custom registry in place so all consumers share the same mapping."""
    custom = {item['id']: CustomProviderIn(**item['config']).provider(item['id'], active=item['active'])
              for item in configs if item['id'] not in BUILTIN_IDS}
    for key in tuple(PROVIDERS):
        if key not in BUILTIN_IDS:
            del PROVIDERS[key]
    PROVIDERS.update(custom)


def provider_catalog():
    return [{'id': key, 'name': provider.display_name or provider.name,
             'domain': provider.domain, 'custom': provider.custom, 'active': provider.active,
             'template': provider.template, 'currency': provider.currency,
             'quota_per_unit': provider.quota_per_unit, 'login_path': provider.login_path,
             'sign_in_path': provider.sign_in_path, 'user_info_path': provider.user_info_path,
             'api_user_key': provider.api_user_key,
             'login_api_path': provider.login_api_path,
             'login_methods': list(provider.login_methods),
             'oauth_availability': provider.oauth_availability}
            for key, provider in PROVIDERS.items()]


def supports(provider_id, method):
    provider = PROVIDERS.get(provider_id)
    return provider is not None and provider.active and method in provider.login_methods
