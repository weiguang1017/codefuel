"""Automatic mode matching, stale responses and legacy configuration — local API fixtures only."""
import asyncio
import copy
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect, Error as PlaywrightError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ui_platform_login import API, StaticHandler, METHODS
from backend.providers import CustomProviderIn
from backend.site_probe import classify_config


def response(domain, *, sub=True, enabled=True):
    if sub:
        return classify_config(domain, '/api/v1/settings/public', {'code': 0, 'data': {'site_name': 'fixture', 'check_in_enabled': enabled}})
    return classify_config(domain, '/api/status', {'success': True, 'data': {'system_name': 'fixture', 'checkin_enabled': enabled}})


class ProbeAPI(API):
    def __init__(self):
        super().__init__()
        self.probes, self.site_creates, self.site_updates = [], [], []
        self.plans = {}

    async def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path == '/api/providers/probe':
            payload = request.post_data_json
            assert set(payload) == {'domain'}
            self.probes.append(payload['domain'])
            plan = self.plans.get(payload['domain'], {})
            if plan.get('started'): plan['started'].set()
            if plan.get('hold'): await plan['hold'].wait()
            try:
                await route.fulfill(json=plan.get('response', response(payload['domain'])))
            except PlaywrightError:
                pass  # Closing the form or changing its URL cancels the browser request.
            return
        if path == '/api/providers' and request.method == 'POST':
            payload = request.post_data_json
            self.site_creates.append(payload)
            self.providers.append({'id': 'created', **payload, 'custom': True, 'active': True,
                                   'login_methods': METHODS, 'account_count': 0, 'units_locked': False})
            return await route.fulfill(status=201, json={'id': 'created'})
        if path.startswith('/api/providers/') and request.method == 'PUT':
            self.site_updates.append(request.post_data_json)
            return await route.fulfill(json={'ok': True})
        return await super().route(route)


async def open_form(api, browser, base):
    page = await api.attach(browser, base)
    await page.get_by_role('button', name='站点管理', exact=True).click()
    await page.get_by_role('button', name='添加自定义站点', exact=True).click()
    assert await page.locator('#site-template option').all_text_contents() == [
        '标准 New API 签到', 'Sub2API 签到', 'AnyRouter 兼容签到', '仅同步余额']
    return page


async def check(base):
    shots = ROOT / 'build/test-auto-probe';shots.mkdir(parents=True, exist_ok=True)
    apis = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
        api = ProbeAPI();apis.append(api)
        page = await open_form(api, browser, base)
        await page.get_by_label('站点网址', exact=True).fill('https://sub.example')
        await expect(page.locator('#site-template')).to_have_value('sub2api')
        await expect(page.locator('#site-info')).to_have_value('/api/v1/auth/me')
        await expect(page.locator('#site-probe-result')).to_contain_text('已自动匹配')
        await page.get_by_label('站点名称', exact=True).fill('自动识别示例')
        await page.screenshot(path=str(shots / 'automatic-sub2api.png'))
        await page.get_by_role('button', name='保存站点', exact=True).click()
        await expect(page.locator('#modal')).not_to_be_visible()
        assert len(api.probes) == 1 and api.site_creates[0]['template'] == 'sub2api'
        await page.close()

        # An unrecognized replacement URL cannot keep an earlier URL's auto-selected protocol.
        api = ProbeAPI();apis.append(api)
        api.plans['https://unknown.example'] = {'response': {'recognized':False, 'message':'未能自动识别，请手动选择。'}}
        page = await open_form(api, browser, base)
        await page.locator('#site-domain').fill('https://first.example')
        await expect(page.locator('#site-template')).to_have_value('sub2api')
        await page.locator('#site-domain').fill('https://unknown.example')
        await expect(page.locator('#site-probe-result')).to_contain_text('未能自动识别')
        await expect(page.locator('#site-template')).to_have_value('newapi')
        await expect(page.locator('#site-info')).to_have_value('/api/user/self')
        await page.close()

        # Saving while detection is pending must use the detected mode.
        api = ProbeAPI();apis.append(api);started, hold = asyncio.Event(), asyncio.Event()
        api.plans['https://slow.example'] = {'started': started, 'hold': hold}
        page = await open_form(api, browser, base)
        await page.locator('#site-name').fill('pending')
        await page.locator('#site-domain').fill('https://slow.example')
        await asyncio.wait_for(started.wait(), 4)
        await page.locator('#provider-form [type="submit"]').click()
        await expect(page.locator('#provider-form [type="submit"]')).to_have_text('正在识别…')
        assert not api.site_creates
        hold.set()
        await expect(page.locator('#modal')).not_to_be_visible()
        assert api.site_creates[0]['template'] == 'sub2api'
        await page.close()

        # User edits win over late automatic results.
        api = ProbeAPI();apis.append(api);started, hold = asyncio.Event(), asyncio.Event()
        api.plans['https://manual.example'] = {'started': started, 'hold': hold}
        page = await open_form(api, browser, base)
        await page.locator('#site-domain').fill('https://manual.example')
        await asyncio.wait_for(started.wait(), 4)
        await page.locator('#site-template').select_option('sign_in')
        await page.locator('summary').click()
        await page.locator('#site-info').fill('/special/profile')
        hold.set()
        await expect(page.locator('#site-probe-result')).to_contain_text('已保留当前配置')
        await expect(page.locator('#site-template')).to_have_value('sign_in')
        await expect(page.locator('#site-info')).to_have_value('/special/profile')
        await page.close()

        # A response for a previous URL cannot overwrite the current URL's result.
        api = ProbeAPI();apis.append(api);started, hold = asyncio.Event(), asyncio.Event()
        api.plans['https://old.example'] = {'started': started, 'hold': hold}
        api.plans['https://new.example'] = {'response': response('https://new.example', sub=False)}
        page = await open_form(api, browser, base)
        await page.locator('#site-domain').fill('https://old.example')
        await asyncio.wait_for(started.wait(), 4)
        await page.locator('#site-domain').fill('https://new.example')
        await expect(page.locator('#site-probe-result')).to_contain_text('已自动匹配：标准 New API')
        hold.set()
        await expect(page.locator('#site-template')).to_have_value('newapi')
        await page.close()

        # Cancellation during detection must not save after the response arrives.
        api = ProbeAPI();apis.append(api);started, hold = asyncio.Event(), asyncio.Event()
        api.plans['https://cancel.example'] = {'started': started, 'hold': hold}
        page = await open_form(api, browser, base)
        await page.locator('#site-name').fill('cancelled')
        await page.locator('#site-domain').fill('https://cancel.example')
        await asyncio.wait_for(started.wait(), 4)
        await page.locator('#provider-form [type="submit"]').click()
        await page.keyboard.press('Escape')
        hold.set()
        await expect(page.locator('#modal')).not_to_be_visible()
        await page.wait_for_function("document.querySelector('#provider-form [type=submit]').disabled===false")
        assert not api.site_creates
        await page.close()

        # Balance-only keeps Sub2API authentication despite having a single visible mode.
        api = ProbeAPI();apis.append(api)
        api.plans['https://balance.example'] = {'response': response('https://balance.example', enabled=False)}
        page = await open_form(api, browser, base)
        await page.locator('#site-domain').fill('https://balance.example')
        await expect(page.locator('#site-template')).to_have_value('balance')
        await page.locator('#site-name').fill('balance only')
        await page.locator('#provider-form [type="submit"]').click()
        await expect(page.locator('#modal')).not_to_be_visible()
        assert api.site_creates[0]['template'] == 'sub2api_balance'
        assert api.site_creates[0]['user_info_path'] == '/api/v1/auth/me'
        await page.close()

        # Editing an old Sub2API balance-only site never resets its protocol or endpoints.
        api = ProbeAPI();apis.append(api)
        legacy = CustomProviderIn(name='旧版余额站点', domain='https://legacy.example', template='sub2api_balance').model_dump()
        api.providers.append({'id':'legacy', **legacy, 'custom':True, 'active':True, 'login_methods':METHODS, 'units_locked':True})
        page = await api.attach(browser, base)
        await page.get_by_role('button', name='站点管理', exact=True).click()
        await page.get_by_role('button', name='编辑配置', exact=True).click()
        await expect(page.locator('#site-template')).to_have_value('balance')
        await page.locator('#site-name').fill('改名余额站点')
        await page.locator('#provider-form [type="submit"]').click()
        await expect(page.locator('#modal')).not_to_be_visible()
        assert not api.probes and api.site_updates[0]['template'] == 'sub2api_balance'
        assert api.site_updates[0]['user_info_path'] == '/api/v1/auth/me'
        await browser.close()
    assert all(not api.errors for api in apis), [api.errors for api in apis]
    print('PASS: four modes; automatic matching; pending save; manual override; URL race; cancellation; Sub2API balance-only compatibility')


def run():
    server = ThreadingHTTPServer(('127.0.0.1',0), partial(StaticHandler,directory=str(ROOT/'web')))
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: asyncio.run(check(f'http://127.0.0.1:{server.server_port}'))
    finally: server.shutdown();server.server_close();thread.join()


if __name__=='__main__': run()
