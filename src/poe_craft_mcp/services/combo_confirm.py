"""Confirmation: price each candidate pair with its exact trade search.

Candidates go in spawn-weight order (w_a * w_b, most likely first). Pairs whose search is
already cached (younger than `trade_cache_min`) are free and always taken; then at most
`max_pair_searches` live searches run before the deadline. A pair is sellable when its value
(p25 less the undercut) is at least the threshold and it has at least `min_listings` listings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from math import prod
from typing import Any

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.services.trade_pricing import OutcomePrice, PricingUnavailable, price_outcome

Pair = frozenset[str]


@dataclass(frozen=True)
class PairPrice:
    pair: Pair
    p25: float | None
    value: float | None
    """p25 less the undercut, in chaos."""
    listing_count: int
    sellable: bool
    cached: bool
    stale: bool


@dataclass
class Confirmation:
    priced: list[PairPrice] = field(default_factory=list)
    errors: dict[Pair, str] = field(default_factory=dict)
    unconfirmed: list[Pair] = field(default_factory=list)
    stopped: str | None = None
    """Why live searches stopped before every candidate was priced, else None."""
    searched: int = 0
    sources: list[Source] = field(default_factory=list)

    @property
    def sellable(self) -> list[PairPrice]:
        return [p for p in self.priced if p.sellable]


def order_candidates(candidates: Iterable[Pair], weights: Mapping[str, float]) -> list[Pair]:
    """Most likely pairs first: spawn-weight product descending, ties by sorted mod ids."""
    return sorted(candidates, key=lambda p: (-prod(weights[m] for m in p), sorted(p)))


def is_sellable(value: float | None, listings: int, threshold: float, min_listings: int) -> bool:
    return value is not None and value >= threshold and listings >= min_listings


@dataclass(frozen=True)
class Rules:
    threshold: float
    min_listings: int
    undercut: float
    """Fraction, e.g. 0.05."""
    max_searches: int
    deadline: float
    """On the trade limiter's clock (`ctx.cache.clock`)."""


def _priced(pair: Pair, price: OutcomePrice, rules: Rules, cached: bool) -> PairPrice:
    value = None if price.p25 is None else price.p25 * (1 - rules.undercut)
    return PairPrice(
        pair,
        price.p25,
        value,
        price.listing_count,
        is_sellable(value, price.listing_count, rules.threshold, rules.min_listings),
        cached,
        price.stale,
    )


async def confirm(
    ctx: AppContext,
    league: str,
    ordered: list[Pair],
    query_for: Callable[[Pair], dict[str, Any]],
    rules: Rules,
) -> Confirmation:
    """Price `ordered` candidates: every fresh cached one, then live searches in order."""
    out = Confirmation()
    fresh_s = ctx.config.trade_cache_min * 60
    uncached: list[Pair] = []
    for pair in ordered:
        try:
            price = await price_outcome(
                ctx, query_json=query_for(pair), league=league, cache_only=True
            )
        except PricingUnavailable:  # QuoteNotCached, or nothing usable cached
            uncached.append(pair)
            continue
        if price.age_seconds >= fresh_s:
            uncached.append(pair)
            continue
        out.priced.append(_priced(pair, price, rules, cached=True))
        out.sources.append(price.sources[-1])

    clock = ctx.cache.clock
    for i, pair in enumerate(uncached):
        if out.searched >= rules.max_searches:
            out.stopped = f"max_pair_searches ({rules.max_searches}) live searches used"
        elif clock() >= rules.deadline:
            out.stopped = "max_seconds time budget spent"
        if out.stopped:
            out.unconfirmed = uncached[i:]
            break
        try:
            price = await price_outcome(
                ctx, query_json=query_for(pair), league=league, deadline=rules.deadline
            )
        except PricingUnavailable as exc:
            if exc.retry_after is not None:
                out.stopped = (
                    "max_seconds time budget spent"
                    if exc.cause == "deadline"
                    else f"trade site rate limited; retry in about {exc.retry_after:.0f}s"
                )
                out.unconfirmed = uncached[i:]
                break
            out.searched += 1
            out.errors[pair] = exc.reason
            continue
        out.searched += 1
        out.priced.append(_priced(pair, price, rules, cached=False))
        out.sources.append(price.sources[-1])
    return out
