#!/usr/bin/env python3
"""端到端验证打包后的 CodeFuel：仅使用隔离数据目录与明确的占位会话。

不读取、不修改真实账号数据；占位会话必然无效，只会命中 401 分支。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT / 'dist' / 'CodeFuel.app' / 'Contents' / 'MacOS' / 'CodeFuel'
# 可用 CODEFUEL_APP 指向任意打包产物，避免把临时构建目录写死在脚本里。
APP = Path(os.environ.get('CODEFUEL_APP') or DEFAULT_APP)
PORT = 18896
BASE = f'http://127.0.0.1:{PORT}'


def call(path, method='GET', body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    if token:
        request.add_header('X-CSRF-Token', token)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode() or '{}')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or '{}')


def wait_healthy(deadline=60):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            status, _ = call('/api/health')
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, ValueError):
            pass
        time.sleep(0.5)
    return False


def main():
    data_dir = tempfile.mkdtemp(prefix='codefuel-e2e-')
    env = {**os.environ, 'CODEFUEL_DATA_DIR': data_dir}
    env.pop('PYTHONPATH', None)
    process = subprocess.Popen([str(APP), '--port', str(PORT)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not wait_healthy():
            print('服务未就绪，输出如下：')
            process.terminate()
            print(process.communicate(timeout=20)[0])
            return 1

        token = call('/api/bootstrap')[1]['csrf_token']
        ids = []
        for provider in ('anyrouter', 'agentrouter'):
            status, body = call('/api/accounts', 'POST', {
                'name': f'E2E-{provider}-占位', 'provider': provider, 'auth_mode': 'session',
                'session': 'codefuel-e2e-invalid-session', 'api_user': '1', 'interval_hours': 6,
            }, token)
            print(f'创建占位账号 {provider}: HTTP {status} id={body.get("id")}')
            ids.append(body['id'])

        for account_id in ids:
            print(f'触发签到 #{account_id}: {call(f"/api/accounts/{account_id}/run", "POST", {}, token)}')

        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            _, boot = call('/api/bootstrap')
            rows = {row['id']: row for row in boot['accounts']}
            if all(rows[i]['status'] not in ('queued', 'running') for i in ids):
                break
            time.sleep(2)

        _, boot = call('/api/bootstrap')
        rows = {row['id']: row for row in boot['accounts']}
        runs = {run['account_id']: run for run in boot['runs']}
        print()
        for account_id in ids:
            row = rows[account_id]
            run = runs.get(account_id, {})
            print(f'{row["provider"]:12s} status={row["status"]:16s} '
                  f'duration={run.get("duration_ms")}ms')
            print(f'{"":12s} message={run.get("message")}')
            print(f'{"":12s} before={run.get("before_balance")} after={run.get("after_balance")} '
                  f'reward={run.get("reward")}')
        # 占位会话无效；AgentRouter 应通过后台 API 明确失败，不得弹出交互登录窗口。
        agent = rows[ids[1]]
        message = agent.get('last_message') or ''
        interactive = '授权窗口' in message or '正在重新登录' in message
        print(f'\nAgentRouter 后台签到：{"触发了交互授权" if interactive else agent["status"]}')
        ok = agent['status'] in ('manual_required', 'failed') and not interactive
        print('\n[通过] 无效会话在后台明确失败' if ok else '\n[未通过] AgentRouter 未按预期处理无效会话')
        return 0 if ok else 1
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=20)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = ''
        if output and output.strip():
            print('\n--- 应用输出 ---')
            print(output[-2000:])
        print(f'\n隔离数据目录：{data_dir}')


if __name__ == '__main__':
    sys.exit(main())
