"""Listing-price cleanup and percentiles, all in chaos."""

import math
import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from poe_craft_mcp.engine.types import Listing, PriceSummary

CHEAPEST_WINDOW = 10
OUTLIER_FRACTION = 0.5


def percentile(sorted_values: list[float], q: float) -> float:
    """Percentile `q` in [0, 1] by linear interpolation (numpy's default method).

    The rank is `q * (n - 1)` over ascending values; fractional ranks interpolate between
    the two neighbours. Raises ValueError on empty input or `q` outside [0, 1].
    """
    if not sorted_values:
        raise ValueError("percentile of empty data")
    if not 0 <= q <= 1:
        raise ValueError(f"q must be within [0, 1], got {q}")
    rank = q * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _age_seconds(indexed_at: datetime | None, now: datetime | None) -> float | None:
    if indexed_at is None or now is None:
        return None
    if indexed_at.tzinfo is None:
        indexed_at = indexed_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return max(0.0, (now - indexed_at).total_seconds())


def summarize(
    listings: Iterable[Listing],
    rates: Mapping[str, float],
    now: datetime | None = None,
) -> PriceSummary:
    """Convert listings to chaos, drop junk, and take percentiles.

    `rates` maps a trade currency id to its chaos value (chaos itself is 1.0). Listings whose
    currency has no usable rate (missing, non-finite or <= 0) are dropped and counted per
    currency. Of the rest, those with a non-positive amount, or priced below 50% of the median
    of the 10 cheapest, count as `dropped_outliers`. `currency_mix` counts the listings kept.
    `cheapest_listing_age_seconds` needs `now`; without it the age is None. Never raises on
    empty input.
    """
    items = list(listings)
    unknown: dict[str, int] = {}
    priced: list[tuple[float, Listing]] = []
    for item in items:
        rate = rates.get(item.currency)
        if rate is None or not math.isfinite(rate) or rate <= 0:
            unknown[item.currency] = unknown.get(item.currency, 0) + 1
            continue
        priced.append((item.amount * rate, item))
    priced.sort(key=lambda pair: pair[0])

    valid = [pair for pair in priced if math.isfinite(pair[0]) and pair[0] > 0]
    kept = valid
    if valid:
        floor = OUTLIER_FRACTION * statistics.median(p for p, _ in valid[:CHEAPEST_WINDOW])
        kept = [pair for pair in valid if pair[0] >= floor]
    dropped = len(priced) - len(kept)

    mix: dict[str, int] = {}
    for _, item in kept:
        mix[item.currency] = mix.get(item.currency, 0) + 1
    if not kept:
        return PriceSummary(None, None, None, 0, len(items), dropped, unknown, mix, None)

    values = [p for p, _ in kept]
    cheapest = kept[0][0]
    ages = [_age_seconds(i.indexed_at, now) for p, i in kept if p == cheapest]
    known_ages = [a for a in ages if a is not None]
    return PriceSummary(
        p10=percentile(values, 0.10),
        p25=percentile(values, 0.25),
        median=percentile(values, 0.50),
        listing_count=len(values),
        raw_count=len(items),
        dropped_outliers=dropped,
        dropped_unknown_currency=unknown,
        currency_mix=mix,
        cheapest_listing_age_seconds=min(known_ages) if known_ages else None,
    )
