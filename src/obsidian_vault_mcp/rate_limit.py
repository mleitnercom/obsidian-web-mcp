"""Monotonic 60-second windows; quota is consumed only by admitted requests."""
from collections import deque
from contextvars import ContextVar, Token
import math
from threading import Lock
import time
from typing import Any

_WINDOW_SECONDS = 60.0
_current_auth_principal: ContextVar[str | None] = ContextVar('current_auth_principal', default=None)
_current_request_metadata: ContextVar[dict[str, Any] | None] = ContextVar('current_request_metadata', default=None)
_rate_buckets: dict[tuple[str, str], deque[float]] = {}
_rate_lock = Lock()

class RateLimitExceeded(ValueError):
    def __init__(self, scope: str, wait_seconds: float):
        self.scope = scope
        self.retry_after_seconds = max(1, math.ceil(wait_seconds))
        super().__init__(f'Rate limit exceeded for {scope}; try again in {self.retry_after_seconds} seconds')

def set_current_auth_principal(principal: str) -> Token:
    return _current_auth_principal.set(principal)

def reset_current_auth_principal(token: Token) -> None:
    _current_auth_principal.reset(token)

def current_auth_principal() -> str | None:
    return _current_auth_principal.get()

def set_current_request_metadata(metadata: dict[str, Any]) -> Token:
    return _current_request_metadata.set(metadata)

def reset_current_request_metadata(token: Token) -> None:
    _current_request_metadata.reset(token)

def current_request_metadata() -> dict[str, Any] | None:
    return _current_request_metadata.get()

def reset_rate_limits() -> None:
    with _rate_lock:
        _rate_buckets.clear()

def check_rate_limit(scope: str, identifier: str, limit_per_minute: int) -> None:
    if limit_per_minute <= 0:
        return
    # Wall-clock corrections (NTP, RTC mismatch, DST display) must not extend a window.
    with _rate_lock:
        now = time.monotonic()
        cutoff = now - _WINDOW_SECONDS
        bucket = _rate_buckets.setdefault((scope, identifier), deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit_per_minute:
            raise RateLimitExceeded(scope, bucket[0] + _WINDOW_SECONDS - now)
        bucket.append(now)
