"""Discovery: two broad instant-buyout searches that show which notable pairs may sell.

Search A finds rares with two or more priceable notables priced at most the threshold T,
search B the same at T or more. Listings are grouped by the notables they hold; a pair seen
`min_listings` times below T is not sellable, one seen above T (and rarely below) is a
candidate that the exact pair search must confirm.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from poe_craft_mcp.clients.trade import RateLimited, TradeError, get_trade
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.envelope import source
from poe_craft_mcp.services.combo_queries import JewelFilter, discovery_query
from poe_craft_mcp.services.trade_pricing import PricingUnavailable

Pair = frozenset[str]
CLASSES = ("not_sellable", "candidate", "thin", "unlisted")


@dataclass
class Tally:
    """One discovery search: listings grouped by the notable pair they hold (chaos prices)."""

    total: int = 0
    fetched: int = 0
    pairs: dict[Pair, list[float]] = field(default_factory=dict)
    multi_notable_listings: int = 0
    """Listings with three or more notables (any the base can roll at any item level);
    never attributed to a pair."""
    unmatched_listings: int = 0
    """Listings showing fewer than two of the notables (e.g. missing stat data)."""
    dropped_unknown_currency: dict[str, int] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return self.total > self.fetched

    def report(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "fetched": self.fetched,
            "pair_listings": sum(len(v) for v in self.pairs.values()),
            "multi_notable_listings": self.multi_notable_listings,
            "unmatched_listings": self.unmatched_listings,
            "dropped_unknown_currency": dict(sorted(self.dropped_unknown_currency.items())),
        }


@dataclass
class Discovery:
    below: Tally
    above: Tally
    classes: dict[Pair, str]
    sources: list[Source]
    warnings: list[str]


def tally(
    listings: Iterable[Mapping[str, Any]],
    rates: Mapping[str, float],
    mod_by_trade: Mapping[str, str],
    total: int,
    other_ids: Iterable[str] = (),
) -> Tally:
    """Group listings by the notables (`mod_by_trade`: trade id -> mod id) they show.

    `other_ids` are trade ids of any other notable the base can roll at any item level; a
    listing showing one of them holds a third notable and is never a pair listing."""
    others = frozenset(other_ids) - mod_by_trade.keys()
    found = Tally(total=total)
    pairs: defaultdict[Pair, list[float]] = defaultdict(list)
    dropped: Counter[str] = Counter()
    for item in listings:
        found.fetched += 1
        currency, amount = item.get("currency"), item.get("amount")
        rate = rates.get(currency) if isinstance(currency, str) else None
        if rate is None or not math.isfinite(rate) or rate <= 0:
            dropped[str(currency)] += 1
            continue
        if not isinstance(amount, int | float) or not math.isfinite(amount) or amount <= 0:
            found.unmatched_listings += 1
            continue
        stats = item.get("explicit_stats") or []
        held = {mod_by_trade[s] for s in stats if s in mod_by_trade}
        notables = len(held) + bool(others.intersection(stats))
        if len(held) == 2 and notables == 2:
            pairs[frozenset(held)].append(float(amount) * rate)
        elif notables > 2:
            found.multi_notable_listings += 1
        else:
            found.unmatched_listings += 1
    found.pairs = {pair: sorted(prices) for pair, prices in pairs.items()}
    found.dropped_unknown_currency = dict(dropped)
    return found


def classify(
    pairs: Iterable[Pair], below: Tally, above: Tally, min_listings: int
) -> dict[Pair, str]:
    """not_sellable: >= min_listings below T; candidate: fewer below and some at T or more;
    thin: only below, fewer than min_listings; unlisted: never seen."""
    classes = {}
    for pair in pairs:
        n_below, n_above = len(below.pairs.get(pair, ())), len(above.pairs.get(pair, ()))
        if n_below >= min_listings:
            classes[pair] = "not_sellable"
        elif n_above:
            classes[pair] = "candidate"
        else:
            classes[pair] = "thin" if n_below else "unlisted"
    return classes


async def search_listings(
    ctx: AppContext, league: str, query: dict[str, Any], sample: int, deadline: float
) -> tuple[dict[str, Any], Source]:
    """Raw instant-buyout listings for `query` plus their envelope source."""
    trade = get_trade(ctx)
    own_limit = trade.limiter.deadline()
    try:
        found = await trade.listings_for(league, query, sample, deadline=min(own_limit, deadline))
    except RateLimited as exc:
        raise PricingUnavailable(str(exc), exc.retry_after) from None
    except TradeError as exc:
        raise PricingUnavailable(f"trade site: {exc}") from None
    search_id = str(found["search_id"])
    src = source(
        "pathofexile.com trade",
        url=f"{ctx.config.trade_base_url}/trade/search/{quote(league, safe='')}/{search_id}",
        age_seconds=found["age_seconds"],
        stale=found["stale"],
        search_id=search_id,
        league=league,
        from_cache=found["from_cache"],
        instant_buyout=True,
    )
    return found, src


def _truncation(name: str, found: Tally, consequence: str) -> str:
    return (
        f"discovery {name} fetched only {found.fetched} of {found.total} listings (cheapest "
        f"first): {consequence}"
    )


async def discover(
    ctx: AppContext,
    league: str,
    jewel: JewelFilter,
    mod_by_trade: Mapping[str, str],
    other_ids: Iterable[str],
    pairs: Iterable[Pair],
    threshold: float,
    *,
    sample: int,
    min_listings: int,
    rates: Mapping[str, float],
    deadline: float,
) -> Discovery:
    """Run searches A (price <= T) and B (price >= T) and classify every pair."""
    tallies, sources = [], []
    for bound in ({"below": threshold}, {"at_least": threshold}):
        query = discovery_query(jewel, mod_by_trade.keys(), **bound)
        found, src = await search_listings(ctx, league, query, sample, deadline)
        tallies.append(
            tally(found["listings"], rates, mod_by_trade, int(found["total"]), other_ids)
        )
        sources.append(src)
    below, above = tallies
    warnings = []
    if below.truncated:
        warnings.append(
            _truncation(
                "below the threshold",
                below,
                "per-pair counts there are lower bounds, so a pair may look scarcer than it is",
            )
        )
    if above.truncated:
        warnings.append(
            _truncation(
                "at or above the threshold",
                above,
                "pairs listed only at higher prices are missed, which can hide the most "
                "expensive pairs",
            )
        )
    classes = classify(pairs, below, above, min_listings)
    return Discovery(below, above, classes, sources, warnings)
