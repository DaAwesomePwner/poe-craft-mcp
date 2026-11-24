"""Value one attempt once sellable pairs are known: miss value, white base, orbs, exact odds."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.gamedata.combo_pairs import ClusterNotables
from poe_craft_mcp.gamedata.combos import ComboOdds, cluster_notables, combo_odds
from poe_craft_mcp.gamedata.models import GameData
from poe_craft_mcp.services.combo_confirm import Confirmation
from poe_craft_mcp.services.combo_costs import currency_costs, economics, trade_value
from poe_craft_mcp.services.combo_notables import pool_change
from poe_craft_mcp.services.combo_queries import (
    JewelFilter,
    base_ilvl_range,
    base_query,
    miss_query,
)

if TYPE_CHECKING:
    from poe_craft_mcp.services.combo_request import ComboRequest


@dataclass
class Valued:
    odds: ComboOdds
    economics: dict[str, Any]
    miss_listings: int | None
    base_listings: int | None
    warnings: list[str]
    sources: list[Source]


def _pool_warning(data: GameData, req: ComboRequest, info: ClusterNotables) -> str | None:
    lo, hi = base_ilvl_range(req.ilvl)
    if hi == req.ilvl:
        return None
    change = pool_change(info, cluster_notables(data, req.base, hi, req.enchant, req.passives))
    if change is None:
        return None
    return (
        f"the white base is priced at item level {lo}-{hi}, but the mod pool at {hi} differs "
        f"from item level {req.ilvl} ({change}): buying a higher-level base changes the odds"
    )


async def price_sellable(
    ctx: AppContext,
    data: GameData,
    req: ComboRequest,
    info: ClusterNotables,
    conf: Confirmation,
    trade_ids: Mapping[str, str],
    excluded: frozenset[str],
    jewel: JewelFilter,
    league: str,
    rates: Mapping[str, float],
    per_divine: float,
) -> Valued:
    """Miss value, base cost and orb costs (each null with a warning when unavailable), then
    the exact odds and per-attempt economics of crafting for the sellable pairs."""
    undercut = ctx.config.undercut_pct / 100
    values = {p.pair: p.value for p in conf.sellable if p.value is not None}
    anchors = sorted(frozenset().union(*values))
    warnings: list[str] = []

    miss_search = miss_query(jewel, [trade_ids[m] for m in anchors], excluded)
    miss = await trade_value(ctx, league, miss_search, undercut)
    miss_value = miss.chaos
    if miss.empty:
        miss_value = 0.0
        warnings.append(
            "no instant-buyout listings for a rare with exactly one anchor notable: misses are "
            "valued at 0"
        )
    elif miss_value is None:
        why = f"miss value unavailable ({miss.error})"
        warnings.append(f"{why}; expected value and profit are null")

    base = await trade_value(ctx, league, base_query(jewel, req.ilvl))
    if base.chaos is None:
        why = "no instant-buyout listings" if base.empty else base.error
        warnings.append(f"white base price unavailable ({why}); cost and profit are null")
    pool = _pool_warning(data, req, info)
    if pool:
        warnings.append(pool)
    orbs, orb_warnings = currency_costs(rates)
    warnings += orb_warnings

    odds = await asyncio.to_thread(
        combo_odds, data, req.base, req.ilvl, req.enchant, req.passives, values, miss_value
    )
    econ = economics(
        odds,
        miss_value=miss_value,
        base=base.chaos,
        orbs=orbs,
        minutes_per_attempt=req.minutes_per_attempt,
        chaos_per_divine=per_divine,
    )
    return Valued(
        odds,
        econ,
        miss.listing_count,
        base.listing_count,
        warnings,
        [*miss.sources, *base.sources],
    )
