"""Restore the Chrome identity used when the account's OAuth cookies were issued."""


def valid_user_agent(value):
    return (isinstance(value, str) and 0 < len(value) <= 512
            and value.startswith('Mozilla/') and 'Chrome/' in value
            and 'HeadlessChrome/' not in value and all(32 <= ord(char) < 127 for char in value))


async def chrome_user_agent(browser, saved=None):
    if valid_user_agent(saved):
        return saved
    cdp = await browser.new_browser_cdp_session()
    try:
        # Legacy accounts were authorized using this same installed Chrome in
        # headed mode. Headless changes its product token and invalidates some
        # cookie-bound sessions; keep the real browser/platform/version intact.
        actual = (await cdp.send('Browser.getVersion'))['userAgent']
        value = actual.replace('HeadlessChrome/', 'Chrome/')
        if not valid_user_agent(value):
            raise ValueError('Chrome did not provide a valid user agent')
        return value
    finally:
        await cdp.detach()
