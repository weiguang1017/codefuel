"""隔离的多平台登录界面回归：仅本机静态页面和模拟 API，不读取账号数据。"""
import asyncio
import copy
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
SHOT = ROOT / 'build/test-platform-login'
METHODS = ['github', 'linuxdo', 'manual', 'password', 'session']
PROVIDERS = [
    {'id': 'anyrouter', 'name': 'AnyRouter', 'login_methods': METHODS, 'oauth_availability': 'detect_on_login'},
    {'id': 'agentrouter', 'name': 'AgentRouter', 'login_methods': METHODS, 'oauth_availability': 'detect_on_login'},
]


class StaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path.startswith('/assets/'):
            path = path.removeprefix('/assets')
        return super().translate_path(path)

    def log_message(self, *_):
        pass


def account(provider='anyrouter', method='github', number=1):
    return {'id': number, 'name': '隔离测试身份', 'provider': provider,
            'auth_mode': 'password' if method == 'password' else 'session', 'login_via': method,
            'interval_hours': 6, 'enabled': True, 'status': 'idle', 'balance': None,
            'last_run': None, 'next_run': None}


class API:
    def __init__(self, providers=None, accounts=None, plans=None):
        self.providers = copy.deepcopy(PROVIDERS if providers is None else providers)
        self.accounts = copy.deepcopy(accounts or [])
        self.plans = list(plans or [])
        self.starts, self.cancels, self.updates, self.creates = [], [], [], []
        self.resumes, self.polled_sessions = [], []
        self.sessions = {}
        self.polls = 0
        self.inflight = 0
        self.max_inflight = 0
        self.errors = []
        self.console_errors = []
        self.page = None

    async def attach(self, browser, base):
        self.page = await browser.new_page(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('console', lambda msg: self.console_errors.append(msg.text) if msg.type == 'error' and 'Failed to load resource' not in msg.text else None)
        await self.page.route('**/*', self.route)
        await self.page.goto(base, wait_until='networkidle')
        await expect(self.page.locator('#nav-count')).to_have_text(str(len(self.accounts)))
        return self.page

    def bootstrap(self):
        return {'csrf_token': 'isolated-test-token', 'providers': self.providers, 'accounts': self.accounts,
                'runs': [], 'trend': [{'date': '10-31', 'reward': 0, 'runs': 0}],
                'stats': {'balance': None, 'today_reward': 0, 'today_completed': 0},
                'settings': {'scheduler_enabled': False, 'interval_hours': 6}}

    def save(self, session):
        if session.get('saved'):
            return
        session['saved'] = True
        payload = session['payload']
        existing = next((a for a in self.accounts if a['id'] == payload.get('account_id')), None)
        row = existing or account(payload['provider'], payload['method'], len(self.accounts) + 1)
        row.update(name=payload['name'], login_via=payload['method'], auth_mode='session')
        if not existing:
            self.accounts.append(row)

    async def route(self, route):
        request = route.request
        url = urlparse(request.url)
        if url.hostname != '127.0.0.1':
            await route.abort()
            raise AssertionError(f'测试不允许外部请求：{request.url}')
        path = url.path
        if not path.startswith('/api/'):
            return await route.continue_()
        if path == '/api/bootstrap':
            return await route.fulfill(json=self.bootstrap())
        if path == '/api/login/start':
            payload = request.post_data_json
            self.starts.append(payload)
            plan = self.plans.pop(0) if self.plans else {'statuses': ['success']}
            if plan.get('http_error'):
                return await route.fulfill(status=plan['http_error'], json={'detail': plan.get('message', '模拟服务暂不可用')})
            sid = str(len(self.starts))
            self.sessions[sid] = {'payload': payload, 'plan': plan, 'poll': 0, 'cancelled': False,
                                  'verification_paused': plan.get('verification_paused', False)}
            if plan.get('start_entered'):
                plan['start_entered'].set()
            if plan.get('hold_start'):
                await plan['hold_start'].wait()
            return await route.fulfill(json={'id': sid, 'status': 'starting'})
        if path.startswith('/api/login/'):
            sid = path.split('/')[3]
            session = self.sessions[sid]
            plan = session['plan']
            if path.endswith('/cancel'):
                self.cancels.append(sid)
                session['cancelled'] = True
                if plan.get('cancel_success'):
                    self.save(session)
                    return await route.fulfill(json={'id': sid, 'status': 'success'})
                return await route.fulfill(json={'id': sid, 'status': 'cancelled'})
            if path.endswith('/resume'):
                assert request.method == 'POST'
                assert session['verification_paused'] and not session['cancelled']
                self.resumes.append(sid)
                session['verification_paused'] = False
                return await route.fulfill(json={'id': sid, 'status': 'waiting', 'verification_paused': False})
            self.polls += 1
            self.polled_sessions.append(sid)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                if plan.get('poll_entered'):
                    plan['poll_entered'].set()
                if plan.get('hold_poll'):
                    await plan['hold_poll'].wait()
                if session['verification_paused']:
                    return await route.fulfill(json={'id': sid, 'status': 'waiting', 'verification_paused': True,
                                                     'message': '请在当前登录窗口完成人工处理，然后重新校验。'})
                index = session['poll']
                session['poll'] += 1
                statuses = plan.get('statuses', ['waiting'])
                status = statuses[min(index, len(statuses) - 1)]
                if status == 'network':
                    return await route.fulfill(status=503, json={'detail': '模拟查询网络中断'})
                if status == 'success':
                    self.save(session)
                return await route.fulfill(json={'id': sid, 'status': status, 'message': plan.get('message', {
                    'success': '当前平台已确认保存', 'unavailable': '平台未提供对应身份入口',
                    'error': '当前平台授权未完成', 'waiting': '等待当前平台授权',
                }.get(status, status))})
            finally:
                self.inflight -= 1
        if path == '/api/accounts' and request.method == 'POST':
            self.creates.append(request.post_data_json)
            row = account(request.post_data_json['provider'], request.post_data_json['auth_mode'], len(self.accounts) + 1)
            row.update({key: value for key, value in request.post_data_json.items() if key not in ['password', 'session', 'api_user', 'email']})
            self.accounts.append(row)
            return await route.fulfill(json=row)
        if path.startswith('/api/accounts/') and request.method == 'PUT':
            payload = request.post_data_json
            self.updates.append(payload)
            row = next(a for a in self.accounts if a['id'] == int(path.split('/')[-1]))
            row.update({key: value for key, value in payload.items() if key not in ['password', 'session', 'api_user', 'email']})
            return await route.fulfill(json=row)
        raise AssertionError(f'未预期的模拟 API：{request.method} {path}')

    async def check(self):
        assert not self.errors, self.errors
        assert not self.console_errors, self.console_errors
        assert self.max_inflight <= 1, self.max_inflight
        assert await self.page.evaluate('document.documentElement.scrollWidth <= innerWidth')

    async def close(self):
        await self.check()
        await self.page.close()


async def add(page, method='github', multiple=True):
    await page.locator('[data-action="add"]').first.click()
    if await page.locator(f'[data-method="{method}"]').is_enabled():
        await page.locator(f'[data-method="{method}"]').click()
    await page.locator('[name="name"]').fill('隔离测试身份')
    if multiple:
        await page.locator('[data-action="select-all-platforms"]').click()


async def wait_count(values, count):
    async def wait():
        while len(values) < count:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 8)
    assert len(values) == count


async def cases(browser, base):
    results = []
    # 两种通用身份方式都支持显式双选，默认仅原默认平台。
    for method in ['github', 'linuxdo']:
        api = API()
        page = await api.attach(browser, base)
        await add(page, method, multiple=False)
        assert await page.locator('[name="providers"]:checked').count() == 1
        assert await page.locator('[name="providers"]:checked').input_value() == 'anyrouter'
        assert await page.locator('[name="providers"]').count() == 2
        await page.locator('[name="providers"]').first.uncheck()
        await expect(page.locator('#account-submit')).to_be_disabled()
        await page.locator('[data-action="select-all-platforms"]').click()
        assert await page.locator('[name="providers"]:checked').count() == 2
        if method == 'github':
            await page.screenshot(path=str(SHOT / 'ui-platform-login.png'), full_page=True)
            await page.set_viewport_size({'width': 1120, 'height': 720})
            await page.locator('#account-submit').scroll_into_view_if_needed()
            await expect(page.locator('#account-submit')).to_be_in_viewport()
            await api.check()
            assert await page.locator('#modal').evaluate('el => el.scrollWidth <= el.clientWidth')
            await page.set_viewport_size({'width': 390, 'height': 844})
            await api.check()
            assert await page.locator('#modal').evaluate('el => el.scrollWidth <= el.clientWidth')
            await page.set_viewport_size({'width': 1440, 'height': 1080})
        await page.locator('#account-submit').click()
        await expect(page.locator('#modal h2')).to_have_text('授权结果')
        assert [p['provider'] for p in api.starts] == ['anyrouter', 'agentrouter']
        assert all(p['method'] == method for p in api.starts)
        assert len(api.accounts) == 2
        assert await page.locator('.platform-progress .success').count() == 2
        await expect(page.locator('#nav-count')).to_have_text('2')
        await api.close()
    results.append('GitHub/LinuxDO显式双平台选择与串行成功')

    # 动态能力、动态名称、未知平台不会误标为 AgentRouter。
    custom = copy.deepcopy(PROVIDERS)
    custom[0]['login_methods'] = ['linuxdo', 'manual', 'password', 'session']
    custom.append({'id': 'custom', 'name': '测试扩展平台', 'login_methods': ['github', 'session'], 'oauth_availability': 'detect_on_login'})
    api = API(providers=custom, accounts=[account('retired-platform')])
    page = await api.attach(browser, base)
    await page.locator('[data-view="accounts"]').first.click()
    await expect(page.locator('[data-filter="custom"]')).to_have_text('测试扩展平台')
    assert 'retired-platform' in await page.locator('.account-table').inner_text()
    await add(page, multiple=False)
    assert await page.locator('[name="providers"]').evaluate_all('els=>els.map(e=>e.value)') == ['agentrouter', 'custom']
    await page.locator('[data-method="linuxdo"]').click()
    assert await page.locator('[name="providers"]').evaluate_all('els=>els.map(e=>e.value)') == ['anyrouter', 'agentrouter']
    for method in ['password', 'session', 'manual']:
        await page.locator(f'[data-method="{method}"]').click()
        assert await page.locator('[name="providers"]').count() == 0
        assert await page.locator('select[name="provider"]').count() == 1
    await page.locator('[data-action="close"]').first.click()
    await api.close()
    api = API(providers=custom, accounts=[account('custom', 'github')])
    page = await api.attach(browser, base)
    await page.locator('[data-action="edit"]').click()
    for method in ['linuxdo', 'manual', 'password']:
        await expect(page.locator(f'[data-method="{method}"]')).to_be_disabled()
    for method in ['github', 'session']:
        await expect(page.locator(f'[data-method="{method}"]')).to_be_enabled()
    await api.close()
    api = API(providers=[{'id': 'session-only', 'name': '仅 Session 平台', 'login_methods': ['session']}])
    page = await api.attach(browser, base)
    await add(page, multiple=False)
    await expect(page.locator('#account-submit')).to_be_disabled()
    assert '当前没有适配此登录方式' in await page.locator('#platform-picker').inner_text()
    await expect(page.locator('[data-method="github"]')).to_be_disabled()
    await expect(page.locator('[data-method="linuxdo"]')).to_be_disabled()
    await page.locator('[data-method="session"]').click()
    await expect(page.locator('#account-submit')).to_be_enabled()
    assert await page.locator('[name="provider"]').input_value() == 'session-only'
    await api.close()
    results.append('动态平台能力、未知平台名称、非OAuth单平台')

    # 首个平台失败后只重试当前平台；成功的平台不重新授权。
    api = API(plans=[{'statuses': ['unavailable']}, {'statuses': ['success']}, {'statuses': ['success']}])
    page = await api.attach(browser, base)
    await add(page)
    await page.locator('#account-submit').click()
    await expect(page.locator('[data-action="retry-login"]')).to_be_visible()
    assert len(api.starts) == 1 and len(api.accounts) == 0
    assert '未提供对应身份入口' in await page.locator('.platform-progress').inner_text()
    await page.locator('[data-action="retry-login"]').click()
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert [p['provider'] for p in api.starts] == ['anyrouter', 'anyrouter', 'agentrouter']
    assert len(api.accounts) == 2
    await api.close()
    api = API(plans=[{'statuses': ['success']}, {'statuses': ['error']}, {'statuses': ['success']}])
    page = await api.attach(browser, base)
    await add(page)
    await page.locator('#account-submit').click()
    await expect(page.locator('[data-action="retry-login"]')).to_be_visible()
    assert len(api.accounts) == 1
    await page.locator('[data-action="retry-login"]').click()
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert [p['provider'] for p in api.starts] == ['anyrouter', 'agentrouter', 'agentrouter']
    assert len(api.accounts) == 2
    await api.close()
    api = API(plans=[{'statuses': ['success']}, {'statuses': ['error']}])
    page = await api.attach(browser, base)
    await add(page)
    await page.locator('#account-submit').click()
    await expect(page.locator('[data-action="skip-login"]')).to_be_visible()
    assert len(api.accounts) == 1
    await page.locator('[data-action="skip-login"]').click()
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert await page.locator('.platform-progress .success').count() == 1
    assert await page.locator('.platform-progress .skipped').count() == 1
    assert len(api.starts) == 2
    await api.close()
    api = API(plans=[{'statuses': ['error']}, {'statuses': ['success']}])
    page = await api.attach(browser, base)
    await add(page)
    await page.locator('#account-submit').click()
    await page.locator('[data-action="skip-login"]').click()
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert api.accounts[0]['provider'] == 'agentrouter'
    await api.close()
    results.append('失败原因、当前平台重试、部分成功与跳过继续')

    # 人工处理期间只查询原会话，显式恢复后保存；暂停时仍可取消整个队列。
    for action in ['resume', 'cancel']:
        api = API(plans=[{'verification_paused': True, 'statuses': ['success']}])
        page = await api.attach(browser, base)
        await add(page, multiple=action == 'cancel')
        await page.locator('#account-submit').click()
        blocked = page.locator('.platform-progress .blocked')
        resume = page.locator('[data-action="resume-login"]')
        cancel = page.locator('[data-action="cancel-login"]')
        await expect(blocked).to_contain_text('等待人工处理')
        await expect(blocked).to_contain_text('请在当前登录窗口完成人工处理')
        await expect(resume).to_have_text('我已处理，重新校验')
        await expect(resume).to_be_enabled()
        await expect(cancel).to_be_enabled()
        await wait_count(api.polled_sessions, 3)
        await expect(blocked).to_be_visible()
        await expect(resume).to_be_visible()
        assert len(api.starts) == 1 and list(api.sessions) == ['1']
        assert set(api.polled_sessions) == {'1'}
        assert not api.accounts and not api.resumes and not api.cancels
        assert await page.locator('[data-action="retry-login"]').count() == 0
        if action == 'resume':
            await resume.click()
            await expect(page.locator('#modal h2')).to_have_text('授权结果')
            await expect(page.locator('.platform-progress .success')).to_have_count(1)
            await expect(page.locator('#nav-count')).to_have_text('1')
            await expect(resume).to_have_count(0)
            assert api.resumes == ['1'] and not api.cancels
            assert not api.sessions['1']['verification_paused']
            assert api.sessions['1']['saved'] and len(api.accounts) == 1
            assert api.accounts[0]['provider'] == 'anyrouter'
            assert api.accounts[0]['login_via'] == 'github'
        else:
            await expect(page.locator('.platform-progress .pending')).to_have_count(1)
            await cancel.click()
            await expect(page.locator('#modal')).not_to_be_visible()
            await wait_count(api.cancels, 1)
            await expect(page.locator('#nav-count')).to_have_text('0')
            assert api.cancels == ['1'] and api.sessions['1']['cancelled']
            assert not api.resumes and not api.accounts
        assert len(api.starts) == 1 and list(api.sessions) == ['1']
        assert set(api.polled_sessions) == {'1'} and not api.creates
        await api.close()
    results.append('人工处理暂停提示、原会话轮询与显式恢复保存、暂停时取消')

    # 请求未返回就关闭/ESC/取消；回包后取消旧会话，不触发下一平台。
    for action in ['close', 'escape', 'cancel']:
        release, entered = asyncio.Event(), asyncio.Event()
        api = API(plans=[{'hold_start': release, 'start_entered': entered, 'statuses': ['success']}])
        page = await api.attach(browser, base)
        await add(page)
        await page.locator('#account-submit').click()
        await asyncio.wait_for(entered.wait(), 5)
        if action == 'escape':
            await page.keyboard.press('Escape')
        else:
            await page.locator(f'[data-action="{"close" if action == "close" else "cancel-login"}"]').first.click()
        await expect(page.locator('#modal')).not_to_be_visible()
        release.set()
        await wait_count(api.cancels, 1)
        await page.wait_for_timeout(100)
        assert len(api.starts) == 1 and not api.accounts
        await api.close()
    results.append('关闭/ESC/取消与start返回竞态')

    # 取消与 success 同时到达，只保留已经完成的账号。
    release, entered = asyncio.Event(), asyncio.Event()
    api = API(plans=[{'hold_poll': release, 'poll_entered': entered, 'statuses': ['success'], 'cancel_success': True}])
    page = await api.attach(browser, base)
    await add(page)
    await page.locator('#account-submit').click()
    await asyncio.wait_for(entered.wait(), 5)
    await page.locator('[data-action="cancel-login"]').click()
    await wait_count(api.cancels, 1)
    release.set()
    await expect(page.locator('#nav-count')).to_have_text('1')
    assert len(api.starts) == 1
    await api.close()
    api = API(plans=[{'statuses': ['success']}, {'statuses': ['waiting']}])
    page = await api.attach(browser, base)
    await add(page)
    await page.locator('#account-submit').click()
    await wait_count(api.starts, 2)
    await expect(page.locator('.platform-progress .success')).to_have_count(1)
    await page.keyboard.press('Escape')
    await wait_count(api.cancels, 1)
    assert len(api.accounts) == 1 and api.accounts[0]['provider'] == 'anyrouter'
    assert len(api.starts) == 2
    await api.close()
    results.append('取消与成功竞态保留已完成账号')

    # 非重叠轮询、双重提交、网络失败只恢复原会话查询。
    release, entered = asyncio.Event(), asyncio.Event()
    api = API(plans=[{'hold_poll': release, 'poll_entered': entered, 'statuses': ['success']}])
    page = await api.attach(browser, base)
    await add(page, multiple=False)
    await page.locator('#account-form').evaluate("form=>{form.requestSubmit();form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));}")
    await asyncio.wait_for(entered.wait(), 5)
    await page.wait_for_timeout(1600)
    assert len(api.starts) == 1 and api.polls == 1
    release.set()
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    await api.close()
    api = API(plans=[{'statuses': ['network', 'success']}])
    page = await api.attach(browser, base)
    await add(page, multiple=False)
    await page.locator('#account-submit').click()
    await page.locator('[data-action="retry-poll"]').click()
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert len(api.starts) == 1 and api.polls == 2
    await api.close()
    api = API(plans=[{'http_error': 503}])
    page = await api.attach(browser, base)
    await add(page)
    await page.locator('#account-submit').click()
    await expect(page.locator('.platform-progress .unknown')).to_be_visible()
    assert await page.locator('[data-action="retry-login"]').count() == 0
    assert len(api.starts) == 1
    await page.keyboard.press('Escape')
    await api.close()
    results.append('无重叠轮询、重复提交与网络不确定结果防重复')

    # 编辑平台禁用；保存显示元数据不重新登录；连接其他平台不复制凭据。
    for method in ['github', 'linuxdo']:
        api = API(accounts=[account(method=method)])
        page = await api.attach(browser, base)
        await page.locator('[data-action="edit"]').click()
        await expect(page.locator('[name="provider"]')).to_be_disabled()
        await page.locator('[name="name"]').fill('独立账号备注')
        await page.locator('#account-submit').click()
        await expect(page.locator('#modal')).not_to_be_visible()
        assert not api.starts and api.updates[-1]['provider'] == 'anyrouter'
        assert api.updates[-1]['auth_mode'] == 'session'
        await page.locator('[data-action="connect-platform"]').click()
        assert await page.locator('[name="name"]').input_value() == '独立账号备注'
        assert await page.locator('[name="providers"]').evaluate_all('els=>els.map(e=>e.value)') == ['agentrouter']
        assert await page.locator(f'[data-method="{method}"]').get_attribute('class') == 'login-method active'
        await page.locator('#account-submit').click()
        await expect(page.locator('#modal h2')).to_have_text('授权结果')
        payload = api.starts[-1]
        assert payload['provider'] == 'agentrouter' and payload['method'] == method
        assert payload['account_id'] is None
        assert not {'session', 'api_user', 'password', 'email'}.intersection(payload)
        await page.locator('[data-action="close"]').last.click()
        await page.locator('[data-action="relogin"]').first.click()
        await expect(page.locator('#modal h2')).to_have_text('授权结果')
        assert api.starts[-1]['provider'] == 'anyrouter' and api.starts[-1]['account_id'] == 1
        await api.close()
    results.append('连接其他平台不传凭据、编辑锁定平台、重授权保持原平台')

    # 认证类型切换必须明确 auth_mode，填写新凭据。
    for original, target in [('github', 'password'), ('password', 'session'), ('github', 'session')]:
        api = API(accounts=[account(method=original)])
        page = await api.attach(browser, base)
        await page.locator('[data-action="edit"]').click()
        await page.locator(f'[data-method="{target}"]').click()
        assert not await page.locator('#account-form').evaluate('form=>form.checkValidity()')
        if target == 'password':
            await page.locator('[name="email"]').fill('isolated@example.invalid')
            await page.locator('[name="password"]').fill('isolated-not-real-password')
        else:
            await page.locator('[name="session"]').fill('isolated-not-real-session')
            await page.locator('[name="api_user"]').fill('100')
        await page.locator('#account-submit').click()
        await expect(page.locator('#modal')).not_to_be_visible()
        assert api.updates[-1]['auth_mode'] == target
        assert api.updates[-1]['login_via'] == target
        await api.close()
    api = API()
    page = await api.attach(browser, base)
    await add(page, method='manual', multiple=False)
    await page.locator('[name="provider"]').select_option('agentrouter')
    await page.locator('#account-submit').click()
    await expect(page.locator('#modal h2')).to_have_text('授权结果')
    assert api.starts[0]['method'] == 'manual' and api.starts[0]['provider'] == 'agentrouter'
    await api.close()
    results.append('认证类型切换新凭据与显式manual浏览器登录')
    return results


async def main():
    SHOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(StaticHandler, directory=str(ROOT / 'web')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        async with async_playwright() as p:
            executable = os.getenv('CODEFUEL_CHROME', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
            browser = await p.chromium.launch(executable_path=executable, headless=True)
            try:
                results = await cases(browser, base)
                print(json.dumps({'checks': results, 'javascript_errors': [], 'horizontal_overflow': False,
                                  'screenshot': str(SHOT / 'ui-platform-login.png')}, ensure_ascii=False, indent=2))
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    asyncio.run(main())
