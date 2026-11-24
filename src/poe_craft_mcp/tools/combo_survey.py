"""`cluster_combo_survey`: rank every enchant of one cluster size by its sellable pairs."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope
from poe_craft_mcp.gamedata.loader import get_gamedata
from poe_craft_mcp.gamedata.models import GameDataError
from poe_craft_mcp.services.combo_survey import SurveyRequest, cluster_combo_survey
from poe_craft_mcp.services.trade_pricing import PricingUnavailable
from poe_craft_mcp.tools.odds import export_source
from poe_craft_mcp.tools.trade import unavailable_message


def register(server: MCPServer, ctx: AppContext) -> None:
    @server.tool(
        name="cluster_combo_survey",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def cluster_combo_survey_tool(
        base: str,
        passives: int,
        ilvl: int,
        min_value_div: float = 1.0,
        min_listings: int = 3,
        discovery_sample: int = 100,
        max_seconds: float = 90.0,
        enchants: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Which cluster jewel enchant gives the best chance per attempt of landing a notable
        pair worth at least `min_value_div` divines.

        Inputs: `base` ('Medium Cluster Jewel' or 'Large Cluster Jewel'; a Small one holds one
        notable, so it has no pairs), `passives` (added passive count; sales are searched with
        exactly this count), `ilvl` (item level the odds are computed at), optional `enchants`
        (names or ids; default every current enchant of the base with a trade filter).
        For each enchant: two cheap discovery searches (rares with 2+ of its notables priced
        strictly below / at or above the threshold, `discovery_sample` 1-100 listings each).
        A candidate pair (fewer than `min_listings` listings below, some at or above) with at
        least `min_listings` listings at or above the threshold gets a PROVISIONAL value: the
        `min_listings`-th cheapest of them, less undercut; fewer listings make it a thin
        candidate, never provisionally sellable. Pairs worth at least the threshold feed the
        exact odds of the Alteration/Augmentation anchor cycle plus one Regal.
        Returns a ranking (by p_any_provisional, then sellable pair count; at most `limit`
        rows) with the provisional sellable, candidate, thin and not_sellable counts, the chance per
        attempt, expected attempts per hit, provisional hit value per attempt and the top
        pairs; plus skipped enchants with reasons, pending ones, discovery totals, sources,
        assumptions and warnings. Nothing is confirmed: run `cluster_combos` on the top
        enchants for exact pair prices and profit. Enchants with fresh cached searches go
        first, from the cache with no request; live searches run until `max_seconds`, a rate
        limit or a stale reply, the rest are pending (with the retry hint): call again.
        Only instant-buyout listings are used, never whisper listings.
        """
        req = SurveyRequest(
            base=base,
            passives=passives,
            ilvl=ilvl,
            min_value_div=min_value_div,
            min_listings=min_listings,
            discovery_sample=discovery_sample,
            max_seconds=max_seconds,
            enchants=None if enchants is None else tuple(enchants),
            limit=limit,
        )
        try:
            data = await asyncio.to_thread(get_gamedata, ctx)
            payload, sources = await cluster_combo_survey(ctx, data, req)
        except (GameDataError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        except PricingUnavailable as exc:
            raise ToolError(unavailable_message(exc)) from None
        return envelope(payload, [export_source(data), *sources])
