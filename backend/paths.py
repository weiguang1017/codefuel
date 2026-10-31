"""Filesystem locations shared by the app, both from source and inside a frozen bundle."""
import os
import shutil
import sys
from pathlib import Path

APP_NAME = '码粮 CodeFuel'
APP_SLUG = 'CodeFuel'
APP_VERSION = '0.4.0'
APP_SLOGAN = '为代码添粮，让灵感续航。'
# 当前数据命名空间。改名前的 DailyQuota 会在首次启动时自动迁移过来。
DATA_NAMESPACE = 'CodeFuel'
LEGACY_NAMESPACE = 'DailyQuota'
LEGACY_DB_NAME = 'dailyquota.db'
DB_NAME = 'codefuel.db'
FROZEN = bool(getattr(sys, 'frozen', False))


def resource_root() -> Path:
    """Read-only assets (web/, vendor/) live next to the code or inside the bundle."""
    if FROZEN:
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def _platform_data_root() -> Path:
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support'
    if os.name == 'nt':
        return Path(os.getenv('APPDATA') or Path.home())
    return Path.home() / '.local' / 'share'


def _default_dir() -> Path:
    return _platform_data_root() / DATA_NAMESPACE


def _legacy_dir() -> Path:
    return _platform_data_root() / LEGACY_NAMESPACE


def migrate_legacy_data(target: Path) -> list[str]:
    """把 DailyQuota 时代的数据库与密钥搬到新目录。

    只在目标为空时迁移，且用复制而非移动：旧目录保留，误操作也能回退。
    返回已迁移的文件名，便于自检展示。
    """
    if os.getenv('CODEFUEL_DATA_DIR') or os.getenv('DAILYQUOTA_DATA_DIR'):
        return []  # 显式指定目录时尊重用户选择，不自动迁移
    legacy = _legacy_dir()
    if not legacy.is_dir() or legacy.resolve() == target.resolve():
        return []
    if any(target.iterdir()) if target.is_dir() else False:
        return []  # 新目录已有数据，绝不覆盖
    target.mkdir(parents=True, exist_ok=True)

    moved = []
    for old_name, new_name in ((LEGACY_DB_NAME, DB_NAME), ('secret.key', 'secret.key')):
        source = legacy / old_name
        if not source.is_file():
            continue
        destination = target / new_name
        shutil.copy2(source, destination)
        if new_name == 'secret.key':
            destination.chmod(0o600)
        moved.append(f'{old_name} → {new_name}')
    return moved


def data_dir() -> Path:
    """Writable state. Never inside the app bundle, which may be read-only."""
    override = os.getenv('CODEFUEL_DATA_DIR') or os.getenv('DAILYQUOTA_DATA_DIR')
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = _default_dir() if FROZEN else resource_root() / 'data'
    path.mkdir(parents=True, exist_ok=True)
    migrate_legacy_data(path)
    return path
