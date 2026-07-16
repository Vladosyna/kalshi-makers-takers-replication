"""Shared HTTP plumbing: token bucket + retrying JSON GET requests.

`time.monotonic()` here is not a timestamp source -- it only meters request
spacing; `now_utc()` (util.py) remains the sole wall-clock call.

Deliberately GET-only: every in-scope Kalshi market-data endpoint (/markets,
/trades, /series, /*/candlesticks, /historical/*) is GET per Kalshi's
OpenAPI spec. There is no `post_json` here -- one less code path that could
ever be repurposed toward the order-placement/portfolio-mutation surface,
reinforcing what tests/test_scope.py already keeps out structurally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(20.0)


class TokenBucket:
    """Async token bucket: `rate` tokens/sec, holding at most `burst`."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class BaseClient:
    """Thin async JSON GET client over one base URL, rate-limited and retrying."""

    def __init__(self, base_url: str, bucket: TokenBucket) -> None:
        self._bucket = bucket
        self._client = httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_random_exponential(multiplier=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._bucket.acquire()
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()
