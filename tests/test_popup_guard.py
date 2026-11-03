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


@pytest.mark.asyncio
async def test_anyrouter_notice_can_discuss_authorization_without_being_consent():
    if not LOCAL_CHROME: pytest.skip('Local Chrome is required')
    html = '''<meta charset="utf-8">
      <div class="semi-modal-content" id="notice"><div class="semi-modal-title">系统公告</div>
        <p>请了解 API 授权方式与权限说明。</p><button onclick="window.persisted=true">今日关闭</button>
        <button onclick="this.parentElement.remove()">关闭公告</button></div>
      <div role="dialog" id="info"><h2>温馨提示</h2><p>操作已完成</p><button onclick="this.parentElement.remove()">确定</button></div>
      <div role="dialog" id="danger"><h2>提示</h2><p>是否删除账号？</p><button onclick="this.parentElement.remove()">确定</button></div>
      <div role="dialog" id="consent"><h2>提示</h2><p>授权访问你的账号信息</p><button onclick="this.parentElement.remove()">关闭</button></div>'''
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
            await setup_notice_guard(context, 'https://anyrouter.top')
            page = await context.new_page()
            await page.goto('https://anyrouter.top/login')
            await page.wait_for_function('!document.querySelector("#notice")&&!document.querySelector("#info")')
            assert not await page.evaluate('!!window.persisted')
            assert await page.locator('#danger').count() == 1
            assert await page.locator('#consent').count() == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_linuxdo_notice_that_becomes_visible_is_closed_but_consent_remains():
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required')
    html = '''<meta charset="utf-8">
        <div class="d-modal__container" id="welcome" style="display:none"><h2>欢迎使用</h2>
          <button onclick="this.parentElement.remove()">知道了</button></div>
        <dialog open id="consent"><h2>应用授权</h2><button>允许</button></dialog>
        <dialog open id="challenge"><h2>人机验证</h2><button>验证</button></dialog>'''
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
            await setup_notice_guard(context, 'https://agentrouter.org',
                                     additional_origins=('https://linux.do', 'https://connect.linux.do'))
            page = await context.new_page()
            for host in ('linux.do', 'connect.linux.do'):
                await page.goto(f'https://{host}/')
                await page.evaluate('document.querySelector("#welcome").style.display="block"')
                await page.wait_for_function('!document.querySelector("#welcome")')
                assert await page.locator('#consent').count() == 1
                assert await page.locator('#challenge').count() == 1
            await page.goto('https://unrelated.example/')
            await page.evaluate('document.querySelector("#welcome").style.display="block"')
            assert await page.locator('#welcome').is_visible()
        finally:
            await browser.close()
