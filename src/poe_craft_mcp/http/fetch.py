"""Polite cached JSON GETs: minimum refresh interval, ETags, stale fallback.

Used for poe.ninja. The trade client has its own limiter and uses `ResponseCache` directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from poe_craft_mcp.http.cache import CachedResponse, ResponseCache

log = logging.getLogger(__name__)

FAILURE_BACKOFF_S = 60.0
"""Minimum wait before retrying a URL whose last fetch failed."""


class FetchError(Exception):
    """The request failed and there was no cached copy to fall back on."""

    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"GET {url} failed: {reason}")
        self.url = url
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    """Decoded JSON plus provenance. `stale=True` means upstream failed and this is old data."""

    data: Any
    age_seconds: float
    from_cache: bool
    stale: bool
    source_url: str


def _from_cached(cached: CachedResponse, *, stale: bool, age: float | None = None) -> FetchResult:
    return FetchResult(
        data=cached.json(),
        age_seconds=cached.age_seconds if age is None else age,
        from_cache=True,
        stale=stale,
        source_url=cached.url,
    )


def cache_key(url: str, params: Mapping[str, Any] | None = None) -> str:
    """The full request URL: the URL's own query merged with `params`, sorted for stability."""
    base = httpx.URL(url)
    merged = base.params.merge(params or {})
    return str(base.copy_with(params=httpx.QueryParams(sorted(merged.multi_items()))))


class CachedHttpClient:
    """Wraps an `httpx.AsyncClient`; every GET goes through `ResponseCache`.

    Callers arriving while a request for the same URL is pending share its result.
    A failed fetch sets a retry deadline of `now + max(min_interval_s, failure_backoff_s)`
    that every caller honours; until then they get the stale copy (or `FetchError`).
    """

    def __init__(
        self,
        cache: ResponseCache,
        user_agent: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        failure_backoff_s: float = FAILURE_BACKOFF_S,
    ) -> None:
        self.cache = cache
        self.user_agent = user_agent
        self.failure_backoff_s = max(failure_backoff_s, FAILURE_BACKOFF_S)
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._inflight: dict[str, asyncio.Task[FetchResult]] = {}
        self._retry_at: dict[str, float] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def get_json(
        self, url: str, params: Mapping[str, Any] | None = None, min_interval_s: float = 0.0
    ) -> FetchResult:
        """GET `url` as JSON, reusing the cached copy if it is younger than `min_interval_s`.

        Raises `FetchError` on failure when nothing is cached.
        """
        key = cache_key(url, params)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(key, min_interval_s))
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        # shield: one caller being cancelled must not cancel the fetch others are awaiting
        return await asyncio.shield(task)

    def _finished(self, key: str, task: asyncio.Task[FetchResult]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # mark retrieved even if every awaiting caller was cancelled

    async def _fetch(self, key: str, min_interval_s: float) -> FetchResult:
        cached = await self.cache.get(key)
        if cached is not None and cached.age_seconds < min_interval_s:
            return _from_cached(cached, stale=False)
        if self.cache.clock() < self._retry_at.get(key, float("-inf")):
            return self._fallback(key, cached, "backing off after a recent failure")

        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag
        try:
            response = await self._http().get(key, headers=headers)
        except httpx.HTTPError as exc:
            return self._failed(key, cached, min_interval_s, f"{type(exc).__name__}: {exc}")

        status = response.status_code
        if status == 304 and cached is not None:
            self._retry_at.pop(key, None)
            await self.cache.touch(key)
            return _from_cached(cached, stale=False, age=0.0)
        if status == 429 or status >= 500:
            return self._failed(key, cached, min_interval_s, f"HTTP {status}", status)
        if status != 200:
            raise FetchError(key, f"HTTP {status}", status)
        try:
            data = response.json()
        except ValueError:
            return self._failed(key, cached, min_interval_s, "response body is not JSON", status)
        self._retry_at.pop(key, None)
        await self.cache.put(key, response.content, status, response.headers.get("ETag"))
        return FetchResult(data, 0.0, from_cache=False, stale=False, source_url=key)

    def _failed(
        self,
        url: str,
        cached: CachedResponse | None,
        min_interval_s: float,
        reason: str,
        status: int | None = None,
    ) -> FetchResult:
        backoff = max(min_interval_s, self.failure_backoff_s)
        self._retry_at[url] = max(self._retry_at.get(url, 0.0), self.cache.clock() + backoff)
        return self._fallback(url, cached, reason, status)

    def _fallback(
        self, url: str, cached: CachedResponse | None, reason: str, status: int | None = None
    ) -> FetchResult:
        if cached is None:
            raise FetchError(url, reason, status)
        log.warning(
            "GET %s failed (%s); serving cached copy %.0fs old", url, reason, cached.age_seconds
        )
        return _from_cached(cached, stale=True)

    async def aclose(self) -> None:
        """Close the owned httpx client; a later request opens a new one."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> CachedHttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
