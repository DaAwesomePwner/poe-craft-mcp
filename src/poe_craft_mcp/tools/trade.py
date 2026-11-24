"""`price_trade_query`: cheapest-listing percentiles for a trade search, in chaos."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope
from poe_craft_mcp.services.trade_pricing import PricingUnavailable, price_outcome

FIELDS = (
    "p10",
    "p25",
    "median",
    "listing_count",
    "raw_count",
    "dropped_outliers",
    "dropped_unknown_currency",
    "cheapest_listing_age_seconds",
    "currency_mix",
    "total",
    "search_id",
    "sample",
    "league",
    "stale",
)


def unavailable_message(exc: PricingUnavailable) -> str:
    if exc.retry_after is None:
        return f"Price unavailable: {exc.reason}"
    return f"Price unavailable: {exc.reason}. Try again in about {exc.retry_after:.0f}s."


def register(server: MCPServer, ctx: AppContext) -> None:
    @server.tool(
        name="price_trade_query",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def price_trade_query(
        trade_url: str | None = None,
        query_json: dict[str, Any] | None = None,
        sample: int | None = None,
    ) -> dict[str, Any]:
        """Price an item from the pathofexile.com trade site (current poe.ninja league).

        Pass exactly one of `trade_url` (a saved /trade/search or /trade/exchange URL; its
        league is replaced by the current one) or `query_json` (a trade search body).
        Uses the `sample` cheapest online listings (1-100, default from config; instant
        buyout only if the query asks for it), converted to chaos with poe.ninja rates;
        outliers and unknown currencies are dropped. Returns p10/p25/median in chaos (plus
        divine equivalents), counts and the currency mix. `stale: true` means old cached
        listings were served because the trade site is rate limiting.
        """
        try:
            price = await price_outcome(
                ctx, trade_url=trade_url, query_json=query_json, sample=sample
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        except PricingUnavailable as exc:
            raise ToolError(unavailable_message(exc)) from None
        payload: dict[str, Any] = {name: getattr(price, name) for name in FIELDS}
        per_divine = price.chaos_per_divine
        payload["chaos_per_divine"] = per_divine
        payload["divine"] = {
            name: None if value is None or not per_divine else round(value / per_divine, 3)
            for name, value in (("p10", price.p10), ("p25", price.p25), ("median", price.median))
        }
        return envelope(payload, price.sources)
