"""`get_league_status`: league, chaos per divine, cached data ages, POESESSID and trade limits."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp import secrets
from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError, divine_chaos
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope
from poe_craft_mcp.http.fetch import FetchError

NO_TRADE_CALLS = {"note": "no trade calls yet in this session"}


def trade_rate_limit(ctx: AppContext) -> dict[str, Any]:
    """The trade limiter's state, without creating the trade client just to report it."""
    client = ctx.services.get("trade")
    return dict(NO_TRADE_CALLS) if client is None else client.limiter.state()


def register(server: MCPServer, ctx: AppContext) -> None:
    ninja = get_ninja(ctx)

    @server.tool(
        name="get_league_status",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def get_league_status() -> dict[str, Any]:
        """Current league (id and name), chaos per divine, the age in seconds of each cached
        poe.ninja dataset, whether a POESESSID is configured (never its value) and the trade
        site rate-limit state (`paused_until` epoch seconds when paused)."""
        try:
            league = await ninja.current_league()
            currency = await ninja.dataset("Currency", league)
            chaos_per_divine = divine_chaos(currency.rows)
            datasets = await ninja.cached_ages(league.id)
        except (NinjaError, FetchError) as exc:
            raise ToolError(str(exc)) from exc
        payload = {
            "league": league.id,
            "league_name": league.name,
            "chaos_per_divine": chaos_per_divine,
            "datasets": datasets,
            "poesessid_set": await asyncio.to_thread(secrets.poesessid_set),
            "trade_rate_limit": trade_rate_limit(ctx),
        }
        return envelope(payload, currency.sources())
