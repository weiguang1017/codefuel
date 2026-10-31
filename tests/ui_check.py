"""Local UI smoke test. Does not contact AnyRouter or AgentRouter."""
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SHOT = ROOT / 'tests/.tmp'
SHOT.mkdir(parents=True, exist_ok=True)
BASE_URL = os.getenv('CODEFUEL_TEST_URL', 'http://127.0.0.1:8765')
PREFIX = '界面验证'


def seed_authorized_account():
    """Insert one session account through the encrypted store, for UI rendering only."""
    from backend.store import Store
    store = Store()
    try:
        rows = [a for a in store.accounts() if a['name'] == f'{PREFIX}授权账号']
        if rows:
            return rows[0]['id']
        return store.add_account({'name': f'{PREFIX}授权账号', 'provider': 'agentrouter', 'auth_mode': 'session',
                                  'session': 'ui-test-session-not-real', 'api_user': '1', 'login_via': 'github',
                                  'enabled': True, 'interval_hours': 6})
    finally:
        store.close()


async def clean(page):
    await page.evaluate("""async (prefix) => { const d = await (await fetch('/api/bootstrap')).json();
      for (const a of d.accounts.filter(a => a.name.startsWith(prefix)))
        await fetch('/api/accounts/' + a.id, {method:'DELETE',headers:{'Content-Type':'application/json','X-CSRF-Token':d.csrf_token},body:'{}'}); }""", PREFIX)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda err: errors.append(str(err)))
        page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' else None)
        await page.goto(BASE_URL, wait_until='networkidle')
        assert '码粮 CodeFuel' in await page.title()
        assert 'CodeFuel' in await page.locator('.brand').inner_text()
        assert await page.locator('#page-title').inner_text() == '为代码添粮，让灵感续航。'
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        await clean(page)
        await page.reload(wait_until='networkidle')

        # 1. Login-method picker, matching the two site buttons.
        await page.locator('[data-action="add"]').first.click()
        await page.locator('#modal').wait_for(state='visible')
        assert await page.locator('.login-method').count() == 2
        assert '使用 GitHub 继续' in await page.locator('.login-method').nth(0).inner_text()
        assert '使用 LinuxDO 继续' in await page.locator('.login-method').nth(1).inner_text()
        assert await page.locator('#account-submit').inner_text() == '打开授权窗口'
        await page.screenshot(path=str(SHOT / 'login-methods.png'))
        await page.locator('[data-method="linuxdo"]').click()
        assert 'LinuxDO 授权登录' in await page.locator('.auth-hint').inner_text()
        await page.locator('input[name="name"]').fill(f'{PREFIX}密码账号')
        await page.locator('[data-method="password"]').click()
        assert await page.locator('#account-submit').inner_text() == '添加账号'
        await page.locator('input[name="email"]').fill('ui-test@example.invalid')
        await page.locator('input[name="password"]').fill('ui-test-not-a-real-account')
        await page.locator('#account-form button[type="submit"]').click()
        await page.locator('#modal').wait_for(state='hidden')
        assert '邮箱密码' in await page.locator('.account-table').inner_text()

        # 2. Authorized account renders its real origin, and offers re-authorization.
        seed_authorized_account()
        await page.reload(wait_until='networkidle')
        assert 'GitHub 授权' in await page.locator('.account-table').inner_text()
        assert await page.locator('[data-action="relogin"]').count() == 1
        await page.screenshot(path=str(SHOT / 'console-overview.png'), full_page=True)

        # 3. Editing an authorized account keeps its session unless re-authorization is chosen.
        await page.locator('[data-view="accounts"]').first.click()
        await page.locator('#account-search').fill(f'{PREFIX}授权账号')
        await page.locator('[data-action="edit"]').first.click()
        assert await page.locator('#account-submit').inner_text() == '保存修改'
        await page.locator('#account-form input[name="name"]').fill(f'{PREFIX}授权账号改名')
        await page.locator('#account-form button[type="submit"]').click()
        await page.locator('#modal').wait_for(state='hidden')
        await page.wait_for_timeout(300)
        assert '账号已更新' in await page.locator('#toast').inner_text()
        assert 'GitHub 授权' in await page.locator('.account-table').inner_text()

        # 4. Settings round-trip.
        await page.locator('[data-action="settings"]').first.click()
        await page.locator('#settings-form select').select_option('12')
        await page.locator('#settings-form button[type="submit"]').click()
        await page.wait_for_timeout(300)
        await page.locator('[data-action="settings"]').first.click()
        await page.locator('#settings-form select').select_option('6')
        await page.locator('#settings-form button[type="submit"]').click()
        await page.wait_for_timeout(300)

        # 5. Delete everything through the UI.
        for name in (f'{PREFIX}授权账号改名', f'{PREFIX}密码账号'):
            await page.locator('#account-search').fill(name)
            await page.locator('[data-action="edit"]').first.click()
            await page.locator('[data-action="delete"]').click()
            await page.locator('[data-action="confirm-delete"]').click()
            await page.wait_for_timeout(400)
        await page.locator('#account-search').fill('')
        await page.locator('[data-view="logs"]').first.click()
        await page.locator('[data-view="accounts"]').first.click()
        assert await page.locator('.empty-state').count() >= 1
        assert not errors, errors
        print(json.dumps({'login_methods': 'ok', 'auth_label': 'ok', 'edit_keeps_session': 'ok',
                          'settings': 'ok', 'delete': 'ok', 'javascript_errors': errors}, ensure_ascii=False))
        await browser.close()


asyncio.run(main())
