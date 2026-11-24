"""Price the inputs of a recipe (base, materials, outcomes, salvage), each with its source.

A failed lookup never raises: it becomes a `Priced` with `chaos=None` and an `error`, so one
bad input is reported instead of crashing the evaluation. Only `TimeBudgetSpent` propagates.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.envelope import source
from poe_craft_mcp.http.fetch import FetchError
from poe_craft_mcp.recipes.models import Base, FixedMaterial, NinjaLookup, NinjaMaterial, Salvage
from poe_craft_mcp.services.trade_pricing import OutcomePrice, PricingUnavailable, price_outcome

SALVAGE = "salvage"


class TimeBudgetSpent(Exception):
    """A new trade lookup was needed after the caller's wall-time budget ran out."""


@dataclass
class Priced:
    """One priced input. `chaos` is a unit price (None when unknown); `status` is one of
    ok, fixed, override, not_found, error."""

    kind: str
    name: str
    chaos: float | None
    source: Source
    status: str = "ok"
    age_seconds: float | None = None
    volume: float | None = None
    listing_count: int | None = None
    error: str | None = None
    sources: list[Source] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status in ("not_found", "error")

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "chaos": self.chaos,
            "status": self.status,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 1),
            "source": self.source,
        }
        if self.volume is not None:
            out["volume"] = self.volume
        if self.listing_count is not None:
            out["listing_count"] = self.listing_count
        if self.error:
            out["error"] = self.error
        return out


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

    def _override(self, kind: str, name: str) -> Priced | None:
        if name not in self.overrides:
            return None
        self.used.add(name)
        return Priced(kind, name, self.overrides[name], source("override"), "override")

    def fixed(self, kind: str, name: str, chaos: float) -> Priced:
        return self._override(kind, name) or Priced(
            kind, name, chaos, source("recipe knowledge base", fixed=True), "fixed"
        )

    async def material(self, m: NinjaMaterial | FixedMaterial) -> Priced:
        if isinstance(m, FixedMaterial):
            return self.fixed("material", m.label, m.chaos)
        key = m.name or m.id or ""
        return self._override("material", key) or await self._ninja(
            "material", key, lambda: self.ninja.price(m.category, key)
        )

    async def base(self, base: Base) -> Priced:
        if base.trade_url is not None:
            return await self.trade("base", "base", base.trade_url)
        lookup: NinjaLookup = base.lookup  # type: ignore[assignment]
        return self._override("base", lookup.name) or await self._ninja(
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
        return self._override("salvage", SALVAGE) or await self._ninja(
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
        return Priced(
            kind,
            name,
            row["chaos"],
            item_source,
            "ok" if row["chaos"] is not None else "not_found",
            age_seconds=item_source["age_seconds"],
            volume=row.get("volume_or_listings"),
            error=None if row["chaos"] is not None else "poe.ninja row has no price",
            sources=[dataset],
        )

    async def trade(self, kind: str, name: str, url: str) -> Priced:
        """Price via trade listings (p25 of the cheapest sample)."""
        hit = self._override(kind, name)
        if hit:
            return hit
        if "<" in url:
            error = "placeholder trade URL; fill in a real saved search"
            return Priced(kind, name, None, source("trade", url=url), "error", error=error)
        if url not in self._trade:
            if self.deadline is not None and self.clock() >= self.deadline:
                raise TimeBudgetSpent(name)
            try:
                self._trade[url] = await price_outcome(self.ctx, trade_url=url)
            except PricingUnavailable as exc:
                self._trade[url] = f"price unavailable: {exc.reason}"
            except ValueError as exc:
                self._trade[url] = f"bad trade URL: {exc}"
        found = self._trade[url]
        if isinstance(found, str):
            return Priced(kind, name, None, source("trade", url=url), "error", error=found)
        trade_source = found.sources[-1]
        return Priced(
            kind,
            name,
            found.p25,
            trade_source,
            "ok" if found.p25 is not None else "not_found",
            age_seconds=found.age_seconds,
            volume=found.listing_count,
            listing_count=found.listing_count,
            error=None if found.p25 is not None else "no usable listings",
            sources=list(found.sources),
        )
