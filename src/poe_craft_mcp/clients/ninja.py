"""poe.ninja economy client on top of the shared cached HTTP client.

Every call is served from `ctx.cache` when the data is younger than
`config.ninja_min_interval_min`; otherwise the request is revalidated with `If-None-Match`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from poe_craft_mcp.clients.ninja_models import (
    ALL_TYPES,
    SNAPSHOT_MIN_LISTINGS,
    Dataset,
    Kind,
    League,
    NinjaError,
    Row,
    match_base,
    match_rows,
    normalize_exchange,
    normalize_stash,
    resolve_type,
)
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.http.cache import PriceSnapshot
from poe_craft_mcp.http.fetch import FetchError, FetchResult, cache_key

log = logging.getLogger(__name__)


def get_ninja(ctx: AppContext) -> NinjaClient:
    """The shared `NinjaClient` for this server."""
    return ctx.service("ninja", NinjaClient)


class NinjaClient:
    """League, price and currency-rate lookups against poe.ninja's PoE1 economy API."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._interval_s = ctx.config.ninja_min_interval_min * 60.0
        self._unavailable: dict[str, float] = {}
        self._warned: set[str] = set()

    # -- leagues -------------------------------------------------------------------------

    async def current_league(self) -> League:
        """`config.league` if explicit (name looked up best-effort), else the first league."""
        config = self.ctx.config
        url = config.ninja_url(config.ninja_leagues_path)
        explicit = config.league != "auto"
        try:
            result = await self.ctx.http.get_json(url, min_interval_s=self._interval_s)
        except FetchError:
            if not explicit:
                raise
            return League(config.league, config.league)
        entries = [e for e in result.data if isinstance(e, dict) and e.get("id")]
        if explicit:
            hit = next((e for e in entries if e["id"] == config.league), {})
            entry = {"id": config.league, "name": hit.get("name", config.league)}
        elif entries:
            entry = entries[0]
        else:
            raise NinjaError("poe.ninja returned no leagues")
        return League(
            entry["id"],
            entry.get("name") or entry["id"],
            result.age_seconds,
            result.stale,
            result.source_url,
        )

    # -- datasets ------------------------------------------------------------------------

    def _endpoint(self, kind: Kind) -> str:
        config = self.ctx.config
        return config.ninja_url(
            config.ninja_exchange_path if kind == "exchange" else config.ninja_stash_item_path
        )

    async def dataset(self, category: str) -> Dataset:
        """Normalized rows of one poe.ninja type; `[]` (logged once) if it is empty or gone."""
        kind, type_name = resolve_type(category)
        league = (await self.current_league()).id
        url, params = self._endpoint(kind), {"league": league, "type": type_name}
        key = cache_key(url, params)
        now = self.ctx.cache.clock()
        if now - self._unavailable.get(key, float("-inf")) < self._interval_s:
            return Dataset(type_name, kind, league, [])
        try:
            result = await self.ctx.http.get_json(url, params, min_interval_s=self._interval_s)
        except FetchError as exc:
            if exc.status is None or exc.status >= 500 or exc.status == 429:
                raise
            self._unavailable[key] = now
            self._warn_once(
                key, "poe.ninja type %s is unavailable (HTTP %s)", type_name, exc.status
            )
            return Dataset(type_name, kind, league, [], url=key)
        payload = result.data if isinstance(result.data, dict) else {}
        normalize = normalize_exchange if kind == "exchange" else normalize_stash
        rows = normalize(payload, type_name)
        if not rows:
            self._warn_once(key, "poe.ninja type %s returned no lines", type_name)
        elif not result.from_cache:
            await self._snapshot(league, type_name, kind, rows)
        return _dataset(type_name, kind, league, rows, result)

    async def exchange_overview(self, type_name: str) -> list[Row]:
        """Rows of an exchange type, e.g. `Currency`, `Essence`, `Fossil`."""
        kind, name = resolve_type(type_name)
        if kind != "exchange":
            raise NinjaError(f"{name} is a stash type; use stash_item_overview")
        return (await self.dataset(name)).rows

    async def stash_item_overview(self, type_name: str) -> list[Row]:
        """Rows of a stash item type, e.g. `BaseType`, `ClusterJewel`."""
        kind, name = resolve_type(type_name)
        if kind != "stash":
            raise NinjaError(f"{name} is an exchange type; use exchange_overview")
        return (await self.dataset(name)).rows

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    async def _snapshot(self, league: str, type_name: str, kind: Kind, rows: Iterable[Row]) -> None:
        snapshots = [
            PriceSnapshot(
                league, f"ninja:{type_name}", str(r["id"]), r["chaos"], r["volume_or_listings"]
            )
            for r in rows
            if r["chaos"] is not None
            and (kind == "exchange" or (r["volume_or_listings"] or 0) >= SNAPSHOT_MIN_LISTINGS)
        ]
        await self.ctx.cache.add_snapshots(snapshots)

    async def cached_ages(self, league: str) -> dict[str, float]:
        """Age in seconds of every poe.ninja dataset currently in the cache, by type."""
        ages: dict[str, float] = {}
        for type_name in ALL_TYPES:
            kind, _ = resolve_type(type_name)
            key = cache_key(self._endpoint(kind), {"league": league, "type": type_name})
            cached = await self.ctx.cache.get(key)
            if cached is not None:
                ages[type_name] = round(cached.age_seconds, 1)
        return ages

    # -- derived values ------------------------------------------------------------------

    async def chaos_per_divine(self) -> float:
        """Chaos value of one Divine Orb (from the `Currency` exchange)."""
        for row in await self.exchange_overview("Currency"):
            if row["id"] == "divine" and row["chaos"]:
                return float(row["chaos"])
        raise NinjaError("poe.ninja Currency data has no Divine Orb price")

    async def currency_rates(self) -> dict[str, float]:
        """Chaos value of one unit of each currency, keyed by **trade-site currency id**.

        poe.ninja's exchange line ids for the `Currency` type already are the trade site's
        listing currency ids (verified: chaos, divine, exalted, alch, alt, aug, annul, chrome,
        fusing, jewellers, gcp, scour, regal, vaal, chance, blessed, transmute, mirror, ...),
        so no translation table is needed; ids the trade site does not know are harmless.
        `chaos` is always 1.0.
        """
        rows = await self.exchange_overview("Currency")
        rates = {r["id"]: float(r["chaos"]) for r in rows if r["chaos"]}
        rates["chaos"] = 1.0
        return rates

    async def price(self, category: str, name_or_id: str) -> Row | None:
        """Quote for one item: the row plus `source` {type, id, age_seconds, stale, url}.

        Matches `id` first, then `name`; among several name matches (variants) the one
        with the most listings wins. `None` when nothing matches.
        """
        ds = await self.dataset(category)
        matches = match_rows(ds.rows, name_or_id)
        if not matches:
            return None
        best = max(matches, key=lambda r: r["volume_or_listings"] or 0)
        return quote(best, ds)

    async def find_base(
        self, base: str, ilvl_min: int | None = None, influence: str | None = None
    ) -> Row | None:
        """Cheapest matching BaseType row (with `source`), or `None`. See `match_base`."""
        ds = await self.dataset("BaseType")
        row = match_base(ds.rows, base, ilvl_min, influence)
        return None if row is None else quote(row, ds)


def quote(row: Row, ds: Dataset) -> Row:
    """`row` plus a `source` dict identifying the poe.ninja type, id and data age."""
    src = {
        "type": ds.type,
        "id": row["id"],
        "age_seconds": None if ds.age_seconds is None else round(ds.age_seconds, 1),
        "stale": ds.stale,
        "url": ds.url,
    }
    return {**row, "source": src}


def _dataset(type_name: str, kind: Kind, league: str, rows: list[Row], res: FetchResult) -> Dataset:
    return Dataset(type_name, kind, league, rows, res.age_seconds, res.stale, res.source_url)
