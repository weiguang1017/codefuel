"""Human-operated Chrome authorization; attach only after the user returns.

The temporary profile belongs to this login, never to the user's daily Chrome.
While the human signs in, only the loopback tab list is read: no page scripting,
automation connection, fingerprint overrides or challenge interaction.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

POLL_INTERVAL = 1.0
RETURN_SETTLE = 2.0


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
        return [SimpleNamespace(url=tab.get('url', ''), title=tab.get('title', ''))
                for tab in response.json() if tab.get('type') == 'page']

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


async def run_native_login(flow, session):
    from .engine import LOCAL_CHROME
    from .login_flow import (ACTIVE_STATUSES, WINDOW_TIMEOUT, PROVIDERS, _origin,
                             verify_target_profile, chrome_user_agent)
    from .proxy import browser_proxy
    from .upstream_imports import get_playwright_proxy

    provider = PROVIDERS[session.provider]
    try:
        if session.cancelled or session.status not in ACTIVE_STATUSES:
            return
        if session.account_id is not None:
            await flow.discard_browser_sessions(session.account_id)
        proxy = browser_proxy(getattr(session, 'account_proxy', None))
        if proxy is None:
            proxy = get_playwright_proxy(use_proxy=provider.use_proxy)
        async with NativeChrome(LOCAL_CHROME, provider.domain + provider.login_path, proxy) as native:
            session.status, session.stage = 'waiting', 'native_login'
            session.verification_paused, session.verification_auto = True, False
            instruction = ('请在新开的 Chrome 窗口点击 LinuxDO 登录，手动完成登录、真人验证和授权；'
                           '返回 AgentRouter 后会自动检测并保存，也可点击下方按钮检测。')
            if native.proxy_auth_required:
                instruction += ' 如 Chrome 弹出代理认证框，请输入该代理的用户名和密码。'
            session.message = instruction
            deadline = time.monotonic() + WINDOW_TIMEOUT
            upstream_seen = False
            returned_since = None
            while time.monotonic() < deadline:
                if session.cancelled or session.status not in ACTIVE_STATUSES:
                    return
                if native.process.returncode is not None:
                    session.status, session.message = 'cancelled', '登录窗口已关闭，未保存账号。'
                    return
                try:
                    pages = await native.tabs()
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                if not pages:
                    session.status, session.message = 'cancelled', '登录窗口已关闭，未保存账号。'
                    return
                pending = flow._upstream_auth_pending(session, SimpleNamespace(pages=pages))
                if pending:
                    upstream_seen = True
                returned = [page for page in pages if _origin(page.url) == _origin(provider.domain)
                            and urlsplit(page.url).path.rstrip('/') not in ('/login', '/register')
                            and not urlsplit(page.url).path.startswith(('/oauth', '/api/'))]
                # Do not attach while an upstream login, callback or visible
                # challenge is pending. A tab URL alone never proves identity.
                challenge = any(any(word in page.title.lower() for word in
                                    ('just a moment', '请稍候', '请稍等', 'attention required')) for page in pages)
                ready = bool(returned) and not pending and not challenge
                returned_since = (returned_since or time.monotonic()) if ready else None
                requested = session.resume_event.is_set()
                session.resume_event.clear()
                automatic = upstream_seen and returned_since is not None and time.monotonic() - returned_since >= RETURN_SETTLE
                if ready and (automatic or requested):
                    # One attempt per return. If blocked, disconnect immediately
                    # and let the human finish before an explicit new attempt.
                    upstream_seen = False
                    session.status, session.stage = 'verifying', 'verifying_target'
                    session.verification_paused = False
                    session.message = '已返回 AgentRouter，正在确认本站账号身份…'
                    async with native.attached() as (browser, context):
                        for page in reversed(context.pages):
                            if _origin(page.url) != _origin(provider.domain):
                                continue
                            cookie = await flow._session_cookie(context, provider)
                            if not cookie:
                                continue
                            try:
                                profile = await verify_target_profile(page, provider)
                            except Exception:
                                profile = None
                            if profile and cookie == await flow._session_cookie(context, provider):
                                session.oauth_user_agent = await chrome_user_agent(browser)
                                await flow._complete_login(session, profile, cookie, context)
                                return
                    session.message = ('尚未确认 AgentRouter 登录成功。请在 Chrome 中完成登录和网站验证，'
                                       '进入控制台后点击「已完成登录，检测并保存」。')
                elif requested:
                    session.message = '浏览器尚未返回 AgentRouter 控制台；请先完成 LinuxDO 登录、真人验证和授权。'
                session.status, session.stage = 'waiting', 'native_login'
                session.verification_paused, session.verification_auto = True, False
                await asyncio.sleep(POLL_INTERVAL)
            session.status, session.message = 'expired', '等待授权超时，未确认 AgentRouter 身份，未保存账号。'
    except asyncio.CancelledError:
        if session.status in ACTIVE_STATUSES:
            session.status, session.message = 'cancelled', '已取消授权。'
        raise
    except FileNotFoundError:
        session.status, session.message = 'failed', '未找到 Google Chrome，请安装后重试。'
    except Exception:
        if session.status in ACTIVE_STATUSES:
            session.status, session.message = 'failed', 'Chrome 授权未能完成，请检查浏览器与该账号的网络代理后重试。'
