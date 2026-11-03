"""Account proxy form regression with local pages and a simulated API."""
import asyncio
import json
from functools import partial
from http.server import ThreadingHTTPServer
from threading import Thread
from pathlib import Path

from playwright.async_api import async_playwright, expect
from ui_platform_login import API, StaticHandler, ROOT, account, add


async def cases(browser, base):
    api = API()
    page = await api.attach(browser, base)
    await add(page)
    await page.locator('[name="proxy_mode"]').select_option('custom')
    await page.locator('[name="proxy_server"]').fill('http://127.0.0.1:7890')
    await page.locator('[name="proxy_auth"]').check()
    await page.locator('[name="proxy_username"]').fill('fixture-user')
    await page.locator('[name="proxy_password"]').fill('fixture-pass')
    await page.locator('#account-submit').click()
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert len(api.starts) == 2
    assert all(item['proxy'] == {'mode': 'custom', 'server': 'http://127.0.0.1:7890', 'authenticate': True,
                                'username': 'fixture-user', 'password': 'fixture-pass'} for item in api.starts)
    await api.close()

    row = account(provider='agentrouter')
    row['proxy'] = {'mode': 'custom', 'server': 'https://proxy.example:8443', 'has_auth': True}
    api = API(accounts=[row])
    page = await api.attach(browser, base)
    await page.locator('[data-action="edit"]').click()
    assert await page.locator('[name="proxy_username"]').input_value() == ''
    assert await page.locator('[name="proxy_password"]').input_value() == ''
    await page.locator('#account-submit').click()
    await expect(page.locator('#modal')).not_to_be_visible()
    assert api.updates[-1]['proxy'] == {'mode': 'custom', 'server': row['proxy']['server'], 'authenticate': True}
    # Address changes require new auth; direct mode disables those requirements.
    api.accounts[0]['proxy'] = row['proxy']
    await page.evaluate('refresh(true)')
    await page.locator('[data-action="edit"]').click()
    await page.locator('[name="proxy_server"]').fill('http://other-proxy.example:8080')
    assert not await page.locator('#account-form').evaluate('(form)=>form.checkValidity()')
    await page.locator('[name="proxy_mode"]').select_option('direct')
    assert await page.locator('#account-form').evaluate('(form)=>form.checkValidity()')
    await page.locator('#account-submit').click()
    await expect(page.locator('#modal')).not_to_be_visible()
    assert api.updates[-1]['proxy'] == {'mode': 'direct'}
    await api.close()

    for method in ('password', 'session', 'manual'):
        api = API()
        page = await api.attach(browser, base)
        await add(page, method, multiple=False)
        await page.locator('[name="proxy_mode"]').select_option('custom')
        await page.locator('[name="proxy_server"]').fill('socks5://127.0.0.1:1080')
        if method == 'password':
            await page.locator('[name="email"]').fill('fixture@example.test')
            await page.locator('[name="password"]').fill('fixture-pass')
        elif method == 'session':
            await page.locator('[name="session"]').fill('fixture-session')
            await page.locator('[name="api_user"]').fill('7')
        await page.locator('#account-submit').click()
        if method == 'manual':
            await expect(page.locator('#modal h2')).to_have_text('授权结果')
            payload = api.starts[0]
        else:
            await expect(page.locator('#modal')).not_to_be_visible()
            payload = api.creates[0]
        assert payload['proxy'] == {'mode': 'custom', 'server': 'socks5://127.0.0.1:1080', 'authenticate': False}
        await api.close()

    output = ROOT / 'build/test-account-proxy'
    output.mkdir(parents=True, exist_ok=True)
    api = API()
    page = await api.attach(browser, base)
    await add(page, multiple=False)
    await page.locator('[name="proxy_mode"]').select_option('custom')
    await page.locator('[name="proxy_auth"]').check()
    for width, height in [(1440, 1080), (390, 844)]:
        await page.set_viewport_size({'width': width, 'height': height})
        await page.locator('.proxy-settings').scroll_into_view_if_needed()
        assert await page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
        await page.screenshot(path=str(output / f'proxy-{width}.png'))
    await api.close()
    return {'checks': ['多平台授权使用所选代理', '编辑保留认证与直连清除', '邮箱/Session/手动登录传递代理',
                       '桌面与窄屏无横向溢出'], 'javascript_errors': []}


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(StaticHandler, directory=str(ROOT / 'web')))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            try:
                print(json.dumps(await cases(browser, f'http://127.0.0.1:{server.server_port}'), ensure_ascii=False))
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


asyncio.run(main())
