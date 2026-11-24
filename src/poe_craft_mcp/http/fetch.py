"""Polite cached JSON GETs: minimum refresh interval, ETags, stale fallback.

Used for poe.ninja. The trade client has its own limiter and uses `ResponseCache` directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from poe_craft_mcp.http.cache import CachedResponse, ResponseCache

log = logging.getLogger(__name__)


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


class CachedHttpClient:
    """Wraps an `httpx.AsyncClient`; every GET goes through `ResponseCache`."""

    def __init__(
        self,
        cache: ResponseCache,
        user_agent: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.cache = cache
        self.user_agent = user_agent
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def get_json(
        self, url: str, params: dict[str, Any] | None = None, min_interval_s: float = 0.0
    ) -> FetchResult:
        """GET `url` as JSON, reusing the cached copy if it is younger than `min_interval_s`.

        Raises `FetchError` on failure when nothing is cached.
        """
        key = str(httpx.URL(url, params=params))
        cached = await self.cache.get(key)
        if cached is not None and cached.age_seconds < min_interval_s:
            return _from_cached(cached, stale=False)

        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag
        try:
            response = await self._http().get(key, headers=headers)
        except httpx.HTTPError as exc:
            return self._fallback(key, cached, f"{type(exc).__name__}: {exc}")

        status = response.status_code
        if status == 304 and cached is not None:
            await self.cache.touch(key)
            return _from_cached(cached, stale=False, age=0.0)
        if status == 429 or status >= 500:
            return self._fallback(key, cached, f"HTTP {status}", status)
        if status != 200:
            raise FetchError(key, f"HTTP {status}", status)
        try:
            data = response.json()
        except ValueError:
            return self._fallback(key, cached, "response body is not JSON", status)
        await self.cache.put(key, response.content, status, response.headers.get("ETag"))
        return FetchResult(data, 0.0, from_cache=False, stale=False, source_url=key)

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
