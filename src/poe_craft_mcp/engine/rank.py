"""Rank evaluated crafts by profit per hour with risk and liquidity filters."""

from collections.abc import Iterable
from dataclasses import dataclass

from poe_craft_mcp.engine.types import Breakdown

RISKS = ("low", "medium", "high")
LOW_VOLUME_FACTOR = 10


@dataclass(frozen=True)
class RankedCraft:
    """An included craft with its breakdown and non-fatal liquidity flags."""

    breakdown: Breakdown
    flags: tuple[str, ...]


@dataclass(frozen=True)
class Exclusion:
    """Why a candidate was left out: the first failing check, in a fixed order."""

    id: str
    reason: str


@dataclass(frozen=True)
class RankResult:
    """Ranked crafts, exclusions and, when `items` is empty, a note saying why."""

    items: tuple[RankedCraft, ...]
    excluded: tuple[Exclusion, ...]
    omitted_by_limit: int
    note: str | None


def _liquidity_drop(b: Breakdown, min_listings: int) -> str | None:
    for o in b.outcomes:
        if o.is_failure or o.probability == 0 or o.p25 is None:
            continue
        if o.listing_count is None:
            return f"outcome '{o.name}' has unknown listing count"
        if o.listing_count < min_listings:
            return f"outcome '{o.name}' has {o.listing_count} listings (< {min_listings})"
    return None


def _volume_flags(b: Breakdown) -> tuple[str, ...]:
    """Flag materials whose poe.ninja volume is under 10x the expected quantity to buy."""
    flags = []
    for m in (*([b.base] if b.base else []), *b.materials):
        if m.volume is not None and m.volume < LOW_VOLUME_FACTOR * m.expected_qty:
            flags.append(
                f"low volume: {m.name} volume {m.volume:g} < "
                f"{LOW_VOLUME_FACTOR} x expected qty {m.expected_qty:g}"
            )
    return tuple(flags)


def _exclusion(
    b: Breakdown, budget_chaos: float, min_listings: int, max_minutes: float | None, risk: str
) -> str | None:
    if not b.complete:
        return "unpriced inputs: cost unknown"
    if max_minutes is not None and b.minutes > max_minutes:
        return f"{b.minutes:g} min per attempt exceeds max_minutes {max_minutes:g}"
    drop = _liquidity_drop(b, min_listings)
    if drop:
        return f"illiquid: {drop}"
    if risk == "low" and b.c90 > budget_chaos:
        return f"C90 {b.c90:g}c exceeds budget {budget_chaos:g}c (risk=low)"
    if risk == "medium" and b.c > budget_chaos:
        return f"C {b.c:g}c exceeds budget {budget_chaos:g}c (risk=medium)"
    return None


def rank(
    candidates: Iterable[Breakdown],
    budget_div: float,
    chaos_per_divine: float,
    min_listings: int = 10,
    max_minutes: float | None = None,
    risk: str = "medium",
    limit: int = 5,
) -> RankResult:
    """Filter and sort candidates by profit/hr desc, then profit desc, then id asc.

    Checks run in order and the first failure is the reported reason: unpriced inputs,
    `max_minutes` (minutes per attempt), liquidity (any priced outcome with probability > 0 and
    fewer than `min_listings` listings, or an unknown count, is dropped), then the budget by
    risk (low: C90, medium: C, high: none). Low-volume materials are flagged, not dropped.
    """
    if risk not in RISKS:
        raise ValueError(f"risk must be one of {RISKS}, got {risk!r}")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if budget_div < 0 or chaos_per_divine <= 0:
        raise ValueError("budget_div must be >= 0 and chaos_per_divine > 0")
    budget_chaos = budget_div * chaos_per_divine
    pool = list(candidates)
    kept: list[Breakdown] = []
    excluded: list[Exclusion] = []
    for b in pool:
        reason = _exclusion(b, budget_chaos, min_listings, max_minutes, risk)
        if reason:
            excluded.append(Exclusion(b.id, reason))
        else:
            kept.append(b)
    kept.sort(key=lambda b: (-b.profit_per_hour, -b.profit, b.id))
    items = tuple(RankedCraft(b, _volume_flags(b)) for b in kept[:limit])
    note = None
    if not pool:
        note = "no candidate recipes were supplied"
    elif not kept:
        note = f"all {len(pool)} candidates were excluded; see `excluded` for each reason"
    return RankResult(items, tuple(excluded), max(0, len(kept) - limit), note)
