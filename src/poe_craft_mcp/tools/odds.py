"""`mod_odds`: chance per attempt of rolling target mods, from RePoE spawn weights."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope, source
from poe_craft_mcp.gamedata.loader import get_gamedata
from poe_craft_mcp.gamedata.models import GameData, GameDataError
from poe_craft_mcp.gamedata.odds import mod_odds as compute_odds


def export_source(data: GameData) -> dict[str, Any]:
    downloaded = data.downloaded_at
    return source(
        f"RePoE export {data.game_version or '(unknown game version)'}",
        url=data.source_url,
        age_seconds=time.time() - downloaded.timestamp() if downloaded else None,
        stale=data.stale_warning is not None,
        game_version=data.game_version,
        downloaded_at=downloaded.isoformat() if downloaded else None,
    )


def register(server: MCPServer, ctx: AppContext) -> None:
    @server.tool(
        name="mod_odds",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def mod_odds(base: str, ilvl: int, target_mods: list[str], method: str) -> dict[str, Any]:
        """Chance per attempt of rolling at least one of `target_mods` (mods.json ids such as
        'IncreasedLife7') on `base` (base type name, e.g. 'Vaal Regalia') at item level `ilvl`.
        `method`: 'chaos', 'alt', 'essence:<essence name>' or 'fossil:<fossil>[,<fossil>...]'.
        Returns chance_per_attempt, expected_attempts, n90 (attempts for a 90% chance), the
        weight table used, assumptions, export_version and warnings."""
        try:
            data = await asyncio.to_thread(get_gamedata, ctx)
            result = await asyncio.to_thread(compute_odds, data, base, ilvl, target_mods, method)
        except GameDataError as exc:
            raise ToolError(str(exc)) from exc
        return envelope(result, sources=[export_source(data)])
