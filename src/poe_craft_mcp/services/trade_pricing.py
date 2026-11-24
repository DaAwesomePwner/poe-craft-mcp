"""Price one craft outcome from trade-site listings, in chaos.

`price_outcome` is the single entry point for tools (`price_trade_query`) and the craft
evaluator: it searches the trade site (cheapest online listings, through the shared rate
limiter and listing cache), converts every price to chaos with poe.ninja currency rates and
summarizes it with `engine.pricing.summarize`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from poe_craft_mcp.clients.ninja import get_ninja, rates_from
from poe_craft_mcp.clients.ninja_models import League, NinjaError, divine_chaos
from poe_craft_mcp.clients.trade import RateLimited, TradeClient, TradeError, get_trade
from poe_craft_mcp.clients.trade_models import (
    STATUS_INSTANT_BUYOUT,
    parse_saved_url,
    query_from_body,
)
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.pricing import summarize
from poe_craft_mcp.engine.types import Listing, Source
from poe_craft_mcp.envelope import source
from poe_craft_mcp.http.fetch import FetchError
from poe_craft_mcp.log import mask_secrets

MIN_SAMPLE = 1
MAX_SAMPLE = 100


class PricingUnavailable(Exception):
    """No price could be produced and no cached listings exist to fall back on.

    `reason` is safe to show to the user (secret-masked, no stack trace). `retry_after` is
    the seconds to wait when the trade site rate-limited us, else None.
    """

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        self.reason = mask_secrets(reason)
        self.retry_after = retry_after
        super().__init__(self.reason)


@dataclass(frozen=True)
class OutcomePrice:
    """`PriceSummary` (chaos) plus provenance of the trade search and the currency rates.

    `stale` is True when any input (listings, rates or league lookup) is an old cached copy
    served because upstream failed; `age_seconds` is the age of the listings. `sources` are
    envelope source dicts: the poe.ninja league lookup (omitted when the caller passed
    `league`), poe.ninja Currency rates, the trade search (in that order).
    `chaos_per_divine` is for display only.
    """

    p10: float | None
    p25: float | None
    median: float | None
    listing_count: int
    raw_count: int
    dropped_outliers: int
    dropped_unknown_currency: dict[str, int]
    currency_mix: dict[str, int]
    cheapest_listing_age_seconds: float | None
    search_id: str
    total: int
    sample: int
    league: str
    stale: bool
    age_seconds: float
    chaos_per_divine: float | None
    sources: list[Source]


def clamp_sample(sample: int | None, default: int) -> int:
    return max(MIN_SAMPLE, min(MAX_SAMPLE, default if sample is None else sample))


def parse_indexed(value: Any) -> datetime | None:
    """A listing's `indexed` ISO-8601 timestamp, or None when missing or malformed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def to_listings(raw: list[dict[str, Any]]) -> list[Listing]:
    listings = []
    for item in raw:
        amount, currency = item.get("amount"), item.get("currency")
        if isinstance(amount, int | float) and isinstance(currency, str):
            listings.append(Listing(float(amount), currency, parse_indexed(item.get("indexed"))))
    return listings


async def wants_instant_buyout(trade: TradeClient, target: str | dict[str, Any]) -> bool:
    """True when a search query itself filters on instant buyout (`status: securable`), so
    pricing keeps that filter instead of widening it to every online listing."""
    if isinstance(target, str):
        tq = await trade.resolve_saved_url(target)
    else:
        tq = query_from_body(target, "search")
    status = tq.query.get("status")
    return (
        tq.kind == "search"
        and isinstance(status, dict)
        and status.get("option") == STATUS_INSTANT_BUYOUT
    )


async def price_outcome(
    ctx: AppContext,
    *,
    trade_url: str | None = None,
    query_json: dict[str, Any] | None = None,
    sample: int | None = None,
    league: str | None = None,
) -> OutcomePrice:
    """Price the item matched by a saved trade URL or a trade query JSON (exactly one).

    `sample` (default `config.trade_sample`, clamped to 1..100) is how many of the cheapest
    online listings are used (instant-buyout only when the query's status asks for it).
    `league` defaults to poe.ninja's current league; a trade URL's own league is ignored.
    Raises `ValueError` for bad arguments and `PricingUnavailable` when rates or listings
    cannot be had (rate limited or failed with nothing cached).
    """
    target: str | dict[str, Any]
    if trade_url is not None and query_json is None:
        try:
            target, kind = trade_url, parse_saved_url(trade_url).kind
        except TradeError as exc:
            raise ValueError(str(exc)) from None
    elif query_json is not None and trade_url is None:
        target, kind = query_json, "search"
    else:
        raise ValueError("pass exactly one of trade_url or query_json")
    n = clamp_sample(sample, ctx.config.trade_sample)
    ninja = get_ninja(ctx)
    try:
        resolved = await ninja.current_league() if league is None else League(league, league)
        currency = await ninja.dataset("Currency", resolved)
    except (FetchError, NinjaError) as exc:
        raise PricingUnavailable(f"poe.ninja currency rates unavailable: {exc}") from None
    rates = rates_from(currency.rows)
    try:
        per_divine: float | None = divine_chaos(currency.rows)
    except NinjaError:
        per_divine = None

    try:
        trade = get_trade(ctx)
        instant = await wants_instant_buyout(trade, target)
        found = await trade.listings_for(resolved.id, target, n, instant_buyout=instant)
    except RateLimited as exc:
        raise PricingUnavailable(str(exc), exc.retry_after) from None
    except TradeError as exc:
        raise PricingUnavailable(f"trade site: {exc}") from None

    now = datetime.fromtimestamp(ctx.cache.clock(), UTC)
    summary = summarize(to_listings(found["listings"]), rates, now)
    search_id = str(found["search_id"])
    trade_source = source(
        "pathofexile.com trade",
        url=f"{ctx.config.trade_base_url}/trade/{kind}/{quote(resolved.id, safe='')}/{search_id}",
        age_seconds=found["age_seconds"],
        stale=found["stale"],
        search_id=search_id,
        league=resolved.id,
        from_cache=found["from_cache"],
        instant_buyout=instant,
    )
    ninja_sources = currency.sources() if league is None else [currency.source()]
    sources = [*ninja_sources, trade_source]
    return OutcomePrice(
        **dataclasses.asdict(summary),
        search_id=search_id,
        total=int(found["total"]),
        sample=n,
        league=resolved.id,
        stale=any(s.get("stale") for s in sources),
        age_seconds=float(found["age_seconds"]),
        chaos_per_divine=per_divine,
        sources=sources,
    )
