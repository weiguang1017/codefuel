"""Check GitHub Releases for a newer version; on macOS download, install and relaunch."""
import asyncio
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .paths import APP_VERSION, FROZEN, data_dir
from .version import normalize_version

RELEASES_API = 'https://api.github.com/repos/weiguang1017/codefuel/releases/latest'
RELEASES_PAGE = 'https://github.com/weiguang1017/codefuel/releases'
REPO_DOWNLOAD_PREFIX = '/weiguang1017/codefuel/releases/download/'
HEADERS = {'Accept': 'application/vnd.github+json', 'User-Agent': 'CodeFuel-Updater'}


class UpdateError(Exception):
    pass


def version_key(value):
    version = normalize_version(value)
    core, _, pre = version.partition('-')
    pre = pre.split('+')[0]
    numbers = tuple(int(part) for part in core.split('.'))
    return numbers, 0 if pre else 1, pre


def app_bundle(executable=None, frozen=None):
    """The .app directory of a packaged macOS build, or None when auto-install is impossible."""
    frozen = FROZEN if frozen is None else frozen
    if not frozen or sys.platform != 'darwin':
        return None
    exe = Path(executable or sys.executable).resolve()
    if exe.parent.name == 'MacOS' and exe.parent.parent.name == 'Contents' and exe.parents[2].suffix == '.app':
        return exe.parents[2]
    return None


def select_asset(assets, machine=None):
    machine = machine or platform.machine()
    arch = 'arm64' if machine == 'arm64' else 'x86_64'
    wanted = f'CodeFuel-macOS-{arch}.dmg'
    for asset in assets or []:
        if asset.get('name') != wanted:
            continue
        url = asset.get('browser_download_url') or ''
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname != 'github.com' or not parts.path.startswith(REPO_DOWNLOAD_PREFIX):
            continue
        return {'name': asset['name'], 'url': url, 'size': asset.get('size') or 0}
    return None


def parse_release(payload, machine=None):
    try:
        latest = normalize_version(payload['tag_name'])
    except (KeyError, TypeError, ValueError):
        raise UpdateError('发布页返回的版本信息无效，请稍后重试。') from None
    asset = select_asset(payload.get('assets'), machine)
    notes = (payload.get('body') or '').strip()
    return {'current': APP_VERSION, 'latest': latest,
            'has_update': version_key(latest) > version_key(APP_VERSION),
            'installable': sys.platform == 'darwin' and app_bundle() is not None and asset is not None,
            'release_url': payload.get('html_url') or RELEASES_PAGE,
            'notes': notes[:1500], 'asset': asset}


async def fetch_latest(machine=None):
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=HEADERS, trust_env=False) as client:
            response = await client.get(RELEASES_API)
    except httpx.HTTPError:
        raise UpdateError('无法连接 GitHub 检查更新，请检查网络后重试。') from None
    if response.status_code == 404:
        raise UpdateError('仓库还没有正式发布版本。')
    if response.status_code != 200:
        raise UpdateError(f'检查更新失败（GitHub 返回 {response.status_code}），请稍后重试。')
    try:
        payload = response.json()
    except ValueError:
        raise UpdateError('发布页返回内容无效，请稍后重试。') from None
    return parse_release(payload, machine)


_state = {'phase': 'idle', 'percent': 0, 'received': 0, 'total': 0, 'message': '', 'version': None}
_task = None


def progress():
    return dict(_state)


def _set(**fields):
    _state.update(fields)


async def _run(*args, timeout=300):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise UpdateError(f'安装步骤超时：{args[0]}') from None
    if process.returncode != 0:
        detail = stderr.decode(errors='replace').strip().splitlines()
        raise UpdateError(f'安装步骤失败（{args[0]}）：{detail[-1] if detail else "未知错误"}')


async def _download(client, asset, destination):
    async with client.stream('GET', asset['url']) as response:
        if response.status_code != 200:
            raise UpdateError(f'下载安装包失败（HTTP {response.status_code}），请稍后重试。')
        total = int(response.headers.get('content-length') or 0) or asset['size']
        _set(phase='downloading', percent=0, received=0, total=total)
        received = 0
        with destination.open('wb') as output:
            async for chunk in response.aiter_bytes(1024 * 512):
                output.write(chunk)
                received += len(chunk)
                _set(received=received, percent=min(99, int(received * 100 / total)) if total else 0)
    if asset['size'] and received != asset['size']:
        raise UpdateError('下载的安装包大小与发布页不一致，已中止安装。')
    _set(percent=100)


async def _install(release):
    bundle = app_bundle()
    asset = release['asset']
    work = Path(tempfile.mkdtemp(prefix='codefuel-update-', dir=str(data_dir())))
    mount = work / 'mount'
    mounted = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True,
                                     headers={'User-Agent': 'CodeFuel-Updater'}, trust_env=False) as client:
            await _download(client, asset, work / 'update.dmg')

        _set(phase='installing', message='正在校验并安装新版本…')
        mount.mkdir()
        await _run('hdiutil', 'attach', '-nobrowse', '-readonly', '-mountpoint', str(mount), str(work / 'update.dmg'))
        mounted = True
        source = mount / 'CodeFuel.app'
        if not source.is_dir():
            raise UpdateError('安装包中没有找到 CodeFuel.app，已中止安装。')
        staged = work / 'CodeFuel.app'
        await _run('ditto', str(source), str(staged))
        await _run('hdiutil', 'detach', str(mount), '-quiet')
        mounted = False
        # 去掉下载产物的隔离标记，避免重启后被 Gatekeeper 拦截。
        await _run('xattr', '-dr', 'com.apple.quarantine', str(staged))

        target = bundle if os.access(bundle.parent, os.W_OK) else Path('/Applications') / bundle.name
        if not os.access(target.parent, os.W_OK):
            raise UpdateError('没有权限写入应用所在目录，请手动下载安装。')
        previous = target.with_name(target.name + '.previous')
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        existed = target.exists()
        if existed:
            os.rename(target, previous)
        try:
            await _run('ditto', str(staged), str(target))
        except BaseException:
            if existed and not target.exists():
                os.rename(previous, target)
            raise
        if existed:
            shutil.rmtree(previous, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

        _set(phase='restarting', message=f'v{release["latest"]} 安装完成，应用即将重启。', version=release['latest'])
        script = (f'while /bin/kill -0 {os.getpid()} 2>/dev/null; do /bin/sleep 0.2; done; '
                  f'/usr/bin/open {shlex.quote(str(target))}')
        subprocess.Popen(['/bin/sh', '-c', script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(1.5)  # 让前端有机会读到 restarting 状态
        os._exit(0)
    finally:
        if mounted:
            subprocess.run(['hdiutil', 'detach', str(mount), '-quiet'], capture_output=True, check=False)
        shutil.rmtree(work, ignore_errors=True)


async def _guarded_install(release):
    try:
        await _install(release)
    except asyncio.CancelledError:
        _set(phase='error', message='更新已取消。')
        raise
    except UpdateError as error:
        _set(phase='error', message=str(error))
    except Exception:
        _set(phase='error', message='更新过程中出现意外错误，请稍后重试或到发布页手动下载。')
    finally:
        global _task
        _task = None


async def start_install():
    global _task
    if _task is not None and not _task.done():
        raise UpdateError('更新正在进行中，请等待当前进度完成。')
    if app_bundle() is None:
        raise UpdateError('当前运行方式不支持自动更新，请到发布页手动下载安装。')
    release = await fetch_latest()
    if not release['has_update']:
        raise UpdateError('当前已是最新版本，无需更新。')
    if not release['asset']:
        raise UpdateError('此版本没有适配本机架构的 macOS 安装包，请到发布页查看。')
    _set(phase='downloading', percent=0, received=0, total=release['asset']['size'], message='', version=release['latest'])
    _task = asyncio.create_task(_guarded_install(release))
    return {'ok': True, 'version': release['latest']}


async def shutdown():
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass