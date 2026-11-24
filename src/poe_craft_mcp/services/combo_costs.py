"""Cost, miss value and profit of one cluster combo attempt.

Cost per attempt = white base + Transmutation + E[Alterations] x Alteration +
E[Augmentations] x Augmentation + Regal. A lookup that fails leaves its part (and anything
that depends on it) null with a warning; no price is ever made up.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.gamedata.combos import ComboOdds
from poe_craft_mcp.services.trade_pricing import PricingUnavailable, price_outcome

CURRENCY = (
    ("transmutation", "transmute", "Orb of Transmutation"),
    ("alteration", "alt", "Orb of Alteration"),
    ("augmentation", "aug", "Orb of Augmentation"),
    ("regal", "regal", "Regal Orb"),
)


def money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def chance(value: float | None) -> float | None:
    return None if value is None else float(f"{value:.6g}")


@dataclass
class TradeValue:
    """A trade search priced at p25 (`chaos` after `undercut`), or why it could not be."""

    chaos: float | None
    p25: float | None = None
    listing_count: int | None = None
    error: str | None = None
    sources: list[Source] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """The search worked but had no usable listings."""
        return self.error is None and self.p25 is None


async def trade_value(
    ctx: AppContext, league: str, query: dict[str, Any], undercut: float = 0.0
) -> TradeValue:
    try:
        price = await price_outcome(ctx, query_json=query, league=league)
    except PricingUnavailable as exc:
        wait = f"; retry in about {exc.retry_after:.0f}s" if exc.retry_after is not None else ""
        return TradeValue(None, error=f"{exc.reason}{wait}")
    src = [price.sources[-1]]
    chaos = None if price.p25 is None else price.p25 * (1 - undercut)
    return TradeValue(chaos, price.p25, price.listing_count, sources=src)


def currency_costs(rates: Mapping[str, float]) -> tuple[dict[str, float | None], list[str]]:
    """Chaos per orb from poe.ninja Currency rates, by role."""
    costs: dict[str, float | None] = {}
    warnings = []
    for role, trade_id, name in CURRENCY:
        rate = rates.get(trade_id)
        costs[role] = float(rate) if rate else None
        if not rate:
            warnings.append(f"no poe.ninja price for {name}; cost per attempt is unknown")
    return costs, warnings


def attempt_cost(
    odds: ComboOdds, base: float | None, orbs: Mapping[str, float | None]
) -> float | None:
    transmute, alt, aug, regal = (orbs[role] for role, _, _ in CURRENCY)
    if base is None or transmute is None or alt is None or aug is None or regal is None:
        return None
    return (
        base
        + transmute
        + odds.expected_alterations * alt
        + odds.expected_augmentations * aug
        + odds.regals * regal
    )


def economics(
    odds: ComboOdds,
    *,
    miss_value: float | None,
    base: float | None,
    orbs: Mapping[str, float | None],
    minutes_per_attempt: float,
    chaos_per_divine: float,
) -> dict[str, Any]:
    """Per-attempt odds, value, cost and profit; null where an input is unknown."""
    ev = odds.expected_value if miss_value is not None else None
    cost = attempt_cost(odds, base, orbs)
    profit = None if ev is None or cost is None else ev - cost
    per_hour = None if profit is None else 60 / minutes_per_attempt * profit
    return {
        "p_any_sellable": chance(odds.p_hit),
        "expected_attempts_per_hit": chance(1 / odds.p_hit) if odds.p_hit > 0 else None,
        "p_cycle_success": chance(odds.p_cycle_success),
        "expected_alterations": chance(odds.expected_alterations),
        "expected_augmentations": chance(odds.expected_augmentations),
        "regals": odds.regals,
        "expected_hit_value": money(odds.expected_hit_value),
        "p_miss": chance(odds.p_miss),
        "miss_value": money(miss_value),
        "expected_value": money(ev),
        "costs": {"base": money(base), **{k: money(v) for k, v in orbs.items()}},
        "cost_per_attempt": money(cost),
        "profit_per_attempt": money(profit),
        "minutes_per_attempt": minutes_per_attempt,
        "profit_per_hour": money(per_hour),
        "profit_per_hour_div": None if per_hour is None else round(per_hour / chaos_per_divine, 3),
    }
