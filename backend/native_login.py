"""Human-operated Chrome authorization with short target-site-only automation phases.

The temporary profile belongs to this login, never to the user's daily Chrome.
While the human signs in upstream, only the loopback tab list is read: no page
scripting, automation connection, fingerprint overrides or challenge interaction.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, quote

import httpx
from playwright.async_api import async_playwright, Error as PlaywrightError

POLL_INTERVAL = 1.0
RETURN_SETTLE = 2.0


def has_challenge(pages):
    return any(any(word in page.title.lower() for word in ('just a moment', '请稍候', '请稍等', 'attention required', 'checking your browser')) for page in pages)


class NativeChrome:
    def __init__(self, executable, url, proxy=None):
        self.executable, self.url, self.proxy = executable, url, proxy
        self.process = self.profile = self.client = None
        self.endpoint = None
        self.proxy_auth_required = False

    def proxy_args(self):
        if not self.proxy:
            return []
        server = self.proxy['server']
        if server == 'direct://':
            return ['--no-proxy-server']
        parsed = urlsplit(server)
        if parsed.scheme not in ('http', 'https', 'socks5') or not parsed.hostname or not parsed.port:
            raise ValueError('Invalid proxy configuration')
        # Chrome's own proxy-auth prompt handles authentication. Never put the
        # configured username/password on the process command line or on disk.
        self.proxy_auth_required = bool(parsed.username or self.proxy.get('username'))
        host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
        return [f'--proxy-server={parsed.scheme}://{host}:{parsed.port}']

    async def __aenter__(self):
        try:
            if not self.executable or not Path(self.executable).is_file():
                raise FileNotFoundError('Google Chrome is required')
            proxy_args = self.proxy_args()
            self.profile = tempfile.TemporaryDirectory(prefix='codefuel-login-')
            # A nonzero port preserves Chrome's native behavior. Port 0 enables
            # its automation mode even when no automation client is attached.
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            self.endpoint = f'http://127.0.0.1:{port}'
            self.process = await asyncio.create_subprocess_exec(
                self.executable, f'--user-data-dir={self.profile.name}',
                f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
                '--no-first-run', '--no-default-browser-check', *proxy_args, self.url,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.client = httpx.AsyncClient(base_url=self.endpoint, trust_env=False, timeout=2)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and self.process.returncode is None:
                try:
                    await self.tabs()
                    return self
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(.2)
            raise RuntimeError('Chrome did not start')
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def tabs(self):
        response = await self.client.get('/json/list')
        response.raise_for_status()
        return [SimpleNamespace(url=tab.get('url', ''), title=tab.get('title', ''), id=tab.get('id'))
                for tab in response.json() if tab.get('type') == 'page']

    async def open_target(self, url, *, close_tabs=()):
        """Continue in this profile without connecting a page driver on the identity-provider page."""
        response = await self.client.put('/json/new?' + quote(url, safe=':/'))
        response.raise_for_status()
        for tab_id in close_tabs:
            if tab_id:
                await self.client.get('/json/close/' + quote(tab_id, safe=''))

    @asynccontextmanager
    async def attached(self):
        # Playwright's HTTP discovery honors HTTP_PROXY; resolve locally so
        # the loopback connection is never sent through an environment proxy.
        response = await self.client.get('/json/version')
        response.raise_for_status()
        websocket = response.json()['webSocketDebuggerUrl']
        address, local = urlsplit(websocket), urlsplit(self.endpoint)
        if (address.scheme != 'ws' or address.netloc != local.netloc
                or not address.path.startswith('/devtools/browser/')):
            raise ValueError('Invalid local Chrome endpoint')
        async with async_playwright() as driver:
            browser = await driver.chromium.connect_over_cdp(websocket, timeout=10000)
            try:
                yield browser, browser.contexts[0]
            finally:
                # For a CDP connection, close disconnects this client. The
                # human's window stays open if identity verification fails.
                await browser.close()

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.profile:
            # Windows may briefly retain file handles after Chrome exits.
            for attempt in range(5):
                try:
                    await asyncio.to_thread(self.profile.cleanup)
                    break
                except OSError:
                    if attempt == 4:
                        raise
                    await asyncio.sleep(.2 * (attempt + 1))


async def _prepare_native_site(native, flow, session, provider):
    """Briefly operate the target site, then disconnect as soon as upstream navigation starts."""
    from .login_flow import _origin
    from .agreements import accept_site_agreements
    from .login_pages import discover_login_page
    from .protocols import resolve_custom_protocol
    metadata = await native.tabs()
    if has_challenge(metadata) or any(urlsplit(p.url).hostname in ('linux.do', 'connect.linux.do') for p in metadata):
        return False
    async with native.attached() as (_, context):
        if session.cancelled:
            return False
        if any(urlsplit(p.url).hostname in ('linux.do', 'connect.linux.do') for p in context.pages):
            return False
        pages = [p for p in context.pages if _origin(p.url) == _origin(provider.domain)]
        if not pages:
            return False
        page = pages[-1]
        leaving = asyncio.Event()

        def on_request(request):
            if getattr(request, 'resource_type', None) != 'document':
                return
            location = urlsplit(request.url)
            if location.scheme == 'https' and location.hostname in ('linux.do', 'connect.linux.do'):
                leaving.set()

        context.on('request', on_request)

        async def prepare():
            current = await discover_login_page(page, provider, flow.store)
            await accept_site_agreements(page, current.domain)
            current = await resolve_custom_protocol(page, current, flow.store)
            if session.cancelled or _origin(page.url) != _origin(current.domain):
                return False
            # A visible site CAPTCHA must be completed in the unattached native window.
            challenge = await page.evaluate('''() => [...document.querySelectorAll('iframe[src*="captcha"],iframe[src*="challenges.cloudflare"],.tcaptcha-popup,.nc-container')]
                .some(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>40&&r.height>40&&s.display!=='none'&&s.visibility!=='hidden'})''')
            if challenge:
                return False
            await accept_site_agreements(page, current.domain)
            if current.name == 'agentrouter':
                # Its button can become enabled before the SPA receives the OAuth client ID.
                client_id = await flow._wait_oauth_config(page, current, session.method)
                if not client_id:
                    return False
                session.oauth_client_id = client_id
            # No init scripts, UA overrides or navigation wait carry over into LinuxDO.
            return await flow._click_oauth(context, page, session.method, current, no_wait_after=True)

        work = asyncio.create_task(prepare())
        departed = asyncio.create_task(leaving.wait())
        try:
            done, _ = await asyncio.wait({work, departed}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
            if leaving.is_set():
                return True
            return await work if work in done else False
        finally:
            for task in (work, departed):
                if not task.done(): task.cancel()
            await asyncio.gather(work, departed, return_exceptions=True)
            context.remove_listener('request', on_request)


async def _run_native_session(flow, session, provider, executable):
    from .login_flow import ACTIVE_STATUSES, WINDOW_TIMEOUT, PROVIDERS, _origin, verify_target_profile, chrome_user_agent
    from .protocols import ProtocolMismatch
    from .proxy import browser_proxy
    from .agreements import accept_site_agreements
    from .upstream_imports import get_playwright_proxy
    try:
        if session.cancelled or session.status not in ACTIVE_STATUSES:
            return
        if session.account_id is not None:
            await flow.discard_browser_sessions(session.account_id)
        proxy = browser_proxy(getattr(session, 'account_proxy', None))
        if proxy is None: proxy = get_playwright_proxy(use_proxy=provider.use_proxy)
        async with NativeChrome(executable, provider.domain + provider.login_path, proxy) as native:
            session.status, session.stage = 'waiting', 'native_login'
            session.verification_paused, session.verification_auto = True, False
            session.message = '正在打开原生 Chrome；将自动处理本站登录协议并查找 LinuxDO 入口。真人验证请在浏览器中完成。'
            if native.proxy_auth_required:
                session.message += ' 如 Chrome 要求代理认证，请手动输入代理用户名和密码。'
            deadline = time.monotonic() + WINDOW_TIMEOUT
            entry_attempted = False
            site_since = returned_since = None
            upstream_seen = False
            while time.monotonic() < deadline:
                if session.cancelled or session.status not in ACTIVE_STATUSES:
                    return
                if native.process.returncode is not None:
                    session.status, session.message = 'cancelled', '登录窗口已关闭，未保存账号。'
                    return
                try: pages = await native.tabs()
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(POLL_INTERVAL);continue
                if not pages:
                    session.status, session.message = 'cancelled', '登录窗口已关闭，未保存账号。'
                    return
                provider = PROVIDERS[session.provider]
                pending = flow._upstream_auth_pending(session, SimpleNamespace(pages=pages))
                challenge = has_challenge(pages)
                upstream_seen = upstream_seen or pending
                homes = [p for p in pages if flow._linuxdo_home(p.url)]
                returned = [p for p in pages if _origin(p.url) == _origin(provider.domain)
                            and not flow._is_login_page(p.url, provider)
                            and not urlsplit(p.url).path.startswith(('/oauth', '/api/', '/auth/'))]
                requested = session.resume_event.is_set();session.resume_event.clear()
                continued = session.oauth_continue_event.is_set();session.oauth_continue_event.clear()
                own = [p for p in pages if _origin(p.url) == _origin(provider.domain)]
                if homes and not returned and not pending and not challenge:
                    session.stage = 'linuxdo_home'
                    session.verification_paused, session.verification_auto = False, False
                    session.oauth_continue_available = session.linuxdo_continue_count < 3
                    session.message = 'LinuxDO 当前停在社区首页。完成登录后点击「继续原站点授权」，会保留本次登录状态。'
                    if continued and session.oauth_continue_available:
                        session.linuxdo_continue_count += 1
                        session.oauth_continue_available = False
                        await native.open_target(provider.domain + provider.login_path,
                                                 close_tabs=[getattr(p, 'id', None) for p in homes])
                        entry_attempted = False;site_since = None
                    await asyncio.sleep(POLL_INTERVAL);continue
                session.oauth_continue_available = False
                if pending or challenge:
                    site_since = returned_since = None
                    session.status, session.stage = 'waiting', 'native_login'
                    session.verification_paused, session.verification_auto = True, False
                    session.message = '请在原生 Chrome 中完成 LinuxDO 登录、真人验证和授权；验证期间应用不连接页面驱动，返回本站后自动保存。'
                    await asyncio.sleep(POLL_INTERVAL);continue
                ready = bool(returned) and upstream_seen
                returned_since = (returned_since or time.monotonic()) if ready else None
                if ready and (requested or time.monotonic() - returned_since >= RETURN_SETTLE):
                    upstream_seen = False
                    session.status, session.stage = 'verifying', 'verifying_target'
                    session.verification_paused = False
                    session.message = '已返回原站点，正在确认本站身份并保存登录状态…'
                    async with native.attached() as (browser, context):
                        for page in reversed(context.pages):
                            if _origin(page.url) != _origin(provider.domain) or flow._is_login_page(page.url, provider):
                                continue
                            from .protocols import resolve_custom_protocol
                            provider = await resolve_custom_protocol(page, provider, flow.store)
                            await accept_site_agreements(page, provider.domain)
                            cookie = await flow._session_cookie(context, provider)
                            if not cookie: continue
                            try: profile = await verify_target_profile(page, provider)
                            except Exception: profile = None
                            if profile and cookie == await flow._session_cookie(context, provider):
                                if session.provider == 'agentrouter':
                                    session.oauth_user_agent = await chrome_user_agent(browser)
                                await flow._complete_login(session, profile, cookie, context)
                                return
                    session.message = '尚未确认本站登录成功，请在浏览器中完成验证；进入控制台后点击检测并保存。'
                elif requested and returned:
                    # Explicit verification retry, after an earlier blocked balance/identity request.
                    upstream_seen = True;returned_since = None
                elif any(flow._is_login_page(p.url, provider) for p in own) and not entry_attempted:
                    site_since = site_since or time.monotonic()
                    if time.monotonic() - site_since >= RETURN_SETTLE:
                        entry_attempted = True
                        session.stage = 'preparing_native_site'
                        session.verification_paused, session.verification_auto = False, True
                        session.message = '正在处理原站点的协议复选框与 LinuxDO 登录入口…'
                        try:
                            started = await _prepare_native_site(native, flow, session, provider)
                            upstream_seen = upstream_seen or started
                        except PlaywrightError:
                            pass
                        provider = PROVIDERS[session.provider]
                        session.message = '请在浏览器中完成登录与授权；如未自动跳转，可点击站内 LinuxDO 按钮。'
                elif requested:
                    session.message = '浏览器尚未返回本站控制台，请先完成登录、真人验证与授权。'
                session.status, session.stage = 'waiting', 'native_login'
                session.verification_paused, session.verification_auto = True, False
                await asyncio.sleep(POLL_INTERVAL)
            session.status, session.message = 'expired', '授权等待超时，尚未确认本站身份，未保存账号。'
    except asyncio.CancelledError:
        if session.status in ACTIVE_STATUSES: session.status, session.message = 'cancelled', '已取消授权。'
        raise
    except ProtocolMismatch as error:
        session.status, session.message = 'unavailable', str(error)
    except FileNotFoundError:
        session.status, session.message = 'failed', '未找到 Google Chrome，请安装后重试。'
    except Exception:
        if session.status in ACTIVE_STATUSES:
            session.status, session.message = 'failed', '原生 Chrome 登录未完成，请检查浏览器或所选代理后重试。'
    finally:
        session.oauth_continue_available = False
        session.oauth_continue_event.clear()


async def run_native_login(flow, session):
    from .engine import LOCAL_CHROME
    from .providers import PROVIDERS
    await _run_native_session(flow, session, PROVIDERS[session.provider], LOCAL_CHROME)
