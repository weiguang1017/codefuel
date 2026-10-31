"""CodeFuel local-only API and scheduler."""
import asyncio
import csv
import io
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from .store import Store, utcnow, iso
from .engine import CheckinEngine, browser_available
from .login_flow import LoginFlow
from .paths import APP_NAME, APP_VERSION, resource_root

ROOT = resource_root()
CSRF = secrets.token_urlsafe(24)
store = Store()
engine = CheckinEngine()
login_flow = LoginFlow(store)
queued = set()
active = set()


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    provider: str
    auth_mode: str = 'password'
    enabled: bool = True
    interval_hours: int = Field(default=6, ge=1, le=168)
    email: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=300)
    session: str | None = Field(default=None, max_length=8000)
    api_user: str | None = Field(default=None, max_length=100)

    @field_validator('name', 'email', 'session', 'api_user')
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def validate_account(self):
        if self.provider not in ('anyrouter', 'agentrouter'):
            raise ValueError('仅支持 AnyRouter 与 AgentRouter')
        if self.auth_mode not in ('password', 'session'):
            raise ValueError('认证方式无效')
        if self.auth_mode == 'password' and (not self.email or not self.password):
            raise ValueError('请填写完整的邮箱和密码')
        if self.auth_mode == 'session' and (not self.session or not self.api_user):
            raise ValueError('请填写完整的 Session 与 API User')
        return self


class SettingsIn(BaseModel):
    scheduler_enabled: bool
    interval_hours: int = Field(ge=1, le=168)


class LoginIn(BaseModel):
    provider: str
    method: str = 'github'
    name: str | None = Field(default=None, max_length=40)
    interval_hours: int = Field(default=6, ge=1, le=168)
    account_id: int | None = None

    @field_validator('name')
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def validate_login(self):
        if self.provider not in ('anyrouter', 'agentrouter'):
            raise ValueError('仅支持 AnyRouter 与 AgentRouter')
        if self.method not in ('github', 'linuxdo', 'manual'):
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
    store.close()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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
    if account_id in queued or account_id in active:
        return False
    row = store.account(account_id)
    if not row or (trigger == 'scheduled' and not row['enabled']):
        return False
    queued.add(account_id)
    store.patch(account_id, status='queued', last_message='已加入签到队列')
    asyncio.create_task(execute(account_id, trigger))
    return True


async def execute(account_id, trigger):
    queued.discard(account_id)
    active.add(account_id)
    row = store.account(account_id)
    run_id = store.begin_run(row, trigger)
    try:
        result = await engine.run(row, store.credentials(row), bool(row['has_snapshot']))
        store.finish_run(run_id, account_id, result, trigger)
    except Exception:
        store.finish_run(run_id, account_id, {'status': 'failed', 'message': '任务未完成，请稍后重试。'}, trigger)
    finally:
        active.discard(account_id)


@app.get('/api/bootstrap')
def bootstrap():
    available = browser_available()
    return {**store.dashboard(), 'csrf_token': CSRF,
            'engine': {'status': 'ready' if available else 'browser_required', 'version': APP_VERSION,
                       'browser_available': available, 'login_methods': ['github', 'linuxdo', 'password', 'session']}}


@app.post('/api/login/start', status_code=202)
async def start_login(data: LoginIn):
    try:
        session = login_flow.start(data.provider, data.method, data.name, data.account_id, data.interval_hours)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error))
    return {'id': session.id, 'status': session.status}


@app.get('/api/login/{session_id}')
def login_status(session_id: str):
    view = login_flow.view(session_id)
    if not view:
        raise HTTPException(404, '授权会话不存在或已过期')
    return view


@app.post('/api/login/{session_id}/cancel')
def cancel_login(session_id: str):
    return {'ok': login_flow.cancel(session_id)}


@app.get('/api/health')
def health():
    return {'status': 'ok'}


@app.post('/api/accounts', status_code=201)
def create_account(data: AccountIn):
    return {'id': store.add_account(data.model_dump())}


@app.put('/api/accounts/{account_id}')
def update_account(account_id: int, request: Request, data: dict):
    row = store.account(account_id)
    if not row:
        raise HTTPException(404, '账号不存在')
    if account_id in active or account_id in queued:
        raise HTTPException(409, '任务运行中，暂不能编辑')
    # Merge placeholders with existing credentials without ever returning them.
    old = store.credentials(row)
    merged = {'name': data.get('name', row['name']), 'provider': data.get('provider', row['provider']),
              'auth_mode': data.get('auth_mode', row['auth_mode']), 'enabled': data.get('enabled', bool(row['enabled'])),
              'interval_hours': data.get('interval_hours', row['interval_hours']), **old}
    if data.get('password'):
        merged['password'] = data['password']
    if data.get('session'):
        merged['session'] = data['session']
    if data.get('email'):
        merged['email'] = data['email']
    if data.get('api_user'):
        merged['api_user'] = data['api_user']
    valid = AccountIn(**merged)
    payload = valid.model_dump()
    if data.get('session') and valid.auth_mode == 'session':
        payload['login_via'] = 'cookie'
    store.update_account(account_id, payload)
    return {'ok': True}


@app.delete('/api/accounts/{account_id}')
def delete_account(account_id: int):
    if not store.delete_account(account_id):
        raise HTTPException(409, '账号不存在或正在执行任务')
    return {'ok': True}


@app.post('/api/accounts/{account_id}/run', status_code=202)
async def run_account(account_id: int):
    if not store.account(account_id):
        raise HTTPException(404, '账号不存在')
    if not enqueue(account_id, 'manual'):
        return {'queued': False, 'message': '该账号已在队列中'}
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
    writer.writerow(['时间', '账号', '平台', '状态', '触发方式', '签到前', '签到后', '额度变化', '说明'])
    for row in store.runs(1000):
        values = [row['started_at'], row['account_name'], row['provider'], row['status'], row['trigger'],
                  row['before_balance'], row['after_balance'], row['reward'], row['message']]
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
