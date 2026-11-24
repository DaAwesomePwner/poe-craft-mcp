"""Discovery for one enchant of `cluster_combo_survey`, one band at a time.

Cache-only mode reads both bands from the cache and never makes a request: anything missing
or older than trade_cache_min means "not cached". Live mode searches band A, checks it, then
band B; a rate limit, the deadline or a stale copy (the trade site refused a new search)
stops at once with the retry hint, and band B is not searched after a bad band A.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from poe_craft_mcp.clients.trade import NotCached, RateLimited, TradeError, get_trade
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.envelope import source
from poe_craft_mcp.http.ratelimit import RateLimiter
from poe_craft_mcp.services.combo_discovery import Discovery, classify, tally
from poe_craft_mcp.services.combo_queries import discovery_query

if TYPE_CHECKING:
    from poe_craft_mcp.services.combo_survey import SurveyRequest
    from poe_craft_mcp.services.combo_survey_enchant import Plan

STALE_STOP = "the trade site served only a stale cached copy (rate limited or failing)"


@dataclass(frozen=True)
class Stop:
    """Live discovery must stop here; the enchant stays pending."""

    reason: str
    retry_after: float | None


@dataclass(frozen=True)
class Failed:
    """The trade site refused this enchant's search for a reason other than a rate limit."""

    reason: str


def _source(ctx: AppContext, league: str, found: Mapping[str, Any]) -> Source:
    search_id = str(found["search_id"])
    return source(
        "pathofexile.com trade",
        url=f"{ctx.config.trade_base_url}/trade/search/{quote(league, safe='')}/{search_id}",
        age_seconds=found["age_seconds"],
        stale=found["stale"],
        search_id=search_id,
        league=league,
        from_cache=found["from_cache"],
        instant_buyout=True,
    )


def limiter_wait(limiter: RateLimiter) -> float | None:
    """The longest wait the limiter now imposes on any request kind (search, fetch or any
    other it knows), or None when it reports none: a stale copy can come from a refused
    search or a refused fetch."""
    kinds = {"search", "fetch", *limiter.kind_policy}
    wait = max(limiter.wait_for(kind) for kind in sorted(kinds))
    return wait if wait > 0 else None


def _rate_limit_stop(exc: RateLimited, now: float, own_limit: float, wait_until: float) -> Stop:
    """Ours when our deadline (not the limiter's own cap or the server) refused the wait."""
    fits = now + exc.retry_after <= own_limit
    ours = (exc.cause == "cancelled" or (exc.cause == "deadline" and fits)) and (
        wait_until < own_limit
    )
    wait = f"{exc.retry_after:.0f}s"
    if ours:
        return Stop(
            f"max_seconds time budget spent (the next trade search would wait about {wait})",
            exc.retry_after,
        )
    return Stop(f"trade site rate limited; retry in about {wait}", exc.retry_after)


async def survey_discovery(
    ctx: AppContext,
    league: str,
    plan: Plan,
    req: SurveyRequest,
    threshold: float,
    rates: Mapping[str, float],
    *,
    deadline: float,
    cache_only: bool,
) -> Discovery | Stop | Failed | None:
    """Both bands classified, or why not (None: not fresh in the cache, cache_only only)."""
    trade = get_trade(ctx)
    tallies, sources = [], []
    for bound in ({"below": threshold}, {"at_least": threshold}):
        query = discovery_query(plan.jewel, plan.split.mod_by_trade.keys(), **bound)
        own_limit = trade.limiter.deadline()
        wait_until = min(own_limit, deadline)
        try:
            if cache_only:
                found = await trade.listings_for(
                    league, query, req.discovery_sample, cache_only=True
                )
            else:
                found = await trade.listings_for(
                    league, query, req.discovery_sample, deadline=wait_until
                )
        except NotCached:
            return None
        except RateLimited as exc:
            return _rate_limit_stop(exc, ctx.cache.clock(), own_limit, wait_until)
        except TradeError as exc:
            return Failed(f"discovery failed: trade site: {exc}")
        if found["stale"]:
            if cache_only:
                return None
            wait = limiter_wait(trade.limiter)
            hint = "retry hint unavailable" if wait is None else f"retry in about {wait:.0f}s"
            return Stop(f"{STALE_STOP}; {hint}", wait)
        mods = plan.split.mod_by_trade
        listings, total = found["listings"], int(found["total"])
        tallies.append(tally(listings, rates, mods, total, plan.excluded, **bound))
        sources.append(_source(ctx, league, found))
    below, above = tallies
    classes = classify(plan.split.pairs, below, above, req.min_listings)
    return Discovery(below, above, classes, sources, [])
