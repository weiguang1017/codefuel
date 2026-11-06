"""The manual continuation control keeps the original account/session target. Mock API only."""
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

from playwright.async_api import async_playwright, expect
from ui_platform_login import API, METHODS, PROVIDERS, StaticHandler

ROOT = Path(__file__).resolve().parents[1]


async def check(base):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
        providers = [*PROVIDERS, {'id':'loxo','name':'LOXO','domain':'https://loxo.example',
                                 'custom':True,'template':'sub2api','login_methods':METHODS}]
        api = API(providers=providers, plans=[{'home_wait':True,'statuses':['success']}])
        page = await api.attach(browser, base)
        await page.get_by_role('button', name='站点管理', exact=True).click()
        card = page.locator('.site-card').filter(has=page.get_by_role('heading', name='LOXO', exact=True))
        await card.get_by_role('button', name='添加账号', exact=True).click()
        await page.locator('[name="name"]').fill('LOXO 测试账号')
        await page.locator('[data-method="linuxdo"]').click()
        await page.locator('#account-submit').click()
        await expect(page.get_by_role('button', name='继续原站点授权', exact=True)).to_be_visible()
        assert not api.accounts
        folder=ROOT/'build/test-linuxdo-return';folder.mkdir(parents=True,exist_ok=True)
        await page.screenshot(path=str(folder/'continue-authorization.png'))
        await page.get_by_role('button', name='继续原站点授权', exact=True).click()
        await expect(page.locator('.platform-progress .success')).to_have_count(1)
        assert len(api.starts)==1 and api.starts[0]['provider']=='loxo'
        assert len(api.continues)==1 and len(api.accounts)==1
        assert api.accounts[0]['provider']=='loxo'
        assert not api.errors and not api.console_errors
        await browser.close()
    print('PASS: LinuxDO home continuation is visible, reuses the same authorization session and saves the original site only')


def run():
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(StaticHandler,directory=str(ROOT/'web')))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: asyncio.run(check(f'http://127.0.0.1:{server.server_port}'))
    finally: server.shutdown();server.server_close();thread.join()


if __name__=='__main__': run()
