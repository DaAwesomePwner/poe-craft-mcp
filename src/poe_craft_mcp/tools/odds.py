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
    async def mod_odds(
        base: str,
        ilvl: int,
        target_mods: list[str],
        method: str,
        enchant: str | None = None,
        passives: int | None = None,
        existing_mods: list[str] | None = None,
    ) -> dict[str, Any]:
        """Chance per attempt of rolling at least one of `target_mods` (mods.json ids such as
        'IncreasedLife7') on `base` (base type name, e.g. 'Vaal Regalia') at item level `ilvl`.
        `method`: 'chaos', 'alt', 'alt_aug' (Alteration, plus an Augmentation when it gave one
        non-target affix and the open side can still roll a target; augmentations per success
        are counted under that rule), 'regal' (adds one mod to the magic item whose mods are
        `existing_mods`: every mod on it, e.g. the notable and its second affix),
        'essence:<essence name>' or 'fossil:<fossil>[,<fossil>...]'.
        Cluster jewels (Small/Medium/Large Cluster Jewel) need `enchant`: the 'Added Small
        Passive Skills grant' enchant by name or id, e.g. 'Chaos Damage over Time' or
        'Minion Life'; `passives` (added passive count) is optional.
        Returns chance_per_attempt, expected_attempts, n90 (attempts for a 90% chance),
        expected_materials (alteration/augmentation/regal per success), the resolved enchant,
        the weight table used, assumptions, export_version and warnings."""
        options = {"enchant": enchant, "passives": passives, "existing_mods": existing_mods}
        try:
            data = await asyncio.to_thread(get_gamedata, ctx)
            result = await asyncio.to_thread(
                compute_odds, data, base, ilvl, target_mods, method, **options
            )
        except GameDataError as exc:
            raise ToolError(str(exc)) from exc
        return envelope(result, sources=[export_source(data)])
