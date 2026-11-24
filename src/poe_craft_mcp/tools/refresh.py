"""`refresh`: user-forced reload of cached poe.ninja data, at most once per cooldown."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope
from poe_craft_mcp.http.fetch import FetchError

COOLDOWN_S = 300.0
ALWAYS = ("Currency",)


def register(server: MCPServer, ctx: AppContext) -> None:
    ninja = get_ninja(ctx)
    last_run: float | None = None

    @server.tool(
        name="refresh",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def refresh() -> dict[str, Any]:
        """Only when the user explicitly asks: reload the league list and every poe.ninja
        dataset cached for the current league now, instead of waiting out the 15-minute
        refresh floor. Unchanged data is revalidated cheaply. At most once per 5 minutes;
        during the cooldown nothing is fetched and the remaining seconds are returned.
        Never touches the trade site."""
        nonlocal last_run
        now = ctx.cache.clock()
        if last_run is not None and now - last_run < COOLDOWN_S:
            remaining = round(COOLDOWN_S - (now - last_run), 1)
            payload = {
                "refreshed": False,
                "cooldown_remaining_seconds": remaining,
                "message": f"Refresh is on cooldown; try again in {remaining:.0f}s.",
            }
            return envelope(payload)
        last_run = now
        try:
            league = await ninja.current_league(force=True)
            cached = await ninja.cached_ages(league.id)
        except (NinjaError, FetchError) as exc:
            raise ToolError(f"poe.ninja league lookup failed: {exc}") from exc
        datasets, errors = await ninja.force_refresh([*ALWAYS, *cached], league)
        payload = {
            "refreshed": True,
            "league": league.id,
            "datasets": {
                ds.type: {"age_seconds": ds.age_seconds, "stale": ds.stale} for ds in datasets
            },
            "errors": errors,
            "cooldown_seconds": COOLDOWN_S,
        }
        return envelope(payload, [league.source(), *(ds.source() for ds in datasets)])
