import asyncio
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend import updater
from backend.updater import UpdateError, app_bundle, parse_release, select_asset, version_key

ASSETS = [
    {'name': 'CodeFuel-1.0.3-setup.exe', 'size': 10,
     'browser_download_url': 'https://github.com/weiguang1017/codefuel/releases/download/v1.0.3/CodeFuel-1.0.3-setup.exe'},
    {'name': 'CodeFuel-macOS-arm64.dmg', 'size': 20,
     'browser_download_url': 'https://github.com/weiguang1017/codefuel/releases/download/v1.0.3/CodeFuel-macOS-arm64.dmg'},
    {'name': 'CodeFuel-macOS-x86_64.dmg', 'size': 30,
     'browser_download_url': 'https://github.com/weiguang1017/codefuel/releases/download/v1.0.3/CodeFuel-macOS-x86_64.dmg'},
]


@pytest.fixture(autouse=True)
def reset_update_state():
    updater._state.update(phase='idle', percent=0, received=0, total=0, message='', version=None)
    yield
    updater._task = None


@pytest.mark.parametrize('older,newer', [
    ('1.0.1', '1.0.3'), ('v1.0.3', 'v1.1.0'), ('1.9.0', '1.10.0'),
    ('1.0.3-rc.1', '1.0.3'), ('1.0.3-rc.1', '1.0.3-rc.2'), ('2.0.0', '10.0.0'),
])
def test_version_ordering(older, newer):
    assert version_key(older) < version_key(newer)
    assert version_key(newer) > version_key(older)


def test_equal_versions_compare_equal():
    assert version_key('v1.0.3') == version_key('1.0.3')


@pytest.mark.parametrize('machine,expected', [('arm64', 'CodeFuel-macOS-arm64.dmg'),
                                              ('x86_64', 'CodeFuel-macOS-x86_64.dmg')])
def test_select_asset_matches_architecture(machine, expected):
    asset = select_asset(ASSETS, machine)
    assert asset['name'] == expected and asset['url'].endswith(expected)


@pytest.mark.parametrize('url', [
    'https://evil.example/weiguang1017/codefuel/releases/download/v1.0.3/CodeFuel-macOS-arm64.dmg',
    'http://github.com/weiguang1017/codefuel/releases/download/v1.0.3/CodeFuel-macOS-arm64.dmg',
    'https://github.com/someone/else/releases/download/v1.0.3/CodeFuel-macOS-arm64.dmg',
])
def test_select_asset_rejects_foreign_download_urls(url):
    assets = [{'name': 'CodeFuel-macOS-arm64.dmg', 'browser_download_url': url, 'size': 1}]
    assert select_asset(assets, 'arm64') is None


def test_select_asset_without_matching_dmg():
    assert select_asset(ASSETS[:1], 'arm64') is None
    assert select_asset([], 'arm64') is None


def release_payload(tag='v1.0.3'):
    return {'tag_name': tag, 'body': '修复若干问题。', 'assets': ASSETS,
            'html_url': f'https://github.com/weiguang1017/codefuel/releases/tag/{tag}'}


def test_parse_release_flags_newer_version(monkeypatch):
    monkeypatch.setattr(updater, 'APP_VERSION', '1.0.1')
    monkeypatch.setattr(updater.sys, 'platform', 'darwin')
    monkeypatch.setattr(updater, 'app_bundle', lambda: Path('/Applications/CodeFuel.app'))
    info = parse_release(release_payload(), 'arm64')
    assert info['has_update'] is True and info['latest'] == '1.0.3' and info['installable'] is True
    assert info['asset']['name'] == 'CodeFuel-macOS-arm64.dmg'
    assert parse_release(release_payload('v1.0.1'), 'arm64')['has_update'] is False
    assert parse_release(release_payload('v0.9.0'), 'arm64')['has_update'] is False


def test_parse_release_without_bundle_is_not_installable(monkeypatch):
    monkeypatch.setattr(updater, 'APP_VERSION', '1.0.1')
    monkeypatch.setattr(updater.sys, 'platform', 'darwin')
    monkeypatch.setattr(updater, 'app_bundle', lambda: None)
    assert parse_release(release_payload(), 'arm64')['installable'] is False


@pytest.mark.parametrize('payload', [{}, {'tag_name': 'latest'}, {'tag_name': 'v1.2'}])
def test_parse_release_rejects_invalid_tag(payload):
    with pytest.raises(UpdateError):
        parse_release(payload)


def test_app_bundle_only_for_packaged_macos_app(monkeypatch):
    monkeypatch.setattr(updater.sys, 'platform', 'darwin')
    exe = '/Applications/CodeFuel.app/Contents/MacOS/CodeFuel'
    assert app_bundle(exe, frozen=True) == Path('/Applications/CodeFuel.app')
    assert app_bundle(exe, frozen=False) is None
    assert app_bundle('/usr/local/bin/codefuel', frozen=True) is None
    monkeypatch.setattr(updater.sys, 'platform', 'win32')
    assert app_bundle(exe, frozen=True) is None


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def test_fetch_latest_reports_network_failure(monkeypatch):
    class Broken(FakeClient):
        async def get(self, url):
            raise httpx.ConnectError('offline')
    monkeypatch.setattr(updater.httpx, 'AsyncClient', Broken)
    with pytest.raises(UpdateError, match='网络'):
        await updater.fetch_latest()


async def test_fetch_latest_parses_github_response(monkeypatch):
    monkeypatch.setattr(updater, 'APP_VERSION', '1.0.1')
    class Ok(FakeClient):
        async def get(self, url):
            assert url == updater.RELEASES_API
            return SimpleNamespace(status_code=200, json=lambda: release_payload())
    monkeypatch.setattr(updater.httpx, 'AsyncClient', Ok)
    info = await updater.fetch_latest('arm64')
    assert info['has_update'] is True and info['latest'] == '1.0.3'


async def test_start_install_requires_packaged_app(monkeypatch):
    monkeypatch.setattr(updater, 'app_bundle', lambda: None)
    with pytest.raises(UpdateError, match='不支持自动更新'):
        await updater.start_install()


async def test_start_install_rejects_concurrent_update(monkeypatch):
    monkeypatch.setattr(updater, 'app_bundle', lambda: Path('/Applications/CodeFuel.app'))
    running = asyncio.create_task(asyncio.sleep(60))
    updater._task = running
    try:
        with pytest.raises(UpdateError, match='正在进行'):
            await updater.start_install()
    finally:
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass


async def test_start_install_rejects_when_already_latest(monkeypatch):
    monkeypatch.setattr(updater, 'app_bundle', lambda: Path('/Applications/CodeFuel.app'))
    monkeypatch.setattr(updater, 'APP_VERSION', '1.0.1')
    monkeypatch.setattr(updater, 'fetch_latest', AsyncMock(return_value=parse_release(release_payload('v1.0.1'), 'arm64')))
    with pytest.raises(UpdateError, match='最新版本'):
        await updater.start_install()


def test_update_api_endpoints(monkeypatch):
    import backend.main as main
    from fastapi.testclient import TestClient
    info = {'current': '1.0.1', 'latest': '1.0.3', 'has_update': True, 'installable': False,
            'release_url': updater.RELEASES_PAGE, 'notes': '', 'asset': None}
    monkeypatch.setattr(updater, 'fetch_latest', AsyncMock(return_value=info))
    client = TestClient(main.app, base_url='http://127.0.0.1')
    assert client.get('/api/update/check').json() == info
    monkeypatch.setattr(updater, 'fetch_latest', AsyncMock(side_effect=UpdateError('无法连接 GitHub 检查更新，请检查网络后重试。')))
    assert client.get('/api/update/check').status_code == 502
    monkeypatch.setattr(updater, 'start_install', AsyncMock(side_effect=UpdateError('当前运行方式不支持自动更新，请到发布页手动下载安装。')))
    response = client.post('/api/update/install', headers={'X-CSRF-Token': main.CSRF}, json={})
    assert response.status_code == 409 and '不支持自动更新' in response.json()['detail']
    assert client.post('/api/update/install', json={}).status_code == 403
    assert client.get('/api/update/progress').json()['phase'] == 'idle'


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS 安装流程依赖 hdiutil/ditto/xattr')
async def test_install_replaces_bundle_clears_quarantine_and_schedules_restart(tmp_path, monkeypatch):
    """用假 app 与本地 DMG 走完整安装链路：下载→挂载→ditto→xattr→替换→重启脚本。"""
    bundle = tmp_path / 'apps' / 'CodeFuel.app'
    executable = bundle / 'Contents' / 'MacOS' / 'CodeFuel'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\necho old\n')
    executable.chmod(0o755)

    source = tmp_path / 'src' / 'CodeFuel.app'
    new_executable = source / 'Contents' / 'MacOS' / 'CodeFuel'
    new_executable.parent.mkdir(parents=True)
    new_executable.write_text('#!/bin/sh\necho new\n')
    new_executable.chmod(0o755)
    subprocess.run(['xattr', '-w', 'com.apple.quarantine', '0081;00000000;test;', str(source)], check=True)
    dmg = tmp_path / 'update.dmg'
    subprocess.run(['hdiutil', 'create', '-srcfolder', str(tmp_path / 'src'), '-volname', 'CodeFuel',
                    '-format', 'UDZO', str(dmg)], check=True, capture_output=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = dmg.read_bytes()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    release = {'latest': '9.9.9', 'asset': {'name': 'CodeFuel-macOS-arm64.dmg', 'size': dmg.stat().st_size,
                                           'url': f'http://127.0.0.1:{server.server_port}/update.dmg'}}
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(updater, 'app_bundle', lambda: bundle)
    monkeypatch.setattr(updater, 'data_dir', lambda: data)
    exits, popens = [], []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(updater.os, '_exit', fake_exit)

    class SubprocessShim:
        DEVNULL = subprocess.DEVNULL
        run = staticmethod(subprocess.run)

        @staticmethod
        def Popen(*args, **kwargs):
            popens.append((args, kwargs))
            return SimpleNamespace(pid=1)

    monkeypatch.setattr(updater, 'subprocess', SubprocessShim)
    try:
        with pytest.raises(SystemExit):
            await updater._install(release)
    finally:
        server.shutdown()

    assert executable.read_text() == '#!/bin/sh\necho new\n'
    assert not bundle.with_name(bundle.name + '.previous').exists()
    assert not list(data.iterdir())  # 临时目录已清理
    assert exits == [0]
    script = popens[0][0][0][2]
    assert 'kill -0' in script and str(bundle) in script and '/usr/bin/open' in script
    assert popens[0][1]['start_new_session'] is True
    probe = subprocess.run(['xattr', '-p', 'com.apple.quarantine', str(bundle)], capture_output=True)
    assert probe.returncode != 0  # 隔离标记已被 xattr -dr 去除
    assert updater.progress()['phase'] == 'restarting'