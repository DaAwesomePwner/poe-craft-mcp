"""Inputs of `cluster_combos` and the pricing assumptions it states."""

from __future__ import annotations

import math
from dataclasses import dataclass

from poe_craft_mcp.services.combo_queries import SALE_FILTER_ASSUMPTION
from poe_craft_mcp.services.trade_pricing import MAX_SAMPLE

PRICING_ASSUMPTIONS = (
    "Only instant-buyout (asynchronous trade) listings are used; whisper listings never are",
    SALE_FILTER_ASSUMPTION,
    "Threshold T = min_value_div x poe.ninja chaos per Divine; discovery price bounds use the "
    "trade site's own chaos-equivalent conversion, listings are converted at poe.ninja rates",
    "Discovery: a pair with min_listings or more listings at or below T is not sellable; a pair "
    "with fewer there and any at or above T is a candidate, confirmed by its exact search",
    "A pair's value is the p25 of its cheapest listings less undercut_pct; it is sellable when "
    "that value is at least T and it has at least min_listings listings",
    "Exact pair searches exclude every other priceable notable (a `not` group), so a "
    "three-notable jewel never counts as a listing of the pair; notables without their own "
    "trade id cannot be excluded",
    "A miss sells as a rare with exactly one anchor notable and no other priceable notable "
    "(p25 less undercut_pct); the white base is bought at its p25 without undercut",
    "Pair searches fetch max(trade_sample, min_listings) listings (at most 100); a stale cached "
    "copy (served because the trade site refused a new search) never confirms a pair or prices "
    "a miss or base",
)


@dataclass(frozen=True)
class ComboRequest:
    base: str
    enchant: str
    passives: int
    ilvl: int
    min_value_div: float = 1.0
    min_listings: int = 3
    max_pair_searches: int = 12
    discovery_sample: int = 100
    minutes_per_attempt: float = 4.0
    max_seconds: float = 90.0

    def check(self) -> None:
        """Raise ValueError for an argument out of range."""
        positive = {
            "min_value_div": self.min_value_div,
            "minutes_per_attempt": self.minutes_per_attempt,
            "max_seconds": self.max_seconds,
        }
        for name, value in positive.items():
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be a finite number > 0 (got {value})")
        if not 1 <= self.min_listings <= MAX_SAMPLE:
            raise ValueError(
                f"min_listings must be 1-{MAX_SAMPLE}, the most listings one search fetches "
                f"(got {self.min_listings})"
            )
        if self.max_pair_searches < 0:
            raise ValueError(f"max_pair_searches must be >= 0 (got {self.max_pair_searches})")
        if not 1 <= self.discovery_sample <= MAX_SAMPLE:
            raise ValueError(
                f"discovery_sample must be 1-{MAX_SAMPLE} (got {self.discovery_sample})"
            )

    def pair_sample(self, trade_sample: int) -> int:
        """Listings fetched per pair search: enough to count min_listings."""
        return min(MAX_SAMPLE, max(trade_sample, self.min_listings))
