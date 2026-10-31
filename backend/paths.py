"""Filesystem locations shared by the app, both from source and inside a frozen bundle."""
import os
import sys
from pathlib import Path

APP_NAME = '码粮 CodeFuel'
APP_SLUG = 'CodeFuel'
APP_VERSION = '0.3.0'
APP_SLOGAN = '为代码添粮，让灵感续航。'
# Keep the existing encrypted database and key reachable after the brand change.
DATA_NAMESPACE = 'DailyQuota'
FROZEN = bool(getattr(sys, 'frozen', False))


def resource_root() -> Path:
    """Read-only assets (web/, vendor/) live next to the code or inside the bundle."""
    if FROZEN:
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """Writable state. Never inside the app bundle, which may be read-only."""
    override = os.getenv('CODEFUEL_DATA_DIR') or os.getenv('DAILYQUOTA_DATA_DIR')
    if override:
        path = Path(override)
    elif FROZEN:
        if sys.platform == 'darwin':
            path = Path.home() / 'Library' / 'Application Support' / DATA_NAMESPACE
        elif os.name == 'nt':
            path = Path(os.getenv('APPDATA') or Path.home()) / DATA_NAMESPACE
        else:
            path = Path.home() / '.local' / 'share' / DATA_NAMESPACE
    else:
        path = resource_root() / 'data'
    path.mkdir(parents=True, exist_ok=True)
    return path
