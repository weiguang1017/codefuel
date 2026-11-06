"""Locate a site's actual login page using its own visible links."""
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .providers import CustomProviderIn, PROVIDERS

LOGIN_PATHS = ('/login', '/sign-in', '/signin', '/sign_in', '/auth/login')


async def discover_login_page(page, provider, store):
    if not provider.custom:
        return provider
    try:
        missing = await page.evaluate("() => [...document.querySelectorAll('h1,h2')].some(n=>/^(404|page not found|not found)$/i.test(n.textContent.trim()))")
        if missing:
            await page.goto(provider.domain, wait_until='domcontentloaded', timeout=15000)
            handle = await page.wait_for_function(r'''o => {
                if(location.origin!==new URL(o.origin).origin)return false;
                for(const a of document.querySelectorAll('a[href]')) {
                    const url=new URL(a.href);const r=a.getBoundingClientRect();
                    if(url.origin===location.origin && o.paths.includes(url.pathname.replace(/\/$/,'')) && r.width>0 && r.height>0)return url.pathname;
                }
                return false;
            }''', arg={'origin':provider.domain,'paths':list(LOGIN_PATHS)}, timeout=8000)
            try:
                path = await handle.json_value()
            finally:
                await handle.dispose()
            await page.goto(provider.domain + path, wait_until='domcontentloaded', timeout=15000)
        location = urlsplit(page.url)
        if (location.scheme, location.netloc) != (urlsplit(provider.domain).scheme, urlsplit(provider.domain).netloc):
            return provider
        path = location.path.rstrip('/')
        if path not in LOGIN_PATHS or path == provider.login_path or not hasattr(store, 'provider_configs'):
            return provider
        with store.lock:
            record = next((r for r in store.provider_configs() if r['id'] == provider.name), None)
            if not record:
                return provider
            config = CustomProviderIn(**{**record['config'], 'login_path': path})
            resolved = config.provider(provider.name, active=provider.active)
            store.save_provider(provider.name, config.model_dump(), active=provider.active)
            PROVIDERS[provider.name] = resolved
            return resolved
    except PlaywrightError:
        return provider
