"""Real DOM checks, with all site documents fulfilled locally and no account data."""
import pytest
from playwright.async_api import async_playwright
from backend.engine import LOCAL_CHROME
from backend.popups import setup_notice_guard


@pytest.mark.asyncio
async def test_notice_guard_closes_announcements_and_preserves_authentication():
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required for the isolated DOM regression')
    markup = '''<html><head><meta charset="utf-8"></head><body>
      <div role="dialog" id="notice"><h2>站点公告</h2><button onclick="this.parentElement.remove()">关闭公告</button></div>
      <div role="dialog" id="challenge"><h2>滑动验证</h2><button onclick="this.parentElement.remove()">关闭</button></div>
      <div role="dialog" id="consent"><h2>应用授权</h2><button onclick="this.parentElement.remove()">同意</button></div>
      <div role="dialog" id="login"><h2>欢迎回来</h2><input type="password"><button onclick="this.parentElement.remove()">关闭</button></div>
      </body></html>'''
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=markup))
            await setup_notice_guard(context, 'https://agentrouter.org')
            page = await context.new_page()
            await page.goto('https://agentrouter.org/login')
            await page.wait_for_function("!document.querySelector('#notice')")
            assert await page.locator('[role="dialog"]').count() == 3
            await page.evaluate('''() => {
                const el = document.createElement('div'); el.setAttribute('role','dialog'); el.id='late';
                el.innerHTML='<h2>欢迎使用</h2><button onclick="this.parentElement.remove()">知道了</button>';
                document.body.appendChild(el);
            }''')
            await page.wait_for_function("!document.querySelector('#late')")
            await page.goto('https://linux.do/login')
            assert await page.locator('#notice').count() == 1
            assert await page.locator('[role="dialog"]').count() == 4
        finally:
            await browser.close()
