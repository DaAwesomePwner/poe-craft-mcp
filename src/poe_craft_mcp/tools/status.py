"""`get_league_status` (stub until the poe.ninja client lands)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope


def register(server: MCPServer, ctx: AppContext) -> None:
    @server.tool(
        name="get_league_status",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def get_league_status() -> dict[str, Any]:
        """League id, chaos per divine, cached dataset ages, whether POESESSID is set,
        and the trade rate-limit state."""
        return envelope(
            {
                "league": ctx.config.league,
                "chaos_per_divine": None,
                "datasets": {},
                "poesessid_set": False,
                "trade_rate_limit": None,
                "note": "stub",
            }
        )
