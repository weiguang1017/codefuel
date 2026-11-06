"""Target-site terms are user-authorized; CAPTCHAs, payments and third-party pages are excluded."""
import pytest
from playwright.async_api import async_playwright
from backend.engine import LOCAL_CHROME
from backend.agreements import accept_site_agreements, setup_agreement_guard


@pytest.mark.asyncio
async def test_native_and_custom_checkboxes_are_checked_once_without_touching_captcha():
    if not LOCAL_CHROME: pytest.skip('Local Chrome required')
    html = '''<meta charset="utf-8"><style>label{display:block;padding:8px}</style>
    <span role="checkbox" id="visual" aria-labelledby="terms-label" aria-checked="false" tabindex="0"
      style="display:inline-block;width:18px;height:18px" onclick="window.clicks=(window.clicks||0)+1;this.setAttribute('aria-checked',this.getAttribute('aria-checked')!=='true');document.querySelector('#hidden').checked=this.getAttribute('aria-checked')==='true'"></span>
    <input type="checkbox" id="hidden" style="clip-path:inset(50%);width:1px;height:1px;position:fixed;top:0">
    <label id="terms-label" for="hidden">我已阅读并同意 <a href="/terms">用户协议</a> 和 <a href="/privacy">隐私政策</a></label>
    <label><input type="checkbox" id="english">I have read and agree to the Terms of Service</label>
    <label><input type="checkbox" id="captcha">请确认我是真人</label>
    <label><input type="checkbox" id="payment">我已阅读并同意自动扣款服务协议</label>
    <label><input type="checkbox" id="marketing">我已阅读并同意接收营销消息的隐私政策</label>
    <label><input type="checkbox" id="permission">我同意授权访问账号信息和隐私政策</label>
    <label><input type="checkbox" id="disabled" disabled>我已阅读并同意用户协议</label>'''
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
            await setup_agreement_guard(context, 'https://fixture.example')
            page = await context.new_page();await page.goto('https://fixture.example/sign-in')
            await page.wait_for_function("document.querySelector('#hidden').checked && document.querySelector('#english').checked")
            assert await page.evaluate('window.clicks') == 1
            for selector in ['#captcha','#payment','#marketing','#permission','#disabled']:
                assert not await page.locator(selector).is_checked()
            # Respect a subsequent user uncheck; the observer must not keep toggling it back.
            await page.locator('#visual').click()
            await page.evaluate("document.body.appendChild(document.createElement('p'))")
            await accept_site_agreements(page, 'https://fixture.example')
            assert not await page.locator('#hidden').is_checked()
            assert await page.evaluate('window.clicks') == 2
            await page.goto('https://linux.do/login')
            assert await accept_site_agreements(page, 'https://fixture.example') == 0
            assert not await page.locator('#hidden').is_checked()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_agreement_added_after_render_is_handled():
    if not LOCAL_CHROME: pytest.skip('Local Chrome required')
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(executable_path=LOCAL_CHROME,headless=True)
        try:
            context=await browser.new_context()
            await context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='<meta charset="utf-8"><div id="root"></div>'))
            await setup_agreement_guard(context,'https://fixture.example')
            page=await context.new_page();await page.goto('https://fixture.example/login')
            await page.evaluate("document.querySelector('#root').innerHTML='<label><input id=late type=checkbox>我已阅读并同意用户协议</label>'")
            await page.wait_for_function("document.querySelector('#late').checked")
        finally: await browser.close()
