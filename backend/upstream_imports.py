"""Single place that imports the vendored check-in helpers (BSD-2-Clause).

Works from a source checkout and from a frozen desktop bundle, where the same
files are shipped under vendor/full-upstream.
"""
import sys

from .paths import resource_root

UPSTREAM = resource_root() / 'vendor' / 'full-upstream'
if UPSTREAM.is_dir() and str(UPSTREAM) not in sys.path:
    sys.path.insert(0, str(UPSTREAM))

from checkin import login_with_credentials  # noqa: E402,F401
from utils.browser import (  # noqa: E402,F401
    has_session_cookie,
    prepare_browser_page,
    verify_browser_login,
    wait_for_waf_ready,
)
from utils.config import ProviderConfig  # noqa: E402,F401
from utils.proxy import get_playwright_proxy  # noqa: E402,F401
