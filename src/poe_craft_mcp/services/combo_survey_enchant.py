"""One enchant in `cluster_combo_survey`: selection, the `cluster_combos` guards and the
provisional value of its discovery candidates.

Provisional means discovery prices only: no exact pair search confirms them (`cluster_combos`
does that).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from poe_craft_mcp.gamedata.cluster import LEGACY_TAG_PREFIX, resolve_enchant
from poe_craft_mcp.gamedata.combo_pairs import ClusterNotables
from poe_craft_mcp.gamedata.combos import cluster_notables, combo_odds
from poe_craft_mcp.gamedata.models import ClusterEnchant, ClusterJewel, GameData
from poe_craft_mcp.services.combo_costs import chance, money
from poe_craft_mcp.services.combo_discovery import Discovery
from poe_craft_mcp.services.combo_notables import (
    Priceable,
    exclusion_ids,
    notable_name,
    priceable,
)
from poe_craft_mcp.services.combo_queries import JewelFilter
from poe_craft_mcp.services.combo_report import classes_report

if TYPE_CHECKING:
    from poe_craft_mcp.services.combo_survey import SurveyRequest

Pair = frozenset[str]
TOP_PAIRS = 3


@dataclass(frozen=True)
class Plan:
    """An enchant that passed every guard and is ready for discovery."""

    enchant: ClusterEnchant
    info: ClusterNotables
    split: Priceable
    excluded: frozenset[str]
    jewel: JewelFilter


def skip_row(enchant: ClusterEnchant, reason: str) -> dict[str, str]:
    return {"enchant": enchant.name, "enchant_id": enchant.id, "reason": reason}


def select_enchants(
    cluster: ClusterJewel, names: Sequence[str] | None
) -> tuple[list[ClusterEnchant], list[dict[str, str]]]:
    """Enchants to survey in file order (all, or the named ones resolved like
    `cluster_combos`), and the legacy or unsearchable ones skipped with a reason."""
    chosen = list(cluster.enchants)
    if names is not None:
        picked = {resolve_enchant(cluster, name).id for name in names}
        chosen = [e for e in chosen if e.id in picked]
    selected, skipped = [], []
    for enchant in chosen:
        if enchant.tag.startswith(LEGACY_TAG_PREFIX):
            why = f"legacy enchant (tag {enchant.tag!r}): new jewels do not drop with it"
            skipped.append(skip_row(enchant, why))
        elif enchant.trade_option is None:
            why = "no trade site filter option (attribute enchant): its jewels cannot be searched"
            skipped.append(skip_row(enchant, why))
        else:
            selected.append(enchant)
    return selected, skipped


def plan_enchant(data: GameData, req: SurveyRequest, enchant: ClusterEnchant) -> Plan | str:
    """The `cluster_combos` guards for one enchant: a Plan, or why it is skipped."""
    info = cluster_notables(data, req.base, req.ilvl, enchant.id, req.passives)
    if not info.pairs:
        return "no two notables fit on one rare: no pairs"
    split = priceable(data, info)
    if not split.pairs:
        return "no legal pair has two notables with their own trade ids: nothing to price"
    # Sale searches accept any item level: rule out every notable the base can ever roll.
    top = cluster_notables(data, req.base, 100, enchant.id, req.passives)
    excluded, unidentifiable = exclusion_ids(data, info, top)
    if unidentifiable:
        return (
            f"cannot rule out jewels carrying {', '.join(unidentifiable)} (no explicit trade id "
            "in the game data), so no pair is valued; refresh the game data export"
        )
    assert enchant.trade_option is not None  # select_enchants keeps only searchable ones
    jewel = JewelFilter(info.item.name, enchant.trade_option, req.passives)
    return Plan(enchant, info, split, excluded, jewel)


def provisional_value(above: Sequence[float], min_listings: int, undercut: float) -> float:
    """The min_listings-th cheapest listing at or above T, or the cheapest one when fewer are
    listed, less undercut (a seller must undercut the cheapest competing listing). `above` is
    sorted ascending and not empty."""
    k = min_listings if len(above) >= min_listings else 1
    return above[k - 1] * (1 - undercut)


def _discovery_summary(disc: Discovery) -> dict[str, Any]:
    tallies = {"below": disc.below, "at_or_above": disc.above}
    summary: dict[str, Any] = {
        name: {"total": t.total, "fetched": t.fetched} for name, t in tallies.items()
    }
    summary["truncated"] = [name for name, t in tallies.items() if t.truncated]
    return summary


async def evaluate(
    data: GameData,
    req: SurveyRequest,
    plan: Plan,
    disc: Discovery,
    threshold: float,
    undercut: float,
    per_divine: float,
) -> tuple[float, dict[str, Any]]:
    """The unrounded p_any_provisional (for ranking) and the ranking row of one enchant."""
    candidates = [p for p, c in disc.classes.items() if c == "candidate"]
    values = {
        p: provisional_value(disc.above.pairs[p], req.min_listings, undercut) for p in candidates
    }
    sellable = {p: v for p, v in values.items() if v >= threshold}
    p_any = hit_value = 0.0
    if sellable:
        odds = await asyncio.to_thread(
            combo_odds, data, req.base, req.ilvl, plan.enchant.id, req.passives, sellable
        )
        p_any, hit_value = odds.p_hit, odds.expected_hit_value
    top = sorted(sellable, key=lambda p: (-sellable[p], sorted(p)))[:TOP_PAIRS]
    classes = classes_report(disc)
    return p_any, {
        "enchant": plan.enchant.name,
        "enchant_id": plan.enchant.id,
        "provisional_sellable_pairs": len(sellable),
        "candidates": classes["candidate"],
        "not_sellable": classes["not_sellable"],
        "unpriceable_pairs": len(plan.split.unpriceable_pairs),
        "p_any_provisional": chance(p_any),
        "expected_attempts_per_hit": round(1 / p_any, 1) if p_any > 0 else None,
        "provisional_hit_value_per_attempt": {
            "chaos": money(hit_value),
            "divine": round(hit_value / per_divine, 4),
        },
        "top_pairs": [
            {
                "pair": " + ".join(sorted(notable_name(data.mods[m]) for m in p)),
                "provisional_value_div": round(sellable[p] / per_divine, 3),
                "listings_at_or_above": len(disc.above.pairs[p]),
            }
            for p in top
        ],
        "from_cache": all(s.get("from_cache") for s in disc.sources),
        "discovery": _discovery_summary(disc),
    }
