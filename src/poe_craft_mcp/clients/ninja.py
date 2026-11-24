"""poe.ninja economy client on top of the shared cached HTTP client.

Every call is served from `ctx.cache` when the data is younger than
`config.ninja_min_interval_min`; otherwise the request is revalidated with `If-None-Match`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from poe_craft_mcp.clients.ninja_history import SnapshotLedger, body_version, worth_bulk_recording
from poe_craft_mcp.clients.ninja_models import (
    ALL_TYPES,
    Dataset,
    Kind,
    League,
    NinjaError,
    Row,
    divine_chaos,
    match_base,
    match_rows,
    normalize_exchange,
    normalize_stash,
    resolve_type,
)
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.http.fetch import FetchError, FetchResult, cache_key

log = logging.getLogger(__name__)


def get_ninja(ctx: AppContext) -> NinjaClient:
    """The shared `NinjaClient` for this server."""
    return ctx.service("ninja", NinjaClient)


class NinjaClient:
    """League, price and currency-rate lookups against poe.ninja's PoE1 economy API."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.ledger = SnapshotLedger(ctx.cache)
        self._interval_s = ctx.config.ninja_min_interval_min * 60.0
        self._failures: dict[str, tuple[float, FetchError]] = {}
        self._warned: set[str] = set()
        self._rows: dict[str, tuple[str, list[Row]]] = {}
        self._fetched_at: dict[str, float] = {}

    # -- fetching ------------------------------------------------------------------------

    async def _get(
        self, url: str, params: dict[str, str] | None = None, *, force: bool = False
    ) -> FetchResult:
        """`get_json` plus: a client-error response is remembered for one interval (no
        re-requests) and, when a cached copy exists, served from it as stale data.

        `force` revalidates now instead of honouring the minimum interval (ETags still apply).
        A failed forced fetch is remembered for the full interval like any other failure.
        """
        key = cache_key(url, params)
        known = self._failures.get(key)
        if known is None or self.ctx.cache.clock() >= known[0]:
            interval = 0.0 if force else self._interval_s
            try:
                result = await self.ctx.http.get_json(url, params, min_interval_s=interval)
            except FetchError as exc:
                transient = exc.status is None or exc.status >= 500 or exc.status == 429
                if transient and not force:
                    raise  # CachedHttpClient already backs these off for the full interval
                known = self._remember_failure(key, exc)
                if transient:
                    raise
            else:
                if not result.stale:
                    self._failures.pop(key, None)
                elif force:  # CachedHttpClient's backoff was sized for interval 0
                    self._remember_failure(key, FetchError(key, "forced refresh failed"))
                return result
        cached = await self.ctx.cache.get(key)
        if cached is not None and known[1].status != 404:
            log.warning("GET %s failed (%s); serving cached copy", key, known[1].reason)
            return FetchResult(cached.json(), cached.age_seconds, True, True, key)
        raise known[1]

    def _remember_failure(self, key: str, exc: FetchError) -> tuple[float, FetchError]:
        backoff = max(self._interval_s, self.ctx.http.failure_backoff_s)
        known = (self.ctx.cache.clock() + backoff, exc)
        self._failures[key] = known
        return known

    def _failing(self, key: str) -> bool:
        known = self._failures.get(key)
        return known is not None and self.ctx.cache.clock() < known[0]

    async def current_league(self, *, force: bool = False) -> League:
        """`config.league` if explicit (name looked up best-effort), else the first league."""
        config = self.ctx.config
        explicit = config.league != "auto"
        try:
            result = await self._get(config.ninja_url(config.ninja_leagues_path), force=force)
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
        name = entry.get("name") or entry["id"]
        return League(entry["id"], name, result.age_seconds, result.stale, result.source_url)

    # -- datasets ------------------------------------------------------------------------

    def _endpoint(self, kind: Kind) -> str:
        config = self.ctx.config
        return config.ninja_url(
            config.ninja_exchange_path if kind == "exchange" else config.ninja_stash_item_path
        )

    async def dataset(
        self, category: str, league: League | None = None, *, force: bool = False
    ) -> Dataset:
        """Normalized rows of one poe.ninja type; `[]` (logged once) if it is empty or gone.

        Pass an already resolved `league` to avoid looking it up again. Failures other than
        a 404 raise `FetchError` (or return the cached copy marked `stale`). `force`
        revalidates now, ignoring the minimum interval once.
        """
        kind, type_name = resolve_type(category)
        league = league or await self.current_league()
        url, params = self._endpoint(kind), {"league": league.id, "type": type_name}
        key, clock = cache_key(url, params), self.ctx.cache.clock
        base = Dataset(type_name, kind, league.id, [], url=key, league_source=league.source())

        peek = await self.ctx.cache.get(key)
        memo = self._rows.get(key)
        peek_version = body_version(peek) if peek is not None else None
        same_body = memo is not None and memo[0] == peek_version
        if (
            not force
            and peek is not None
            and memo is not None
            and same_body
            and peek.age_seconds < self._interval_s
            and not self._failing(key)
        ):
            return self._ready(base, memo[1], peek.age_seconds, False, peek_version)
        try:
            result = await self._get(url, params, force=force)
        except FetchError as exc:
            if exc.status != 404:
                raise
            self._warn_once(key, "poe.ninja type %s is unavailable (HTTP 404)", type_name)
            return base
        if memo is not None and same_body and result.from_cache:
            rows = memo[1]
        else:
            payload = result.data if isinstance(result.data, dict) else {}
            normalize = normalize_exchange if kind == "exchange" else normalize_stash
            rows = normalize(payload, type_name)
        version = peek_version if result.from_cache else None
        if not result.from_cache:  # a fresh 200: identify the body just stored
            stored = await self.ctx.cache.get(key)
            version = body_version(stored) if stored else None
        if version is not None:
            self._rows[key] = (version, rows)
        if not rows:
            self._warn_once(key, "poe.ninja type %s returned no lines", type_name)
        ds = self._ready(base, rows, result.age_seconds, result.stale, version, at=clock())
        if not result.from_cache:
            await self.ledger.record(ds, [r for r in rows if worth_bulk_recording(ds, r)])
        return ds

    def _ready(
        self,
        base: Dataset,
        rows: list[Row],
        age: float,
        stale: bool,
        version: str | None,
        at: float | None = None,
    ) -> Dataset:
        now = at if at is not None else self.ctx.cache.clock()
        self._fetched_at[base.url or ""] = now - age
        return Dataset(
            base.type,
            base.kind,
            base.league,
            rows,
            age,
            stale,
            base.url,
            version,
            base.league_source,
        )

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

    async def record(self, ds: Dataset, rows: list[Row]) -> None:
        """Snapshot rows handed to a caller so every quoted price can be replayed from history."""
        await self.ledger.record(ds, rows)

    async def cached_ages(self, league: str) -> dict[str, float]:
        """Age in seconds of every poe.ninja dataset in the cache, by type.

        Datasets touched in this process come from memory; the rest are read from the cache's
        timestamps (no response body is loaded).
        """
        keys = {
            type_name: cache_key(
                self._endpoint(resolve_type(type_name)[0]), {"league": league, "type": type_name}
            )
            for type_name in ALL_TYPES
        }
        stored = await self.ctx.cache.fetched_at(
            k for k in keys.values() if k not in self._fetched_at
        )
        now, ages = self.ctx.cache.clock(), {}
        for type_name, key in keys.items():
            at = self._fetched_at.get(key, stored.get(key))
            if at is not None:
                ages[type_name] = round(max(0.0, now - at), 1)
        return ages

    async def force_refresh(
        self, categories: Iterable[str], league: League
    ) -> tuple[list[Dataset], dict[str, str]]:
        """Revalidate each category now, one after another, ignoring the minimum interval
        once; unchanged data costs a 304. Returns the datasets and `{type: error}` failures."""
        datasets: list[Dataset] = []
        errors: dict[str, str] = {}
        for category in dict.fromkeys(categories):
            try:
                datasets.append(await self.dataset(category, league, force=True))
            except (FetchError, NinjaError) as exc:
                errors[category] = str(exc)
        return datasets, errors

    # -- derived values ------------------------------------------------------------------

    async def chaos_per_divine(self, league: League | None = None) -> float:
        """Chaos value of one Divine Orb (from the `Currency` exchange)."""
        return divine_chaos((await self.dataset("Currency", league)).rows)

    async def currency_rates(self, league: League | None = None) -> dict[str, float]:
        """Chaos value of one unit of each currency, keyed by **trade-site currency id**.

        poe.ninja's exchange line ids for the `Currency` type already are the trade site's
        listing currency ids (verified: chaos, divine, exalted, alch, alt, aug, annul, chrome,
        fusing, jewellers, gcp, scour, regal, vaal, chance, blessed, transmute, mirror, ...),
        so no translation table is needed; ids the trade site does not know are harmless.
        `chaos` is always 1.0.
        """
        return rates_from((await self.dataset("Currency", league)).rows)

    async def price(self, category: str, name_or_id: str) -> Row | None:
        """Quote for one item: the row plus `source` {type, id, age_seconds, stale, url}.

        Matches `id` first, then `name`; among several name matches (variants) the one
        with the most listings wins. `None` when nothing matches. The quote is snapshotted.
        """
        ds = await self.dataset(category)
        matches = match_rows(ds.rows, name_or_id)
        if not matches:
            return None
        best = max(matches, key=lambda r: r["volume_or_listings"] or 0)
        await self.record(ds, [best])
        return quote(best, ds)

    async def find_base(
        self, base: str, ilvl_min: int | None = None, influence: str | None = None
    ) -> Row | None:
        """Cheapest matching BaseType row (with `source`), or `None`. See `match_base`."""
        ds = await self.dataset("BaseType")
        row = match_base(ds.rows, base, ilvl_min, influence)
        if row is None:
            return None
        await self.record(ds, [row])
        return quote(row, ds)


def rates_from(currency_rows: list[Row]) -> dict[str, float]:
    """Chaos per unit keyed by trade currency id, from `Currency` rows (see `currency_rates`)."""
    rates = {r["id"]: float(r["chaos"]) for r in currency_rows if r["chaos"]}
    rates["chaos"] = 1.0
    return rates


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
