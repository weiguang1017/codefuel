from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from backend.browser_identity import chrome_user_agent
from backend.engine import LOCAL_CHROME


UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'


@pytest.mark.asyncio
async def test_legacy_identity_keeps_installed_chrome_version_and_platform():
    cdp = SimpleNamespace(send=AsyncMock(return_value={'userAgent': UA.replace('Chrome/', 'HeadlessChrome/')}),
                          detach=AsyncMock())
    browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp))
    assert await chrome_user_agent(browser) == UA
    cdp.send.assert_awaited_once_with('Browser.getVersion')
    cdp.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_saved_oauth_identity_survives_a_browser_upgrade():
    browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(side_effect=AssertionError('Use the account identity')))
    assert await chrome_user_agent(browser, UA) == UA
    browser.new_browser_cdp_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['', 'bot', UA + '\r\nInjected: true', UA + '\x7f',
                                   UA.replace('Chrome/', 'HeadlessChrome/'), {}, 'x' * 513])
async def test_invalid_cached_identity_uses_real_chrome(invalid):
    cdp = SimpleNamespace(send=AsyncMock(return_value={'userAgent': UA}), detach=AsyncMock())
    browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp))
    assert await chrome_user_agent(browser, invalid) == UA
    cdp.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_headless_page_uses_compatible_identity_without_a_visible_browser():
    if not LOCAL_CHROME:
        pytest.skip('Local Chrome is required')
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=LOCAL_CHROME, headless=True)
        try:
            original = await browser.new_page()
            original_ua = await original.evaluate('navigator.userAgent')
            assert 'HeadlessChrome/' in original_ua
            ua = await chrome_user_agent(browser)
            context = await browser.new_context(user_agent=ua)
            page = await context.new_page()
            assert await page.evaluate('navigator.userAgent') == original_ua.replace('HeadlessChrome/', 'Chrome/')
        finally:
            await browser.close()
