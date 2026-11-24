"""pathofexile.com trade client: saved URL to query, instant-buyout search, fetch.

Every search is restricted to instant-buyout (asynchronous trade) listings, status
`securable`, whatever status a saved URL or query asks for; fetched listings without the
async-trade fee are dropped. Bulk exchange URLs are refused: the exchange only lists
whisper offers.

Every request goes through one `RateLimiter`. Results of `listings_for` are cached per
query hash and league for `trade_cache_min`; when the limiter refuses, the cached copy is
returned with `stale=True`. The optional POESESSID is only ever sent as a cookie, and only
to the official HTTPS origin.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from poe_craft_mcp.clients.trade_models import (
    EXCHANGE_REFUSED,
    SearchResult,
    TradeError,
    TradeQuery,
    contains_text,
    decode_search_id,
    is_instant_buyout,
    normalize_listing,
    parse_saved_url,
    query_from_body,
    query_hash,
    with_instant_buyout,
)
from poe_craft_mcp.config import Config
from poe_craft_mcp.http.cache import CachedResponse, ResponseCache
from poe_craft_mcp.http.ratelimit import RateLimited, RateLimiter, setup_state_log
from poe_craft_mcp.log import register_secret
from poe_craft_mcp.secrets import get_poesessid

if TYPE_CHECKING:
    from poe_craft_mcp.context import AppContext

log = logging.getLogger(__name__)

__all__ = [
    "NotCached",
    "RateLimited",
    "SearchResult",
    "TradeClient",
    "TradeError",
    "TradeQuery",
    "get_trade",
]

SEARCH_PATH = "/api/trade/search/{league}"
SAVED_PATH = "/api/trade/{kind}/{league}/{id}"
FETCH_PATH = "/api/trade/fetch/{ids}"
FETCH_BATCH = 10
PRICE_SORT = {"price": "asc"}
COOKIE_ORIGINS = frozenset({("https", "www.pathofexile.com", 443)})


class NotCached(TradeError):
    """A cache-only lookup found nothing cached; no request was made."""


def _seg(value: str) -> str:
    return quote(value, safe="")


def _from_cache(cached: CachedResponse, *, stale: bool) -> dict[str, Any]:
    return {**cached.json(), "age_seconds": cached.age_seconds, "from_cache": True, "stale": stale}


class TradeClient:
    def __init__(
        self,
        config: Config,
        cache: ResponseCache,
        *,
        limiter: RateLimiter | None = None,
        client: httpx.AsyncClient | None = None,
        poesessid: Callable[[], str | None] = get_poesessid,
    ) -> None:
        self.config = config
        self.cache = cache
        self.limiter = limiter or RateLimiter(clock=cache.clock)
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._poesessid = poesessid
        self._session: str | None = None
        self._session_loaded = False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _cookie(self, url: httpx.URL) -> str | None:
        port = url.port if url.port is not None else {"https": 443, "http": 80}.get(url.scheme)
        if (url.scheme, url.host, port) not in COOKIE_ORIGINS:
            return None
        if not self._session_loaded:
            self._session = await asyncio.to_thread(self._poesessid)
            register_secret(self._session)
            self._session_loaded = True
        return self._session

    async def _request(
        self,
        kind: str,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Any = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        url = httpx.URL(self.config.trade_base_url + path)
        headers = {"User-Agent": self.config.user_agent, "Accept": "application/json"}
        session = await self._cookie(url)
        if session:
            headers["Cookie"] = f"POESESSID={session}"

        async def send() -> httpx.Response:
            return await self._client.request(
                method, url, json=body, params=params, headers=headers, follow_redirects=False
            )

        try:
            response = await self.limiter.run(kind, send, deadline=deadline)
        except httpx.HTTPError as exc:
            raise TradeError(f"{method} {path} failed: {type(exc).__name__}") from None
        status = response.status_code
        if session and session in response.text:
            raise TradeError(f"{method} {path}: response echoed the session cookie", status)
        if 300 <= status < 400:
            raise TradeError(f"{method} {path}: redirect refused (HTTP {status})", status)
        try:
            data = response.json()
        except ValueError:
            data = None
        if status >= 400:
            error = data.get("error") if isinstance(data, dict) else None
            detail = error.get("message") if isinstance(error, dict) else None
            raise TradeError(f"{method} {path}: HTTP {status} {detail or ''}".rstrip(), status)
        if not isinstance(data, dict):
            raise TradeError(f"{method} {path}: unexpected response body", status)
        if session and contains_text(data, session):
            raise TradeError(f"{method} {path}: response echoed the session cookie", status)
        return data

    async def resolve_saved_url(
        self, url: str, *, deadline: float | None = None, cache_only: bool = False
    ) -> TradeQuery:
        """Query JSON behind a saved trade URL. The URL's league is dropped: pass the
        current league to `search`/`listings_for`. `cache_only` raises
        `NotCached` instead of requesting an uncached opaque saved search."""
        saved = parse_saved_url(url)
        decoded = decode_search_id(saved.search_id)
        if decoded is not None:
            return query_from_body(decoded, saved.kind)
        key = f"trade://saved/{saved.kind}/{saved.url_league}/{saved.search_id}"
        cached = await self.cache.get(key)
        if cached is not None:
            return query_from_body(cached.json(), saved.kind)
        if cache_only:
            raise NotCached("saved search is not cached")
        path = SAVED_PATH.format(
            kind=saved.kind, league=_seg(saved.url_league), id=_seg(saved.search_id)
        )
        data = await self._request("search", "GET", path, deadline=deadline)
        resolved = query_from_body(data, saved.kind)
        await self.cache.put(key, json.dumps({"query": resolved.query, "sort": resolved.sort}))
        return resolved

    async def search(
        self,
        league: str,
        query: dict[str, Any],
        sort: dict[str, Any] | None = None,
        *,
        deadline: float | None = None,
    ) -> SearchResult:
        """Search instant-buyout listings only; any status in `query` is replaced."""
        body = {"query": with_instant_buyout(query), "sort": sort or PRICE_SORT}
        path = SEARCH_PATH.format(league=_seg(league))
        data = await self._request("search", "POST", path, body=body, deadline=deadline)
        return SearchResult(
            str(data["id"]), list(data.get("result") or []), int(data.get("total") or 0)
        )

    async def fetch(
        self, ids: list[str], search_id: str, *, deadline: float | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """Normalized instant-buyout listings for `ids`, `FETCH_BATCH` per request, all
        within one deadline, plus the number of listings dropped as not instant buyout."""
        deadline = self.limiter.deadline() if deadline is None else deadline
        listings, not_buyout = [], 0
        for start in range(0, len(ids), FETCH_BATCH):
            path = FETCH_PATH.format(ids=",".join(ids[start : start + FETCH_BATCH]))
            data = await self._request(
                "fetch", "GET", path, params={"query": search_id}, deadline=deadline
            )
            for entry in data.get("result") or []:
                listing = normalize_listing(entry)
                if listing is None:
                    continue
                if is_instant_buyout(entry):
                    listings.append(listing)
                else:
                    not_buyout += 1
        return listings, not_buyout

    async def listings_for(
        self,
        league: str,
        query_or_url: str | dict[str, Any],
        sample: int | None = None,
        *,
        deadline: float | None = None,
        cache_only: bool = False,
    ) -> dict[str, Any]:
        """The cheapest `sample` instant-buyout listings for a saved /trade/search URL or
        query JSON, cached per query. Any saved status is replaced by `securable` and any
        saved sort by price ascending.

        `cache_only` never makes a request: it returns any cached copy (marked `stale` once
        older than `trade_cache_min`) or raises `NotCached`.

        Returns `{listings, total, search_id, dropped_not_instant_buyout, age_seconds,
        from_cache, stale}`. Raises `RateLimited` or `TradeError` only when there is no
        cached copy to fall back on, and `ValueError` for a bulk exchange URL (whisper-only
        listings). All requests share one deadline: the caller's, else `max_wait_s` from now.
        """
        deadline = self.limiter.deadline() if deadline is None else deadline
        sample = sample or self.config.trade_sample
        if isinstance(query_or_url, str) and parse_saved_url(query_or_url).kind == "exchange":
            raise ValueError(EXCHANGE_REFUSED)
        if isinstance(query_or_url, str):
            tq = await self.resolve_saved_url(
                query_or_url, deadline=deadline, cache_only=cache_only
            )
        else:
            tq = query_from_body(query_or_url, "search")
        # The key hashes the query as sent (status forced to securable), so entries cached
        # under the old online-only pricing are never served.
        digest = query_hash(tq.kind, with_instant_buyout(tq.query), sample)
        key = f"trade://listings/{league}/{digest}"
        cached = await self.cache.get(key)
        fresh = cached is not None and cached.age_seconds < self.config.trade_cache_min * 60
        if cached is not None and (fresh or cache_only):
            return _from_cache(cached, stale=not fresh)
        if cache_only:
            raise NotCached("trade listings are not cached")
        try:
            found = await self.search(league, tq.query, PRICE_SORT, deadline=deadline)
            listings, not_buyout = await self.fetch(
                found.result[:sample], found.id, deadline=deadline
            )
        except (RateLimited, TradeError) as exc:
            if cached is None:
                raise
            log.warning("trade query %s: serving stale cache (%s)", digest, exc)
            return _from_cache(cached, stale=True)
        payload = {
            "listings": listings,
            "total": found.total,
            "search_id": found.id,
            "dropped_not_instant_buyout": not_buyout,
        }
        await self.cache.put(key, json.dumps(payload))
        return {**payload, "age_seconds": 0.0, "from_cache": False, "stale": False}


def _create(ctx: AppContext) -> TradeClient:
    setup_state_log(ctx.config.logs_dir / "ratelimit.log")
    return TradeClient(ctx.config, ctx.cache)


def get_trade(ctx: AppContext) -> TradeClient:
    """The shared trade client (one limiter, one queue) for this server."""
    return ctx.service("trade", _create)
