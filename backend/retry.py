"""Bounded retries for temporary transport failures; never retry an unknown submission."""
import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


RETRY_DELAYS = (2.0, 5.0)


def transient_error(error):
    if isinstance(error, (TimeoutError, PlaywrightTimeoutError, httpx.TransportError)):
        return True
    if isinstance(error, PlaywrightError):
        return any(reason in str(error) for reason in (
            'net::ERR_CONNECTION_', 'net::ERR_NETWORK_CHANGED', 'net::ERR_INTERNET_DISCONNECTED',
            'net::ERR_NAME_NOT_RESOLVED', 'net::ERR_TIMED_OUT', 'net::ERR_EMPTY_RESPONSE',
            'net::ERR_ABORTED', 'Execution context was destroyed', 'Failed to fetch',
            'net::ERR_PROXY_CONNECTION_FAILED', 'net::ERR_TUNNEL_CONNECTION_FAILED',
            'Target page, context or browser has been closed',
        ))
    return False


def transient_status(status):
    return status == 408 or (isinstance(status, int) and 500 <= status <= 599)
