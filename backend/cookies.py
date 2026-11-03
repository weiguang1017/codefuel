"""Keep only unexpired first-party cookies issued for this provider."""
import time
from urllib.parse import urlparse


def site_cookies(cookies, provider):
    host = urlparse(provider.domain).hostname
    names = set(provider.waf_cookie_names or [])
    result = []
    for cookie in cookies:
        domain = (cookie.get('domain') or '').lstrip('.').lower()
        if (cookie.get('name') not in names or not cookie.get('value')
                or domain != host):
            continue
        if cookie.get('expires', -1) > 0 and cookie['expires'] <= time.time():
            continue
        result.append({k: v for k, v in cookie.items() if k in
                       ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite')})
    return result
