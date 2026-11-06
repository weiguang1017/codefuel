"""CodeFuel local-only API and scheduler."""
import asyncio
import csv
import io
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from .store import Store, utcnow, iso
from .engine import CheckinEngine, browser_available
from .login_flow import LoginFlow, LoginConflict, ACTIVE_STATUSES
from .providers import (PROVIDERS, BROWSER_METHODS, CREDENTIAL_METHODS, provider_catalog,
                        supports, CustomProviderIn, load_custom_providers)
from .paths import APP_NAME, APP_VERSION, resource_root
from .proxy import ProxyConfig, normalize_proxy
from .protocols import resolve_custom_protocol
from .site_probe import probe_site

ROOT = resource_root()
CSRF = secrets.token_urlsafe(24)
store = Store()
load_custom_providers(store.provider_configs())
login_flow = LoginFlow(store)


async def reauthenticate_checkin(row, api_user):
    store.patch(row['id'], last_message='正在后台复用已保存的授权会话…')
    return await login_flow.authenticate_for_checkin(row, api_user)


async def resolve_provider(page, provider):
    return await resolve_custom_protocol(page, provider, store)


engine = CheckinEngine(reauthenticate=reauthenticate_checkin, resolve_provider=resolve_provider)
queued = set()
active = set()
jobs = set()


class AccountIn(BaseModel):
    proxy: ProxyConfig | None = None
    name: str = Field(min_length=1, max_length=40)
    provider: str
    auth_mode: str = 'password'
    enabled: bool = True
    interval_hours: int = Field(default=6, ge=1, le=168)
    email: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=300)
    session: str | None = Field(default=None, max_length=32000)
    api_user: str | None = Field(default=None, max_length=100)

    @field_validator('name', 'email', 'session', 'api_user')
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def validate_account(self):
        if self.provider not in PROVIDERS:
            raise ValueError('未支持的平台')
        if self.auth_mode not in CREDENTIAL_METHODS or not supports(self.provider, self.auth_mode):
            raise ValueError('认证方式无效')
        if self.auth_mode == 'password' and (not self.email or not self.password):
            raise ValueError('请填写完整的邮箱和密码')
        if self.auth_mode == 'session' and (not self.session or not self.api_user):
            raise ValueError('请填写完整的 Session 与 API User')
        return self


class SettingsIn(BaseModel):
    scheduler_enabled: bool
    interval_hours: int = Field(ge=1, le=168)


class ProviderProbeIn(BaseModel):
    domain: str = Field(max_length=250)

    @field_validator('domain')
    @classmethod
    def validate_domain(cls, value):
        return CustomProviderIn.validate_domain(value)


class LoginIn(BaseModel):
    proxy: ProxyConfig | None = None
    provider: str
    method: str = 'github'
    name: str | None = Field(default=None, max_length=40)
    interval_hours: int = Field(default=6, ge=1, le=168)
    account_id: int | None = Field(default=None, ge=1)

    @field_validator('name')
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def validate_login(self):
        if self.provider not in PROVIDERS:
            raise ValueError('未支持的平台')
        if self.method not in BROWSER_METHODS or not supports(self.provider, self.method):
            raise ValueError('登录方式无效')
        return self


@asynccontextmanager
async def lifespan(app):
    store.recover_interrupted()
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    pending_jobs = tuple(jobs)
    for job in pending_jobs:
        job.cancel()
    try:
        await login_flow.close()
    finally:
        try:
            if pending_jobs:
                await asyncio.gather(*pending_jobs, return_exceptions=True)
        finally:
            store.close()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    # FastAPI's default errors echo submitted inputs, including proxy passwords.
    if request.url.path.startswith('/api/providers'):
        labels = {'name': '站点名称', 'domain': 'HTTPS 根网址（不含账号、路径或参数）',
                  'template': '签到模式', 'login_path': '登录页面路径', 'login_api_path': '账号密码登录接口路径', 'sign_in_path': '签到接口路径',
                  'user_info_path': '余额接口路径', 'api_user_key': '用户标识请求头（不能使用保留头）',
                  'quota_per_unit': '额度换算值', 'currency': '额度单位'}
        fields = list(dict.fromkeys(item['loc'][-1] for item in error.errors() if item['loc'][-1] in labels))
        detail = ('站点配置无效，请检查：' + '、'.join(labels[field] for field in fields)
                  if fields else '站点配置包含不支持的字段或格式。')
        return JSONResponse(status_code=422, content={'detail': detail,
                            'fields': fields})
    proxy_error = any('proxy' in item['loc'] for item in error.errors())
    return JSONResponse(status_code=422, content={'detail': (
        '代理配置无效：请检查协议、主机和端口；认证信息须单独填写，SOCKS5 仅支持无认证。'
        if proxy_error else '请求信息不完整或格式无效。')})


@app.middleware('http')
async def local_security(request: Request, call_next):
    if request.url.hostname not in ('127.0.0.1', 'localhost', '[::1]'):
        return JSONResponse({'detail': '仅允许本机访问'}, status_code=403)
    origin = request.headers.get('origin')
    if origin and origin != 'null':
        from urllib.parse import urlparse
        if urlparse(origin).hostname not in ('127.0.0.1', 'localhost'):
            return JSONResponse({'detail': '请求来源不受信任'}, status_code=403)
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if request.headers.get('x-csrf-token') != CSRF or request.headers.get('content-type', '').split(';')[0] != 'application/json':
            return JSONResponse({'detail': '本机请求校验失败'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-eval'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    return response


async def scheduler():
    while True:
        try:
            settings = store.settings()
            if settings['scheduler_enabled']:
                for row in store.accounts(enabled_only=True):
                    if row['next_run'] and datetime.fromisoformat(row['next_run']) <= utcnow():
                        enqueue(row['id'], 'scheduled')
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(30)


def enqueue(account_id, trigger):
    with store.lock:
        if account_id in queued or account_id in active or login_flow.is_authorizing(account_id):
            return False
        row = store.account(account_id)
        if not row or row['status'] in ('queued', 'running') or (trigger == 'scheduled' and not row['enabled']):
            return False
        queued.add(account_id)
        store.patch(account_id, status='queued', last_message='已加入签到队列')
        task = asyncio.create_task(execute(account_id, trigger))
        jobs.add(task)
        task.add_done_callback(jobs.discard)
        return True


async def execute(account_id, trigger):
    queued.discard(account_id)
    active.add(account_id)
    row = store.account(account_id)
    run_id = store.begin_run(row, trigger)
    try:
        result = await engine.run(row, store.credentials(row), bool(row['has_snapshot']))
        updated_session = result.pop('_session_update', None)
        if updated_session:
            store.patch(account_id, encrypted=store.encrypt_credentials({**store.credentials(row), **updated_session}))
        store.finish_run(run_id, account_id, result, trigger)
    except Exception:
        store.finish_run(run_id, account_id, {'status': 'failed', 'message': '任务未完成，请稍后重试。'}, trigger)
    finally:
        active.discard(account_id)


@app.get('/api/bootstrap')
def bootstrap():
    available = browser_available()
    with store.lock:
        dashboard = store.dashboard()
        catalog = provider_catalog()
        for provider in catalog:
            provider['account_count'] = sum(a['provider'] == provider['id'] for a in dashboard['accounts'])
            provider['units_locked'] = bool(provider['account_count'] or store.db.execute(
                'SELECT 1 FROM runs WHERE provider=? LIMIT 1', (provider['id'],)).fetchone())
    return {**dashboard, 'csrf_token': CSRF, 'providers': catalog,
            'engine': {'status': 'ready' if available else 'browser_required', 'version': APP_VERSION,
                       'browser_available': available,
                       'login_methods': list(BROWSER_METHODS + CREDENTIAL_METHODS)}}


def editable_provider(provider_id):
    provider = PROVIDERS.get(provider_id)
    if not provider or not provider.active:
        raise HTTPException(404, '站点不存在')
    if not provider.custom:
        raise HTTPException(409, '内置站点不可修改或移除')
    rows = [r for r in store.accounts() if r['provider'] == provider_id]
    if (any(r['id'] in active or r['id'] in queued or r['status'] in ('queued', 'running') for r in rows)
            or any(s.provider == provider_id and s.status in ACTIVE_STATUSES for s in login_flow.sessions.values())):
        raise HTTPException(409, '该站点正在排队、签到或授权，请结束后再修改')
    return provider, rows


def check_duplicate_domain(domain, provider_id=None):
    if any(p.active and p.domain == domain and key != provider_id for key, p in PROVIDERS.items()):
        raise HTTPException(409, '该网址已存在，请直接在已有站点下添加账号')


@app.post('/api/providers', status_code=201)
async def create_provider(data: CustomProviderIn):
    with store.lock:
        check_duplicate_domain(data.domain)
        provider_id = 'custom_' + uuid.uuid4().hex
        store.save_provider(provider_id, data.model_dump())
        PROVIDERS[provider_id] = data.provider(provider_id)
    return {'id': provider_id}


@app.put('/api/providers/{provider_id}')
async def update_provider(provider_id: str, data: CustomProviderIn):
    with store.lock:
        previous, rows = editable_provider(provider_id)
        check_duplicate_domain(data.domain, provider_id)
        if rows and previous.domain != data.domain:
            raise HTTPException(409, '站点已关联账号，不能更改网址；请新增站点并重新授权')
        has_history = store.db.execute('SELECT 1 FROM runs WHERE provider=? LIMIT 1', (provider_id,)).fetchone()
        if (rows or has_history) and (previous.currency != data.currency or previous.quota_per_unit != data.quota_per_unit):
            raise HTTPException(409, '站点已有账号或日志，不能更改额度单位与换算；请新增站点')
        store.save_provider(provider_id, data.model_dump())
        PROVIDERS[provider_id] = data.provider(provider_id)
    return {'ok': True}


@app.delete('/api/providers/{provider_id}')
async def delete_provider(provider_id: str):
    with store.lock:
        previous, rows = editable_provider(provider_id)
        if rows:
            raise HTTPException(409, '请先移除该站点下的账号，再移除站点')
        # Keep the definition for historical names and units; it is no longer selectable.
        config = next(item['config'] for item in store.provider_configs() if item['id'] == provider_id)
        store.save_provider(provider_id, config, active=False)
        PROVIDERS[provider_id] = CustomProviderIn(**config).provider(provider_id, active=False)
    return {'ok': True}


@app.post('/api/providers/probe')
async def probe_provider(data: ProviderProbeIn, request: Request):
    """Read public capabilities without cookies, account credentials or a check-in POST."""
    task = asyncio.create_task(probe_site(data.domain))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.25)
            if not task.done() and await request.is_disconnected():
                raise HTTPException(499, '检测已取消')
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@app.post('/api/login/start', status_code=202)
async def start_login(data: LoginIn):
    try:
        with store.lock:
            if data.account_id in queued or data.account_id in active:
                raise LoginConflict('账号正在排队或运行，暂不能重新授权')
            options = {'proxy': data.proxy} if data.proxy is not None else {}
            session = login_flow.start(data.provider, data.method, data.name, data.account_id, data.interval_hours, **options)
    except LookupError as error:
        raise HTTPException(404, str(error))
    except LoginConflict as error:
        raise HTTPException(409, str(error))
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error))
    return {'id': session.id, 'status': session.status}


@app.get('/api/login/{session_id}')
def login_status(session_id: str):
    view = login_flow.view(session_id)
    if not view:
        raise HTTPException(404, '授权会话不存在或已过期')
    return view


@app.post('/api/login/{session_id}/resume')
async def resume_login(session_id: str):
    if not login_flow.resume_verification(session_id):
        raise HTTPException(409, '当前授权不在等待人工处理状态，请查询最新进度。')
    return {'ok': True, **login_flow.view(session_id)}


@app.post('/api/login/{session_id}/continue-oauth')
async def continue_oauth(session_id: str):
    if not login_flow.continue_oauth(session_id):
        raise HTTPException(409, '当前授权不处于可继续状态，请等待页面更新或重新发起授权。')
    return {'ok': True, **login_flow.view(session_id)}


@app.post('/api/login/{session_id}/cancel')
async def cancel_login(session_id: str):
    cancelled = login_flow.cancel(session_id)
    return {'ok': cancelled, **(login_flow.view(session_id) or {})}


@app.get('/api/health')
def health():
    return {'status': 'ok'}


@app.post('/api/accounts', status_code=201)
def create_account(data: AccountIn):
    try:
        with store.lock:
            if not supports(data.provider, data.auth_mode):
                raise HTTPException(409, '站点已被移除，请重新选择')
            payload = data.model_dump()
            payload['proxy'] = normalize_proxy(data.proxy)
            return {'id': store.add_account(payload)}
    except ValueError:
        raise HTTPException(422, '代理认证信息不完整，请填写代理用户名和密码。') from None


@app.put('/api/accounts/{account_id}')
async def update_account(account_id: int, request: Request, data: dict):
    with store.lock:
        row = store.account(account_id)
        if not row:
            raise HTTPException(404, '账号不存在')
        if data.get('provider', row['provider']) != row['provider']:
            raise HTTPException(409, '已有账号不能更改平台，请单独添加账号')
        if (account_id in active or account_id in queued or row['status'] in ('queued', 'running')
                or login_flow.is_authorizing(account_id)):
            raise HTTPException(409, '账号正在排队、运行或授权，暂不能编辑')
        # 空凭据沿用原值，但不将解密结果返回给客户端。
        old = store.credentials(row)
        merged = {'name': data.get('name', row['name']), 'provider': row['provider'],
                  'auth_mode': data.get('auth_mode', row['auth_mode']), 'enabled': data.get('enabled', bool(row['enabled'])),
                  'interval_hours': data.get('interval_hours', row['interval_hours']), **old}
        for key in ('password', 'session', 'email', 'api_user'):
            if data.get(key):
                merged[key] = data[key]
        try:
            valid = AccountIn(**merged)
            proxy = normalize_proxy(data.get('proxy'), old.get('proxy'))
        except ValueError:
            raise HTTPException(422, '账号信息不完整或格式无效')
        payload = valid.model_dump()
        payload['proxy'] = proxy
        payload['replace_credentials'] = (valid.auth_mode != row['auth_mode'] or
                                          bool(data.get('session') or data.get('api_user')))
        if (data.get('session') or data.get('api_user')) and valid.auth_mode == 'session':
            payload['login_via'] = 'cookie'
        store.update_account(account_id, payload)
    if proxy != old.get('proxy', {'mode': 'default'}):
        await login_flow.discard_browser_sessions(account_id)
    return {'ok': True}


@app.delete('/api/accounts/{account_id}')
async def delete_account(account_id: int):
    with store.lock:
        if (account_id in active or account_id in queued or login_flow.is_authorizing(account_id)
                or not store.delete_account(account_id)):
            raise HTTPException(409, '账号不存在或正在排队、执行任务、授权')
    await login_flow.discard_browser_sessions(account_id)
    return {'ok': True}


@app.post('/api/accounts/{account_id}/run', status_code=202)
async def run_account(account_id: int):
    if not store.account(account_id):
        raise HTTPException(404, '账号不存在')
    if login_flow.is_authorizing(account_id):
        raise HTTPException(409, '该账号正在授权，暂不能运行任务')
    if not enqueue(account_id, 'manual'):
        return {'queued': False, 'message': '该账号已在队列、运行或授权中'}
    return {'queued': True}


@app.post('/api/run-all', status_code=202)
async def run_all():
    return {'queued': sum(enqueue(row['id'], 'manual') for row in store.accounts(enabled_only=True))}


@app.patch('/api/settings')
def update_settings(data: SettingsIn):
    store.update_settings(data.scheduler_enabled, data.interval_hours)
    return {'ok': True}


@app.get('/api/logs/export')
def export_logs():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['时间', '账号', '平台', '状态', '触发方式', '签到前', '签到后', '额度变化', '说明', '额度单位'])
    for row in store.runs(1000):
        values = [row['started_at'], row['account_name'], row['provider'], row['status'], row['trigger'],
                  row['before_balance'], row['after_balance'], row['reward'], row['message'], row['currency']]
        writer.writerow(["'" + str(v) if isinstance(v, str) and v[:1] in ('=', '+', '-', '@') else v for v in values])
    return StreamingResponse(iter(['\ufeff' + output.getvalue()]), media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename=codefuel-logs.csv'})


app.mount('/assets', StaticFiles(directory=ROOT / 'web'), name='assets')


@app.get('/{path:path}')
def index(path: str):
    return FileResponse(ROOT / 'web' / 'index.html')


def main():
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8765, log_level='warning')


if __name__ == '__main__':
    main()
