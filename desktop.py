"""CodeFuel desktop client: a native window around the local console."""
import argparse
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import webview

from backend.paths import APP_NAME, data_dir

PREFERRED_PORT = 8765
WINDOW_SIZE = (1360, 900)
MIN_SIZE = (1120, 720)


def health(port, timeout=1.0):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=timeout) as response:
            return json.loads(response.read().decode()) == {'status': 'ok'}
    except (urllib.error.URLError, OSError, ValueError):
        return False


def free_port(start=PREFERRED_PORT, limit=25):
    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('本机没有可用端口，请关闭其他程序后重试。')


class LocalServer:
    def __init__(self, port=None):
        self.port = port or free_port()
        self.server = None
        self.thread = None

    def start(self):
        import uvicorn
        from backend.main import app

        config = uvicorn.Config(app, host='127.0.0.1', port=self.port, log_level='warning',
                                access_log=False, lifespan='on', loop='asyncio', http='h11', ws='none')
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name='dailyquota-api', daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if health(self.port, 0.5):
                return
            if not self.thread.is_alive():
                break
            time.sleep(0.25)
        raise RuntimeError('本地服务未能启动，请重启应用或在终端中查看错误。')

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=6)


def build_window(url):
    window = webview.create_window(
        f'{APP_NAME} · 每日额度',
        url,
        width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        background_color='#f6f7f8',
        text_select=True,
    )

    def on_closing():
        # Closing the window stops the scheduler; say so instead of failing silently.
        try:
            pending = window.evaluate_js('window.__dqEnabledAccounts ? window.__dqEnabledAccounts() : 0') or 0
        except Exception:
            pending = 0
        if not pending:
            return True
        return window.create_confirmation_dialog(
            '退出码粮？',
            '关闭窗口会同时停止自动签到。电脑休眠或关机期间无法执行，下次打开时会补做一次到期任务。',
        )

    window.events.closing += on_closing
    return window


def run_selftest(port):
    """Prove the packaged app can serve the console without opening a window."""
    from backend.paths import resource_root
    assets = resource_root() / 'web' / 'index.html'
    server = LocalServer(port)
    server.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.port}/api/bootstrap', timeout=10) as response:
            bootstrap = json.loads(response.read().decode())
        print(json.dumps({'selftest': 'ok', 'port': server.port, 'data_dir': str(data_dir()),
                          'assets': assets.exists(), 'frozen': bool(getattr(sys, 'frozen', False)),
                          'accounts': len(bootstrap['accounts']), 'engine': bootstrap['engine']},
                         ensure_ascii=False), flush=True)
    finally:
        server.stop()
    return 0


def run_smoke(port):
    """Open the real window, read the rendered page, then quit — end-to-end check."""
    server = LocalServer(port)
    server.start()
    window = webview.create_window('CodeFuel smoke', f'http://127.0.0.1:{server.port}',
                                   width=1280, height=860)
    report = {}

    def inspect():
        heading = None
        for _ in range(25):
            time.sleep(1)
            try:
                heading = window.evaluate_js(
                    "document.querySelector('h1') ? document.querySelector('h1').innerText : null")
                if heading:
                    break
            except Exception:
                continue
        try:
            report.update(heading=heading,
                          title=window.evaluate_js('document.title'),
                          brand=window.evaluate_js("document.querySelector('.brand').innerText"),
                          metrics=window.evaluate_js("document.querySelectorAll('.metric').length"),
                          nav=window.evaluate_js("document.querySelectorAll('.nav-item').length"))
        except Exception as error:  # noqa: BLE001
            report['error'] = repr(error)
        report['smoke'] = 'ok' if (heading == '为代码添粮，让灵感续航。'
                                  and 'CodeFuel' in report.get('brand', '')
                                  and report.get('metrics') == 4) else 'failed'
        print(json.dumps(report, ensure_ascii=False), flush=True)
        window.destroy()

    try:
        webview.start(inspect)
    finally:
        server.stop()
    return 0 if report.get('smoke') == 'ok' else 1


def run_browser_selftest():
    """Exercise the packaged driver with local HTML; no credentials or site calls."""
    import asyncio
    from backend.engine import LOCAL_CHROME
    from playwright.async_api import async_playwright

    if not LOCAL_CHROME:
        print(json.dumps({'browser_selftest': 'unavailable', 'reason': '未安装 Google Chrome'},
                         ensure_ascii=False), flush=True)
        return 1

    async def inspect():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<title>CodeFuel driver selftest</title><h1>码粮</h1>')
                title = await page.title()
                assert title == 'CodeFuel driver selftest'
                print(json.dumps({'browser_selftest': 'ok', 'browser': LOCAL_CHROME,
                                  'title': title}, ensure_ascii=False), flush=True)
            finally:
                await browser.close()

    asyncio.run(inspect())
    return 0


def main():
    parser = argparse.ArgumentParser(description='CodeFuel desktop client')
    parser.add_argument('--port', type=int, default=None, help='使用固定端口（默认自动选择）')
    parser.add_argument('--browser', action='store_true', help='改用系统浏览器打开（调试用）')
    parser.add_argument('--debug', action='store_true', help='打开开发者工具')
    parser.add_argument('--selftest', action='store_true', help='验证能启动本地服务，不打开窗口')
    parser.add_argument('--smoke', action='store_true', help='打开窗口读取页面内容后退出，用于端到端自检')
    parser.add_argument('--browser-selftest', action='store_true', help='使用本地页面验证浏览器驱动，不连接站点')
    args = parser.parse_args()

    if args.browser_selftest:
        return run_browser_selftest()

    data_dir()  # create and validate the writable state directory early

    if args.smoke:
        return run_smoke(args.port)
    if args.selftest:
        return run_selftest(args.port)

    server = None
    port = args.port
    if port and health(port):
        reuse = True
    elif not args.port and health(PREFERRED_PORT):
        reuse, port = True, PREFERRED_PORT
    else:
        reuse = False

    if not reuse:
        server = LocalServer(port)
        try:
            server.start()
        except Exception as error:  # noqa: BLE001 - surfaced to the user
            print(f'[CodeFuel] {error}', file=sys.stderr, flush=True)
            if args.browser:
                raise
            webview.create_window(f'{APP_NAME} · 启动失败',
                                  html=f'<body style="font:14px -apple-system,sans-serif;padding:40px">'
                                       f'<h2>无法启动本地服务</h2><p>{error}</p></body>',
                                  width=520, height=280)
            webview.start()
            return 1
        port = server.port

    url = f'http://127.0.0.1:{port}'
    if args.browser:
        import webbrowser
        webbrowser.open(url)
        print(f'[CodeFuel] 控制台地址 {url}（关闭此终端即停止后台任务）', flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            if server:
                server.stop()
        return 0

    build_window(url)
    try:
        webview.start(debug=args.debug)
    finally:
        if server:
            server.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
