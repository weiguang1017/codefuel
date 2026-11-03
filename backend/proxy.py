"""Account-owned proxy configuration. Credentials never appear in public account data."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


class ProxyConfig(BaseModel):
    mode: Literal['default', 'direct', 'custom'] = 'default'
    server: str = Field(default='', max_length=2048)
    authenticate: bool = False
    username: str | None = Field(default=None, max_length=300, repr=False)
    password: str | None = Field(default=None, max_length=1000, repr=False)

    @model_validator(mode='after')
    def validate_proxy(self):
        if self.mode != 'custom':
            return self
        try:
            parsed = urlsplit(self.server.strip())
            port = parsed.port
            host = parsed.hostname
            if (parsed.scheme not in ('http', 'https', 'socks5') or not host or not port
                    or '\\' in self.server
                    or any(c.isspace() or ord(c) < 32 for c in self.server)
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                raise ValueError()
            host = f'[{host}]' if ':' in host else host.encode('idna').decode()
            self.server = f'{parsed.scheme}://{host}:{port}'
        except (ValueError, UnicodeError):
            raise ValueError('代理地址需为 http://主机:端口、https://主机:端口 或 socks5://主机:端口；认证信息请单独填写。') from None
        if parsed.scheme == 'socks5' and self.authenticate:
            raise ValueError('当前系统 Chrome 不支持 SOCKS5 用户名密码认证，请使用 HTTP/HTTPS 认证代理。')
        return self


def normalize_proxy(value, previous=None):
    if value is None:
        return previous or {'mode': 'default'}
    config = value if isinstance(value, ProxyConfig) else ProxyConfig.model_validate(value)
    if config.mode != 'custom':
        return {'mode': config.mode}
    result = {'mode': 'custom', 'server': config.server, 'authenticate': config.authenticate}
    if config.authenticate:
        prior = previous or {}
        same_server = prior.get('mode') == 'custom' and prior.get('server') == config.server
        for key in ('username', 'password'):
            value = getattr(config, key)
            if value is None and same_server:
                value = prior.get(key)
            if not value:
                raise ValueError('请填写代理用户名和密码；修改代理地址后需重新填写认证信息。')
            result[key] = value
    return result


def browser_proxy(config):
    """None keeps legacy routing; launchers translate the direct:// sentinel to --no-proxy-server."""
    config = config or {}
    if config.get('mode', 'default') == 'default':
        return None
    if config['mode'] == 'direct':
        return {'server': 'direct://'}
    return {'server': config['server'], **({key: config[key] for key in ('username', 'password')}
                                         if config.get('authenticate') else {})}


def public_proxy(config):
    config = config or {'mode': 'default'}
    return {'mode': config['mode'], 'server': config.get('server', ''),
            'has_auth': bool(config.get('authenticate'))}


def proxy_failure_message(error):
    text = str(error)
    if any(code in text for code in ('ERR_INVALID_AUTH_CREDENTIALS', 'ERR_PROXY_AUTH_', 'ERR_PROXY_CERTIFICATE_INVALID')):
        return '代理认证或证书验证未通过，请检查该账号的代理配置。'
    if any(code in text for code in ('ERR_PROXY_CONNECTION_FAILED', 'ERR_TUNNEL_CONNECTION_FAILED', 'ERR_NO_SUPPORTED_PROXIES')):
        return '代理连接失败，请检查该账号的代理地址、端口和代理服务是否可用。'
    return None
