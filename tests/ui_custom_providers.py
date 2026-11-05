"""Run the real UI/API with a disposable database; never contact a real check-in site."""
import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.async_api import async_playwright, expect
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SHOTS = ROOT / 'build/test-custom-providers'


async def check(base, chrome):
    SHOTS.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=chrome, headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1080})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        async def local_only(route):
            if not route.request.url.startswith(base + '/'):
                return await route.abort()
            if route.request.url.endswith('/api/providers/probe'):
                return await route.fulfill(json={'recognized': True, 'message': '模拟公开配置：标准签到已开启，仍需登录验证。'})
            await route.continue_()
        await page.route('**/*', local_only)
        await page.goto(base)
        await page.get_by_role('button', name='站点管理', exact=True).click()
        await expect(page.locator('.nav-item.active')).to_have_count(1)
        await expect(page.locator('.nav-item.active')).to_have_attribute('data-view', 'sites')
        await expect(page.locator('.site-card')).to_have_count(2)
        await expect(page.get_by_text('内置配置只读', exact=True)).to_have_count(2)
        await page.get_by_role('button', name='添加自定义站点', exact=True).click()
        await page.get_by_role('button', name='保存站点', exact=True).click()
        await expect(page.locator('#site-name')).to_have_attribute('aria-invalid', 'true')
        await expect(page.get_by_role('button', name='保存站点', exact=True)).to_be_enabled()
        await page.get_by_label('站点名称', exact=True).fill('我的开发站点')
        await page.get_by_label('站点网址', exact=True).fill('https://custom.example/v1')
        await page.get_by_role('button', name='保存站点', exact=True).click()
        await expect(page.locator('#site-domain')).to_have_attribute('aria-invalid', 'true')
        await page.get_by_label('站点网址', exact=True).fill('https://custom.example')
        await page.get_by_role('button', name='检测公开配置', exact=True).click()
        await expect(page.locator('#site-probe-result')).to_contain_text('标准签到已开启')
        await page.locator('summary').click()
        await page.get_by_label('额度单位', exact=True).select_option('credits')
        await page.get_by_label('每单位对应的 quota', exact=True).fill('1')
        await page.screenshot(path=str(SHOTS / 'site-form.png'), full_page=True)
        await page.get_by_role('button', name='保存站点', exact=True).click()
        await expect(page.locator('#modal')).not_to_be_visible()
        await expect(page.locator('.site-card')).to_have_count(3)
        card = page.locator('.site-card').filter(has=page.get_by_role('heading', name='我的开发站点', exact=True))
        await card.get_by_role('button', name='添加账号', exact=True).click()
        await expect(page.locator('[name="provider"]')).to_have_value(await card.get_attribute('data-site-id'))
        await expect(page.locator('[data-method="manual"]')).to_have_class('active')
        await page.locator('[name="name"]').fill('本地测试账号')
        await page.get_by_role('button', name='Session Cookie', exact=True).click()
        await page.locator('[name="session"]').fill('fixture-session')
        await page.locator('[name="api_user"]').fill('7')
        await page.locator('#account-submit').click()
        await expect(page.locator('#modal')).not_to_be_visible()
        await expect(card).to_contain_text('1 个账号')
        await card.get_by_role('button', name='编辑配置', exact=True).click()
        await expect(page.locator('#site-domain')).to_have_attribute('readonly', '')
        await expect(page.locator('#site-currency')).to_be_disabled()
        await page.get_by_label('站点名称', exact=True).fill('个人 API 站点')
        await page.get_by_label('签到模式', exact=True).select_option('balance')
        await expect(page.locator('#site-sign-field')).to_be_hidden()
        await page.get_by_role('button', name='保存配置', exact=True).click()
        await expect(page.locator('#modal')).not_to_be_visible()
        await page.reload()
        await page.get_by_role('button', name='站点管理', exact=True).click()
        await expect(page.get_by_role('heading', name='个人 API 站点', exact=True)).to_be_visible()
        await expect(page.locator('.nav-item.active')).to_have_attribute('data-view', 'sites')
        await expect(page.locator('.nav-item.active')).to_have_css('background-color', 'rgb(237, 243, 238)')
        await page.screenshot(path=str(SHOTS / 'site-management.png'), full_page=True)
        await page.set_viewport_size({'width': 390, 'height': 844})
        await expect(page.locator('.nav-item.active')).to_have_css('font-size', '0px')
        await page.evaluate('document.fonts.ready')
        await page.screenshot(path=str(SHOTS / 'site-management-mobile.png'))
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.get_by_role('button', name='添加自定义站点', exact=True).click()
        await page.get_by_label('站点名称', exact=True).fill('临时测试站点')
        await page.get_by_label('站点网址', exact=True).fill('https://second.example')
        await page.get_by_label('签到模式', exact=True).select_option('sign_in')
        await expect(page.locator('#site-sign')).to_have_value('/api/user/sign_in')
        await page.get_by_role('button', name='保存站点', exact=True).click()
        await expect(page.locator('#modal')).not_to_be_visible()
        await page.locator('.site-card').filter(has=page.get_by_role('heading', name='临时测试站点', exact=True)).get_by_role('button', name='移除', exact=True).click()
        await page.get_by_role('button', name='确认移除', exact=True).click()
        await expect(page.locator('#modal')).not_to_be_visible()
        await expect(page.locator('.site-card')).to_have_count(3)
        await page.set_viewport_size({'width': 1440, 'height': 1080})
        await page.get_by_role('button', name='概览', exact=True).click()
        options = await page.locator('#currency-picker option').all_text_contents()
        assert options == ['USD', '个人 API 站点 · 积分']
        await page.locator('#currency-picker').select_option(label='个人 API 站点 · 积分')
        await expect(page.locator('.metrics')).to_contain_text('0.00 积分')
        assert not errors, errors
        await browser.close()
    print('PASS: custom site CRUD, validation, templates, account binding, reload, separate units, desktop/mobile; no external requests')


def main():
    with tempfile.TemporaryDirectory(prefix='codefuel-site-ui-') as directory:
        os.environ['CODEFUEL_DATA_DIR'] = directory
        # Import only after installing the disposable data location.
        from backend.main import app, store
        from backend.engine import LOCAL_CHROME
        store.update_settings(False, 6)
        sock = socket.socket();sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline: time.sleep(.05)
            assert server.started
            asyncio.run(check(f'http://127.0.0.1:{port}', LOCAL_CHROME))
        finally:
            server.should_exit = True;thread.join(timeout=10);sock.close()


if __name__ == '__main__': main()
