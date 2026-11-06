import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright

from backend import native_login
from backend.engine import LOCAL_CHROME
from backend.login_flow import LoginFlow, LoginSession
from backend.providers import CustomProviderIn, PROVIDERS
from backend.store import Store


@pytest.mark.asyncio
@pytest.mark.parametrize('builtin', [False, True], ids=['custom-discovery', 'agentrouter-config'])
async def test_discovery_terms_and_disconnect_happen_before_upstream_response(tmp_path, monkeypatch, builtin):
    if not LOCAL_CHROME: pytest.skip('Local Chrome required')
    db=Store(tmp_path/'state.db',tmp_path/'key')
    config=CustomProviderIn(name='fixture',domain='https://fixture.example')
    provider=(replace(PROVIDERS['agentrouter'],domain=config.domain) if builtin else config.provider('fixture'))
    monkeypatch.setitem(PROVIDERS,provider.name,provider)
    if not builtin: db.save_provider(provider.name,config.model_dump())
    flow=LoginFlow(db)
    session=LoginSession(provider.name,'linuxdo','fixture')
    session.status='waiting'
    held=asyncio.Event();entered=asyncio.Event()
    state=SimpleNamespace(attached=0,client=None)
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(executable_path=LOCAL_CHROME,headless=True)
        context=await browser.new_context()
        async def route_request(route):
            url=urlsplit(route.request.url)
            if url.netloc=='connect.linux.do':
                state.client=parse_qs(url.query).get('client_id', [None])[0]
                entered.set();await held.wait()
                await route.fulfill(content_type='text/html',body='<h1>Human verification</h1>');return
            if url.netloc!='fixture.example':await route.abort();return
            if builtin:
                body='''<meta charset="utf-8"><script>
                localStorage.removeItem('status');
                setTimeout(()=>localStorage.setItem('status',JSON.stringify({linuxdo_client_id:'fixture-client'})),750);
                </script><button onclick="location.href='https://connect.linux.do/oauth2/authorize?client_id='+JSON.parse(localStorage.getItem('status')||'{}').linuxdo_client_id">使用 LinuxDO 继续</button>'''
            elif url.path=='/login':body='<h1>404</h1>'
            elif url.path=='/':body='<a href="/sign-in">登录</a>'
            else:
                body='''<meta charset="utf-8"><script>localStorage.setItem('status',JSON.stringify({system_name:'fixture',linuxdo_oauth:true}))</script>
                <label><input type="checkbox" id="agree" onchange="document.querySelector('#oauth').disabled=!this.checked">我已阅读并同意用户协议和隐私政策</label>
                <button id="oauth" disabled onclick="location.href='https://connect.linux.do/oauth2/authorize?state=fixture'">使用 LinuxDO 继续</button>'''
            await route.fulfill(content_type='text/html',body=body)
        await context.route('**/*',route_request)
        page=await context.new_page();await page.goto('https://fixture.example/login')
        context.add_init_script=AsyncMock(side_effect=AssertionError('Native setup must not leave init scripts on upstream pages'))
        class Chrome:
            async def tabs(self):return [SimpleNamespace(url=page.url,title='Fixture')]
            @asynccontextmanager
            async def attached(self):
                state.attached+=1
                try:yield browser,context
                finally:state.attached-=1
        try:
            result=await asyncio.wait_for(native_login._prepare_native_site(Chrome(),flow,session,provider),8)
            assert state.attached==0
            await asyncio.wait_for(entered.wait(),2)
            assert result is True and not held.is_set()
            if builtin:
                assert state.client=='fixture-client'
                assert session.oauth_client_id=='fixture-client'
            else:
                assert PROVIDERS['fixture'].login_path=='/sign-in'
                assert db.provider_configs()[0]['config']['login_path']=='/sign-in'
            context.add_init_script.assert_not_awaited()
        finally:
            held.set();await browser.close();db.close()
