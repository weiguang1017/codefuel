"""Check footer version updates and unrestricted UI using local, credential-free fixtures."""
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

from playwright.async_api import async_playwright, expect
from ui_platform_login import API, StaticHandler

ROOT = Path(__file__).resolve().parents[1]


async def check(base):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
        fixture = API()
        original_bootstrap = fixture.bootstrap
        fixture.version = '8.7.6'
        fixture.bootstrap = lambda: {**original_bootstrap(), 'engine': {'version': fixture.version}}
        page = await fixture.attach(browser, base)
        await expect(page.locator('#app-version')).to_have_text('v8.7.6')
        fixture.version = '8.7.7-rc.1'
        await expect(page.locator('#app-version')).to_have_text('v8.7.7-rc.1', timeout=8000)
        await expect(page.locator('#vip-entry')).to_have_count(0)
        await expect(page.locator('.ad-slot')).to_have_count(2)
        await page.get_by_role('button', name='站点管理', exact=True).click()
        await page.get_by_role('button', name='添加自定义站点', exact=True).click()
        await expect(page.locator('#provider-form')).to_be_visible()
        await page.keyboard.press('Escape')
        for width, height in [(1360, 900), (1120, 720)]:
            await page.set_viewport_size({'width': width, 'height': height})
            await page.evaluate('document.fonts.ready')
            await page.evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
            for selector in ['.sidebar-bottom [data-action="settings"]', '.sidebar-bottom .local-card', '#app-version']:
                rect = await page.locator(selector).bounding_box()
                assert rect and rect['y'] >= 0 and rect['y'] + rect['height'] <= height, (selector, rect)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not fixture.errors and not fixture.console_errors
        await browser.close()
    print('PASS: footer follows API version including prerelease updates; no VIP entry; custom-site form opens directly; 2 ads and full sidebar footer')


def run():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(StaticHandler, directory=str(ROOT / 'web')))
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        asyncio.run(check(f'http://127.0.0.1:{server.server_port}'))
    finally:
        server.shutdown();server.server_close();thread.join()


if __name__ == '__main__': run()
