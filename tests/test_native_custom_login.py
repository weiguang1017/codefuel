import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import native_login as native, login_flow
from backend.providers import PROVIDERS, CustomProviderIn
from backend.store import Store


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate(): await asyncio.sleep(.01)


@pytest.fixture
def window(tmp_path, monkeypatch):
    db=Store(tmp_path/'test.db',tmp_path/'key')
    provider=CustomProviderIn(name='CUN fixture',domain='https://fixture.example',login_path='/sign-in').provider('fixture')
    monkeypatch.setitem(PROVIDERS,'fixture',provider)
    state=SimpleNamespace(pages=[SimpleNamespace(url='https://fixture.example/sign-in',title='Fixture',id='site')],attached=0,closed=False,opens=[])
    target=SimpleNamespace(url='https://fixture.example/console',evaluate=AsyncMock(return_value=0))
    context=SimpleNamespace(pages=[target],cookies=AsyncMock(return_value=[]))
    class Chrome:
        def __init__(self,*args):self.proxy_auth_required=False;self.process=SimpleNamespace(returncode=None)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):state.closed=True
        async def tabs(self):return state.pages
        async def open_target(self,url,**kwargs):state.opens.append(url);state.pages=[SimpleNamespace(url=url,title='Fixture',id='new')]
        @asynccontextmanager
        async def attached(self):
            state.attached+=1
            try:yield SimpleNamespace(),context
            finally:state.attached-=1
    async def prepare(*args):
        state.pages=[SimpleNamespace(url='https://linux.do/session/sso_provider?sig=fixture',title='Just a moment…',id='upstream')]
        return True
    monkeypatch.setattr(native,'NativeChrome',Chrome)
    monkeypatch.setattr(native,'_prepare_native_site',AsyncMock(side_effect=prepare))
    monkeypatch.setattr(native,'POLL_INTERVAL',.01)
    monkeypatch.setattr(native,'RETURN_SETTLE',.02)
    monkeypatch.setattr(login_flow,'verify_target_profile',AsyncMock(return_value={'id':7,'quota':5000000,'used_quota':0}))
    flow=login_flow.LoginFlow(db)
    monkeypatch.setattr(flow,'_session_cookie',AsyncMock(return_value='fixture-session'))
    from backend import protocols
    monkeypatch.setattr(protocols,'resolve_custom_protocol',AsyncMock(return_value=provider))
    state.db,state.flow,state.target,state.context=db,flow,target,context
    yield state
    db.close()


@pytest.mark.asyncio
async def test_custom_linuxdo_is_unattached_through_repeated_challenges_then_saves(window):
    session=window.flow.start('fixture','linuxdo','fixture')
    try:
        await eventually(lambda:window.pages[0].title=='Just a moment…')
        for _ in range(4):
            assert window.attached==0 and not window.db.accounts()
            assert login_flow.verify_target_profile.await_count==0
            assert window.flow.resume_verification(session.id)
            await asyncio.sleep(.025)
        window.pages=[SimpleNamespace(url=window.target.url,title='Fixture console',id='site')]
        await asyncio.wait_for(session.task,3)
        assert session.status=='success' and window.closed and window.attached==0
        assert window.db.credentials(window.db.account(session.account))['session']=='fixture-session'
        assert login_flow.verify_target_profile.await_count==1
        assert 'sig=' not in str(window.flow.view(session.id))
    finally:
        await window.flow.close()


@pytest.mark.asyncio
async def test_native_community_home_can_continue_without_attaching_to_linuxdo(window):
    session=window.flow.start('fixture','linuxdo','fixture')
    try:
        await eventually(lambda:window.pages[0].title=='Just a moment…')
        window.pages=[SimpleNamespace(url='https://linux.do/',title='LINUX DO',id='home')]
        await eventually(lambda:session.oauth_continue_available)
        assert window.attached==0 and window.flow.continue_oauth(session.id)
        await eventually(lambda:bool(window.opens))
        assert window.opens==['https://fixture.example/sign-in']
        assert not window.db.accounts()
    finally:
        await window.flow.close()


@pytest.mark.parametrize('path',['/sign-in','/signin','/login','/sign-up'])
def test_login_aliases_never_count_as_oauth_return(path):
    provider=CustomProviderIn(name='fixture',domain='https://fixture.example').provider('fixture')
    session=login_flow.LoginSession('fixture','linuxdo','fixture')
    context=SimpleNamespace(pages=[SimpleNamespace(url='https://fixture.example'+path)])
    assert login_flow.LoginFlow._oauth_return_pending(session,context,provider)
