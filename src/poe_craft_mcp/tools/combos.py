"""`cluster_combos`: sellable notable pairs on a cluster jewel and the value of crafting them."""

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
from poe_craft_mcp.services.cluster_combos import ComboRequest, cluster_combos
from poe_craft_mcp.services.trade_pricing import PricingUnavailable
from poe_craft_mcp.tools.odds import export_source
from poe_craft_mcp.tools.trade import unavailable_message


def register(server: MCPServer, ctx: AppContext) -> None:
    @server.tool(
        name="cluster_combos",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def cluster_combos_tool(
        base: str,
        enchant: str,
        passives: int,
        ilvl: int,
        min_value_div: float = 1.0,
        min_listings: int = 3,
        max_pair_searches: int = 12,
        discovery_sample: int = 100,
        minutes_per_attempt: float = 4.0,
        max_seconds: float = 90.0,
    ) -> dict[str, Any]:
        """Which notable pairs on one cluster jewel sell for at least `min_value_div` divines,
        and the expected profit of crafting for ANY of them.

        Inputs: `base` ('Medium Cluster Jewel' or 'Large Cluster Jewel'), `enchant` (the
        'Added Small Passive Skills grant' enchant by name or id, e.g. 'Minion Life'),
        `passives` (added passive count; sales are searched with exactly this count),
        `ilvl` (item level the odds are computed at).
        Strategy priced: Transmutation, then Alteration (and Augmentation) until the magic
        jewel shows an "anchor" (a notable from any sellable pair), then one Regal Orb.
        "Sellable" pair: its exact trade search has at least `min_listings` instant-buyout
        listings and their p25, less the configured undercut, is at least `min_value_div`
        divines. Only instant-buyout listings are used, never whisper listings.
        How: two discovery searches (rares with 2+ of the notables, priced below / at or above
        the threshold; `discovery_sample` 1-100 listings each) sort pairs into not_sellable,
        candidate, thin and unlisted; candidates (most likely first) are confirmed with their
        exact search: cached ones free, then up to `max_pair_searches` live searches within
        `max_seconds`. Unconfirmed candidates are listed; call again to continue (confirmed
        searches stay cached). Small Cluster Jewels hold one notable, so they have no pairs.
        Returns the pair counts and classes, every sellable pair (value, listings, chance per
        attempt), p_any_sellable, expected attempts per hit, expected alterations and
        augmentations, EV, cost and profit per attempt and per hour (`minutes_per_attempt`),
        with sources, assumptions and warnings; unavailable prices are null, never guessed.
        With nothing sellable it shows the top not_sellable and candidate pairs instead.
        """
        req = ComboRequest(
            base=base,
            enchant=enchant,
            passives=passives,
            ilvl=ilvl,
            min_value_div=min_value_div,
            min_listings=min_listings,
            max_pair_searches=max_pair_searches,
            discovery_sample=discovery_sample,
            minutes_per_attempt=minutes_per_attempt,
            max_seconds=max_seconds,
        )
        try:
            data = await asyncio.to_thread(get_gamedata, ctx)
            payload, sources = await cluster_combos(ctx, data, req)
        except (GameDataError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        except PricingUnavailable as exc:
            raise ToolError(unavailable_message(exc)) from None
        return envelope(payload, [export_source(data), *sources])
