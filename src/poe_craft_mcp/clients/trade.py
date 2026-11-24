"""pathofexile.com trade client: saved URL to query, search, fetch, bulk exchange.

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
    SearchResult,
    TradeError,
    TradeQuery,
    contains_text,
    decode_search_id,
    normalize_listing,
    normalize_offer,
    parse_saved_url,
    query_from_body,
    query_hash,
    with_status,
)
from poe_craft_mcp.config import Config
from poe_craft_mcp.http.cache import CachedResponse, ResponseCache
from poe_craft_mcp.http.ratelimit import RateLimited, RateLimiter, setup_state_log
from poe_craft_mcp.log import register_secret
from poe_craft_mcp.secrets import get_poesessid

if TYPE_CHECKING:
    from poe_craft_mcp.context import AppContext

log = logging.getLogger(__name__)

__all__ = ["RateLimited", "SearchResult", "TradeClient", "TradeError", "TradeQuery", "get_trade"]

SEARCH_PATH = "/api/trade/search/{league}"
SAVED_PATH = "/api/trade/{kind}/{league}/{id}"
FETCH_PATH = "/api/trade/fetch/{ids}"
EXCHANGE_PATH = "/api/trade/exchange/{league}"
FETCH_BATCH = 10
PRICE_SORT = {"price": "asc"}
EXCHANGE_SORT = {"have": "asc"}
COOKIE_ORIGINS = frozenset({("https", "www.pathofexile.com", 443)})


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
        port = url.port or {"https": 443, "http": 80}.get(url.scheme)
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

    async def resolve_saved_url(self, url: str, *, deadline: float | None = None) -> TradeQuery:
        """Query JSON behind a saved trade URL. The URL's league is dropped: pass the
        current league to `search`/`exchange`/`listings_for`."""
        saved = parse_saved_url(url)
        decoded = decode_search_id(saved.search_id)
        if decoded is not None:
            return query_from_body(decoded, saved.kind)
        key = f"trade://saved/{saved.kind}/{saved.url_league}/{saved.search_id}"
        cached = await self.cache.get(key)
        if cached is not None:
            return query_from_body(cached.json(), saved.kind)
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
        online_only: bool = True,
        instant_buyout: bool = False,
        deadline: float | None = None,
    ) -> SearchResult:
        body = {
            "query": with_status(query, online_only=online_only, instant_buyout=instant_buyout),
            "sort": sort or PRICE_SORT,
        }
        path = SEARCH_PATH.format(league=_seg(league))
        data = await self._request("search", "POST", path, body=body, deadline=deadline)
        return SearchResult(
            str(data["id"]), list(data.get("result") or []), int(data.get("total") or 0)
        )

    async def fetch(
        self, ids: list[str], search_id: str, *, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        """Normalized listings for `ids`, `FETCH_BATCH` per request, all within one deadline."""
        deadline = self.limiter.deadline() if deadline is None else deadline
        listings = []
        for start in range(0, len(ids), FETCH_BATCH):
            path = FETCH_PATH.format(ids=",".join(ids[start : start + FETCH_BATCH]))
            data = await self._request(
                "fetch", "GET", path, params={"query": search_id}, deadline=deadline
            )
            for entry in data.get("result") or []:
                listing = normalize_listing(entry)
                if listing is not None:
                    listings.append(listing)
        return listings

    async def exchange(
        self,
        league: str,
        want: list[str],
        have: list[str],
        *,
        online_only: bool = True,
        extra: dict[str, Any] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Bulk exchange offers, cheapest first, each priced per unit of `want`:
        `{id, total, listings}`. Exchange has no instant-buyout filter."""
        query = {**(extra or {}), "want": want, "have": have}
        body = {
            "query": with_status(query, online_only=online_only, instant_buyout=False),
            "sort": EXCHANGE_SORT,
            "engine": "new",
        }
        path = EXCHANGE_PATH.format(league=_seg(league))
        data = await self._request("exchange", "POST", path, body=body, deadline=deadline)
        results = data.get("result") or {}
        offers = [normalize_offer(k, v) for k, v in results.items()]
        listings = sorted((o for o in offers if o), key=lambda o: o["amount"])
        return {"id": data.get("id"), "total": int(data.get("total") or 0), "listings": listings}

    async def listings_for(
        self,
        league: str,
        query_or_url: str | dict[str, Any],
        sample: int | None = None,
        *,
        online_only: bool = True,
        instant_buyout: bool = False,
    ) -> dict[str, Any]:
        """The cheapest `sample` listings for a saved URL or query JSON, cached per query.
        Any saved sort is replaced by price ascending.

        Returns `{listings, total, search_id, age_seconds, from_cache, stale}`. Raises
        `RateLimited` or `TradeError` only when there is no cached copy to fall back on,
        and `ValueError` for `instant_buyout` on a bulk exchange query. All requests share
        one `max_wait_s` deadline.
        """
        deadline = self.limiter.deadline()
        sample = sample or self.config.trade_sample
        if isinstance(query_or_url, str):
            tq = await self.resolve_saved_url(query_or_url, deadline=deadline)
        else:
            tq = query_from_body(query_or_url, "search")
        if tq.kind == "exchange" and instant_buyout:
            raise ValueError("instant_buyout is not supported for bulk exchange queries")
        digest = query_hash(tq.kind, tq.query, online_only, instant_buyout, sample)
        key = f"trade://listings/{league}/{digest}"
        cached = await self.cache.get(key)
        if cached is not None and cached.age_seconds < self.config.trade_cache_min * 60:
            return _from_cache(cached, stale=False)
        try:
            if tq.kind == "exchange":
                want, have = tq.query.get("want") or [], tq.query.get("have") or []
                ex = await self.exchange(
                    league, want, have, extra=tq.query, online_only=online_only, deadline=deadline
                )
                found = SearchResult(str(ex["id"]), [], ex["total"])
                listings = ex["listings"][:sample]
            else:
                found = await self.search(
                    league,
                    tq.query,
                    PRICE_SORT,
                    online_only=online_only,
                    instant_buyout=instant_buyout,
                    deadline=deadline,
                )
                listings = await self.fetch(found.result[:sample], found.id, deadline=deadline)
        except (RateLimited, TradeError) as exc:
            if cached is None:
                raise
            log.warning("trade query %s: serving stale cache (%s)", digest, exc)
            return _from_cache(cached, stale=True)
        payload = {"listings": listings, "total": found.total, "search_id": found.id}
        await self.cache.put(key, json.dumps(payload))
        return {**payload, "age_seconds": 0.0, "from_cache": False, "stale": False}


def _create(ctx: AppContext) -> TradeClient:
    setup_state_log(ctx.config.logs_dir / "ratelimit.log")
    return TradeClient(ctx.config, ctx.cache)


def get_trade(ctx: AppContext) -> TradeClient:
    """The shared trade client (one limiter, one queue) for this server."""
    return ctx.service("trade", _create)
