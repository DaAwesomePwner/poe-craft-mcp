"""`get_league_status`: league, chaos per divine and the age of cached poe.ninja data."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError, divine_chaos
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope
from poe_craft_mcp.http.fetch import FetchError

PENDING = ["poesessid_set", "trade_rate_limit"]


def register(server: MCPServer, ctx: AppContext) -> None:
    ninja = get_ninja(ctx)

    @server.tool(
        name="get_league_status",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def get_league_status() -> dict[str, Any]:
        """Current league (id and name), chaos per divine, and the age in seconds of each
        cached poe.ninja dataset. POESESSID and trade rate-limit state are not wired up yet."""
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
            "poesessid_set": None,
            "trade_rate_limit": None,
            "pending": PENDING,
        }
        return envelope(payload, currency.sources())
