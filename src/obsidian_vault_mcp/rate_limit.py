"""In-memory rate limiting helpers for authenticated and OAuth requests."""

from collections import deque
from contextvars import ContextVar, Token
import time


_WINDOW_SECONDS = 60.0
_current_auth_principal: ContextVar[str | None] = ContextVar("current_auth_principal", default=None)
_rate_buckets: dict[tuple[str, str], deque[float]] = {}


def set_current_auth_principal(principal: str) -> Token:
    """Bind the current authenticated principal to the request context."""
    return _current_auth_principal.set(principal)


def reset_current_auth_principal(token: Token) -> None:
    """Restore the previous authenticated principal in the request context."""
    _current_auth_principal.reset(token)


def current_auth_principal() -> str | None:
    """Return the current authenticated principal, if any."""
    return _current_auth_principal.get()


def reset_rate_limits() -> None:
    """Clear all rate-limiter state. Intended for tests."""
    _rate_buckets.clear()


def check_rate_limit(scope: str, identifier: str, limit_per_minute: int) -> None:
    """Raise ValueError if the identifier exceeded the configured rate limit."""
    if limit_per_minute <= 0:
        return

    now = time.time()
    cutoff = now - _WINDOW_SECONDS
    key = (scope, identifier)
    bucket = _rate_buckets.setdefault(key, deque())

    while bucket and bucket[0] <= cutoff:
        bucket.popleft()

    if len(bucket) >= limit_per_minute:
        raise ValueError(f"Rate limit exceeded for {scope}; try again in under a minute")

    bucket.append(now)

