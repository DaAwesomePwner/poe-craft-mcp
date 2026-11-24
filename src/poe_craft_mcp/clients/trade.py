"""pathofexile.com trade client: saved URL to query, search, fetch, bulk exchange.

Every request goes through one `RateLimiter`. Results of `listings_for` are cached per
query hash and league for `trade_cache_min`; when the limiter refuses, the cached copy is
returned with `stale=True`. The optional POESESSID is only ever sent as a cookie.
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
from poe_craft_mcp.http.ratelimit import RateLimited, RateLimiter
from poe_craft_mcp.log import add_file_log, register_secret
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
DEFAULT_SORT = {"price": "asc"}
EXCHANGE_SORT = {"have": "asc"}


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

    async def _cookie(self) -> str | None:
        if not self._session_loaded:
            self._session = await asyncio.to_thread(self._poesessid)
            register_secret(self._session)
            self._session_loaded = True
        return self._session

    async def _request(
        self, kind: str, method: str, path: str, *, body: Any = None, params: Any = None
    ) -> Any:
        headers = {"User-Agent": self.config.user_agent, "Accept": "application/json"}
        session = await self._cookie()
        if session:
            headers["Cookie"] = f"POESESSID={session}"
        url = self.config.trade_base_url + path

        async def send() -> httpx.Response:
            return await self._client.request(
                method, url, json=body, params=params, headers=headers
            )

        try:
            response = await self.limiter.run(kind, send)
        except httpx.HTTPError as exc:
            raise TradeError(f"{method} {path} failed: {type(exc).__name__}") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            error = data.get("error") if isinstance(data, dict) else None
            detail = error.get("message") if isinstance(error, dict) else None
            raise TradeError(
                f"{method} {path}: HTTP {response.status_code} {detail or ''}".rstrip(),
                response.status_code,
            )
        if not isinstance(data, dict):
            raise TradeError(f"{method} {path}: unexpected response body", response.status_code)
        return data

    async def resolve_saved_url(self, url: str) -> TradeQuery:
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
        data = await self._request("search", "GET", path)
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
    ) -> SearchResult:
        body = {
            "query": with_status(query, online_only=online_only, instant_buyout=instant_buyout),
            "sort": sort or DEFAULT_SORT,
        }
        data = await self._request(
            "search", "POST", SEARCH_PATH.format(league=_seg(league)), body=body
        )
        return SearchResult(
            str(data["id"]), list(data.get("result") or []), int(data.get("total") or 0)
        )

    async def fetch(self, ids: list[str], search_id: str) -> list[dict[str, Any]]:
        """Normalized listings for `ids`, requested `FETCH_BATCH` at a time."""
        listings = []
        for start in range(0, len(ids), FETCH_BATCH):
            path = FETCH_PATH.format(ids=",".join(ids[start : start + FETCH_BATCH]))
            data = await self._request("fetch", "GET", path, params={"query": search_id})
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
    ) -> dict[str, Any]:
        """Bulk exchange offers, each priced per unit of `want`: `{id, total, listings}`."""
        query = {**(extra or {}), "want": want, "have": have}
        return await self._exchange(league, TradeQuery("exchange", query), online_only)

    async def _exchange(self, league: str, tq: TradeQuery, online_only: bool) -> dict[str, Any]:
        body = {
            "query": with_status(tq.query, online_only=online_only, instant_buyout=False),
            "sort": tq.sort or EXCHANGE_SORT,
            "engine": "new",
        }
        path = EXCHANGE_PATH.format(league=_seg(league))
        data = await self._request("exchange", "POST", path, body=body)
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

        Returns `{listings, total, search_id, age_seconds, from_cache, stale}`. Raises
        `RateLimited` or `TradeError` only when there is no cached copy to fall back on.
        """
        sample = sample or self.config.trade_sample
        if isinstance(query_or_url, str):
            tq = await self.resolve_saved_url(query_or_url)
        else:
            tq = query_from_body(query_or_url, "search")
        digest = query_hash(tq.kind, tq.query, tq.sort, online_only, instant_buyout, sample)
        key = f"trade://listings/{league}/{digest}"
        cached = await self.cache.get(key)
        if cached is not None and cached.age_seconds < self.config.trade_cache_min * 60:
            return _from_cache(cached, stale=False)
        try:
            if tq.kind == "exchange":
                ex = await self._exchange(league, tq, online_only)
                payload = {
                    "listings": ex["listings"][:sample],
                    "total": ex["total"],
                    "search_id": ex["id"],
                }
            else:
                found = await self.search(
                    league,
                    tq.query,
                    tq.sort,
                    online_only=online_only,
                    instant_buyout=instant_buyout,
                )
                listings = await self.fetch(found.result[:sample], found.id)
                payload = {"listings": listings, "total": found.total, "search_id": found.id}
        except (RateLimited, TradeError) as exc:
            if cached is None:
                raise
            log.warning("trade query %s: serving stale cache (%s)", digest, exc)
            return _from_cache(cached, stale=True)
        await self.cache.put(key, json.dumps(payload))
        return {**payload, "age_seconds": 0.0, "from_cache": False, "stale": False}


def _create(ctx: AppContext) -> TradeClient:
    add_file_log("poe_craft_mcp.ratelimit", ctx.config.logs_dir / "ratelimit.log")
    return TradeClient(ctx.config, ctx.cache)


def get_trade(ctx: AppContext) -> TradeClient:
    """The shared trade client (one limiter, one queue) for this server."""
    return ctx.service("trade", _create)
