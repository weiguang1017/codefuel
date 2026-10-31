# -*- mode: python ; coding: utf-8 -*-
"""构建码粮 CodeFuel，只收集桌面运行所需文件。"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).resolve().parent
VENDOR = ROOT / 'vendor' / 'full-upstream'

# 上游仅分发运行源码与 BSD 许可证，不携带仓库、截图、测试或缓存。
vendor_files = [VENDOR / 'checkin.py', VENDOR / 'LICENSE', *sorted((VENDOR / 'utils').glob('*.py'))]
datas = [(str(ROOT / 'web'), 'web')]
datas += [(str(path), str(Path('vendor/full-upstream') / path.relative_to(VENDOR).parent))
          for path in vendor_files]
for package in ('cloakbrowser', 'playwright'):
    datas += copy_metadata(package)

# Playwright 自带 hook 会完整收集 driver（含 Node 和许可证），无需 collect_all。
hiddenimports = [
    'uvicorn.logging', 'uvicorn.loops.asyncio',
    'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on',
    'checkin', 'utils.browser', 'utils.config', 'utils.debug',
    'utils.notify', 'utils.popups', 'utils.proxy',
    'email.mime.text', 'email.mime.multipart',
]
excludes = [
    'pytest', 'tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
    'IPython', 'matplotlib', 'PIL',
    'uvicorn.loops.auto', 'uvicorn.loops.uvloop',
    'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.httptools_impl',
    'uvicorn.protocols.websockets',
    'webview.platforms.android', 'webview.platforms.gtk', 'webview.platforms.qt',
]
if sys.platform == 'darwin':
    hiddenimports += ['webview.platforms.cocoa']
    excludes += ['webview.platforms.winforms', 'webview.platforms.edgechromium',
                 'webview.platforms.mshtml', 'webview.platforms.cef', 'webview.platforms.win32']
elif sys.platform == 'win32':
    hiddenimports += ['webview.platforms.winforms', 'webview.platforms.edgechromium']
    excludes += ['webview.platforms.cocoa', 'webview.platforms.cef']
else:
    raise SystemExit('CodeFuel 桌面构建仅支持 macOS 和 Windows。')

a = Analysis(
    [str(ROOT / 'desktop.py')],
    pathex=[str(ROOT), str(VENDOR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='CodeFuel',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / 'build' / ('icon.icns' if sys.platform == 'darwin' else 'icon.ico')),
)

if sys.platform == 'darwin':
    # 直接组装 .app，避免在 dist 同时复制一份完整 onedir。
    app = BUNDLE(
        exe, a.binaries, a.datas,
        name='CodeFuel.app',
        icon=str(ROOT / 'build' / 'icon.icns'),
        bundle_identifier='local.dailyquota.desktop',
        info_plist={'CFBundleDisplayName': '码粮 CodeFuel'},
    )
else:
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='CodeFuel')
