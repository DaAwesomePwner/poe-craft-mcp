"""Price the inputs of a recipe (base, materials, outcomes, salvage), each with its source.

A failed lookup never raises: it becomes a `Priced` with `chaos=None` and an `error`, so one
bad input is reported instead of crashing the evaluation. Only `TimeBudgetSpent` propagates.
Volumes are in units: poe.ninja exchange volume (a chaos amount) is divided by the unit price.
A trade price's age and staleness include the currency rates its listings were converted at.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError
from poe_craft_mcp.clients.trade import get_trade
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.envelope import source
from poe_craft_mcp.http.fetch import FetchError
from poe_craft_mcp.recipes.models import Base, FixedMaterial, NinjaLookup, NinjaMaterial, Salvage
from poe_craft_mcp.services.priced import Priced
from poe_craft_mcp.services.trade_pricing import (
    OutcomePrice,
    PricingUnavailable,
    QuoteNotCached,
    price_outcome,
)

SALVAGE = "salvage"
CURRENCY = "poe.ninja Currency"


class TimeBudgetSpent(Exception):
    """A new trade lookup was needed after the caller's wall-time budget ran out."""


def check_overrides(overrides: Mapping[str, float] | None) -> dict[str, float]:
    """Validate `price_overrides` (chaos per unit, finite and >= 0); raises ValueError."""
    clean: dict[str, float] = {}
    for name, value in (overrides or {}).items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"price override for {name!r} must be a number")
        if not (math.isfinite(value) and value >= 0):
            raise ValueError(f"price override for {name!r} must be finite and >= 0, got {value}")
        clean[name] = float(value)
    return clean


def _ninja_source(row: dict[str, Any]) -> tuple[Source, Source]:
    """(per-input source, envelope source) for a poe.ninja quote."""
    quoted = row["source"]
    dataset = source(
        f"poe.ninja {quoted['type']}",
        url=quoted.get("url"),
        age_seconds=quoted.get("age_seconds"),
        stale=bool(quoted.get("stale")),
        type=quoted["type"],
    )
    return {**dataset, "id": quoted["id"]}, dataset


class Pricer:
    """Prices inputs for one or more recipes; trade results are memoized per URL.

    `deadline` (on `clock`) stops new trade lookups: past it, `TimeBudgetSpent` is raised
    unless the URL was already priced by this pricer.
    """

    def __init__(
        self,
        ctx: AppContext,
        overrides: Mapping[str, float] | None = None,
        *,
        deadline: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ctx = ctx
        self.ninja = get_ninja(ctx)
        self.overrides = dict(overrides or {})
        self.used: set[str] = set()
        self.deadline = deadline
        self.clock = clock or ctx.cache.clock
        self._trade: dict[str, OutcomePrice | str] = {}

    def unused_overrides(self) -> list[str]:
        return sorted(set(self.overrides) - self.used)

    def override(self, kind: str, name: str) -> Priced | None:
        if name not in self.overrides:
            return None
        self.used.add(name)
        return Priced(kind, name, self.overrides[name], source("override"), "override")

    def fixed(self, kind: str, name: str, chaos: float) -> Priced:
        return self.override(kind, name) or Priced(
            kind, name, chaos, source("recipe knowledge base", fixed=True), "fixed"
        )

    async def material(self, m: NinjaMaterial | FixedMaterial) -> Priced:
        if isinstance(m, FixedMaterial):
            return self.fixed("material", m.label, m.chaos)
        key = m.name or m.id or ""
        return self.override("material", key) or await self._ninja(
            "material", key, lambda: self.ninja.price(m.category, key)
        )

    async def base(self, base: Base) -> Priced:
        if base.trade_url is not None:
            return await self.trade("base", "base", base.trade_url)
        lookup: NinjaLookup = base.lookup  # type: ignore[assignment]
        return self.override("base", lookup.name) or await self._ninja(
            "base",
            lookup.name,
            lambda: self.ninja.find_base(lookup.name, lookup.ilvl_min, lookup.influence),
        )

    async def salvage(self, salvage: Salvage) -> Priced:
        if salvage.fixed_chaos is not None:
            return self.fixed("salvage", SALVAGE, salvage.fixed_chaos)
        if salvage.trade_url is not None:
            return await self.trade("salvage", SALVAGE, salvage.trade_url)
        lookup: NinjaLookup = salvage.lookup  # type: ignore[assignment]
        return self.override("salvage", SALVAGE) or await self._ninja(
            "salvage",
            SALVAGE,
            lambda: self.ninja.find_base(lookup.name, lookup.ilvl_min, lookup.influence),
        )

    async def _ninja(self, kind: str, name: str, lookup: Callable[[], Any]) -> Priced:
        try:
            row = await lookup()
        except (FetchError, NinjaError) as exc:
            return Priced(kind, name, None, source("poe.ninja"), "error", error=str(exc))
        if row is None:
            error = "no matching poe.ninja row"
            return Priced(kind, name, None, source("poe.ninja"), "not_found", error=error)
        item_source, dataset = _ninja_source(row)
        chaos, volume = row["chaos"], row.get("volume_or_listings")
        if volume is not None and row.get("volume_kind") == "chaos_volume":
            volume = volume / chaos if chaos else None
        return Priced(
            kind,
            name,
            chaos,
            item_source,
            "ok" if chaos is not None else "not_found",
            age_seconds=item_source["age_seconds"],
            volume=volume,
            error=None if chaos is not None else "poe.ninja row has no price",
            sources=[dataset],
            stale=dataset["stale"],
            ilvl=row.get("ilvl"),
        )

    def _spent(self) -> bool:
        return self.deadline is not None and self.clock() >= self.deadline

    def _budget_refused(self, exc: PricingUnavailable) -> bool:
        """True when the limiter refused a wait it would have made without our deadline:
        the wait fits its own `max_wait_s` but ends past the time budget."""
        wait = exc.retry_after
        if self.deadline is None or wait is None:
            return False
        limit = get_trade(self.ctx).limiter.max_wait_s
        return wait <= limit and self.clock() + wait > self.deadline

    async def _lookup(self, name: str, url: str) -> OutcomePrice | str:
        """Trade price for `url`; past the deadline only cached listings are served."""
        try:
            return await price_outcome(
                self.ctx, trade_url=url, deadline=self.deadline, cache_only=self._spent()
            )
        except QuoteNotCached:
            raise TimeBudgetSpent(name) from None
        except PricingUnavailable as exc:
            if self._spent() or self._budget_refused(exc):
                raise TimeBudgetSpent(name) from None
            return f"price unavailable: {exc.reason}"
        except ValueError as exc:
            return f"bad trade URL: {exc}"

    async def trade(self, kind: str, name: str, url: str) -> Priced:
        """Price via trade listings (p25 of the cheapest sample)."""
        hit = self.override(kind, name)
        if hit:
            return hit
        if "<" in url:
            error = "placeholder trade URL; fill in a real saved search"
            return Priced(kind, name, None, source("trade", url=url), "error", error=error)
        if url not in self._trade:
            self._trade[url] = await self._lookup(name, url)
        found = self._trade[url]
        if isinstance(found, str):
            return Priced(kind, name, None, source("trade", url=url), "error", error=found)
        trade_source = found.sources[-1]
        age, stale = found.age_seconds, bool(trade_source.get("stale"))
        if set(found.currency_mix) - {"chaos"}:  # some listings converted at Currency rates
            rates = [s for s in found.sources[:-1] if s["name"] == CURRENCY]
            age = max([age, *(s["age_seconds"] for s in rates if s["age_seconds"] is not None)])
            stale = stale or any(s.get("stale") for s in rates)
        return Priced(
            kind,
            name,
            found.p25,
            trade_source,
            "ok" if found.p25 is not None else "not_found",
            age_seconds=age,
            volume=found.listing_count,
            listing_count=found.listing_count,
            error=None if found.p25 is not None else "no usable listings",
            sources=list(found.sources),
            stale=stale,
        )
