"""Focus/validation and automatic authorization progress with isolated API responses."""
import asyncio
import json
from functools import partial
from http.server import ThreadingHTTPServer
from threading import Thread

from playwright.async_api import async_playwright, expect
from ui_platform_login import API, ROOT, StaticHandler, add


async def cases(browser, base):
    output = ROOT / 'build/test-authorization'
    output.mkdir(parents=True, exist_ok=True)
    for width, height in [(1440, 1080), (390, 844)]:
        api = API()
        page = await api.attach(browser, base)
        await page.set_viewport_size({'width': width, 'height': height})
        await page.locator('[data-action="add"]').first.click()
        await page.locator('#account-submit').click()
        name = page.locator('[name="name"]')
        await expect(name).to_be_focused()
        await expect(name).to_be_in_viewport()
        await expect(name).to_have_attribute('aria-invalid', 'true')
        await expect(page.locator('#field-error-name')).to_have_text('请填写账号备注')
        assert not api.starts
        await page.screenshot(path=str(output / f'missing-name-{width}.png'))
        await name.fill('   ')
        await page.locator('#account-submit').click()
        await expect(name).to_be_focused()
        assert not api.starts
        await name.fill('fixture')
        await expect(page.locator('#field-error-name')).to_have_count(0)

        await page.locator('[name="proxy_mode"]').select_option('custom')
        await page.locator('[name="proxy_auth"]').check()
        await page.locator('#account-submit').click()
        await expect(page.locator('[name="proxy_server"]')).to_be_focused()
        await expect(page.locator('[name="proxy_server"]')).to_be_in_viewport()
        await expect(page.locator('.field-error')).to_have_count(3)
        await page.screenshot(path=str(output / f'missing-proxy-{width}.png'))
        await page.locator('[name="proxy_server"]').fill('broken-address')
        await page.locator('#account-submit').click()
        await expect(page.locator('#field-error-proxy_server')).to_contain_text('有效代理地址')
        assert not api.starts
        await page.locator('[name="proxy_mode"]').select_option('direct')
        await expect(page.locator('.field-error')).to_have_count(0)
        await page.locator('[name="providers"]').first.uncheck()
        await page.locator('#account-submit').click()
        await expect(page.locator('[name="providers"]').first).to_be_focused()
        await expect(page.locator('#field-error-providers')).to_have_text('请至少选择一个授权平台')
        assert not api.starts
        await page.locator('[data-action="select-all-platforms"]').click()
        await expect(page.locator('.field-error')).to_have_count(0)

        await page.locator('[data-method="password"]').click()
        await page.locator('[name="email"]').fill('bad-email')
        await page.locator('#account-submit').click()
        await expect(page.locator('[name="email"]')).to_be_focused()
        await expect(page.locator('#field-error-email')).to_have_text('请输入有效的登录邮箱')
        await expect(page.locator('#field-error-password')).to_have_text('请填写登录密码')
        assert not api.starts and not api.creates
        await page.locator('[data-method="session"]').click()
        await page.locator('#account-submit').click()
        await expect(page.locator('[name="session"]')).to_be_focused()
        await expect(page.locator('#field-error-session')).to_have_text('请填写Session Cookie')
        await page.locator('[name="session"]').fill('fixture-session')
        await page.locator('[name="api_user"]').fill('7')
        await page.locator('#account-submit').click()
        await expect(page.locator('#modal')).not_to_be_visible()
        assert len(api.creates) == 1
        await api.close()

    api = API(plans=[{'verification_paused': True, 'verification_auto': True, 'statuses': ['success']}])
    page = await api.attach(browser, base)
    await add(page, multiple=False)
    await page.locator('#account-submit').click()
    await expect(page.locator('.platform-progress .detecting')).to_be_visible()
    await expect(page.locator('.platform-progress .detecting')).to_contain_text('自动检测')
    await expect(page.get_by_role('button', name='我已处理，重新校验')).to_have_count(0)
    await page.screenshot(path=str(output / 'auto-detection.png'))
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert len(api.accounts) == 1 and len(api.starts) == 1
    assert api.resumes == []
    await api.close()
    api = API(plans=[{'upstream_wait_polls': 2, 'statuses': ['success']}])
    page = await api.attach(browser, base)
    await add(page, 'linuxdo', multiple=False)
    await page.locator('[name="providers"][value="anyrouter"]').uncheck()
    await page.locator('[name="providers"][value="agentrouter"]').check()
    await page.locator('#account-submit').click()
    await expect(page.locator('.platform-progress .detecting')).to_contain_text('正在等待 LinuxDO')
    await expect(page.locator('[data-action="resume-login"]')).to_have_count(0)
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert len(api.accounts) == 1 and api.accounts[0]['provider'] == 'agentrouter'
    assert len(api.starts) == 1 and api.resumes == []
    await api.close()
    return {'checks': ['必填项自动定位、聚焦、红色提示', '代理及邮箱格式校验', '修正后清除错误与隐藏字段跳过',
                       '无手动确认的自动检测与保存', 'LinuxDO SSO 期间不显示立即检测按钮', '桌面/窄屏布局'], 'javascript_errors': []}


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
