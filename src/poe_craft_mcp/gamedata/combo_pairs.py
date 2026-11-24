"""Cluster notables that can roll for one enchant, and which pairs one rare jewel can hold."""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any

from poe_craft_mcp.gamedata.cluster import CLUSTER_ASSUMPTIONS, CLUSTER_DOMAIN, apply_enchant
from poe_craft_mcp.gamedata.draws import CountModel
from poe_craft_mcp.gamedata.lookup import resolve_base
from poe_craft_mcp.gamedata.method import RARE_JEWEL
from poe_craft_mcp.gamedata.models import BaseItem, GameData, GameDataError, ModDef
from poe_craft_mcp.gamedata.pool import PoolEntry, build_pool, entry_for

NOTABLE_TAG = "has_affliction_notable"
SMALL_TAG = "expansion_jewel_small"


def is_notable(mod: ModDef) -> bool:
    """Cluster notables are the affliction_jewel mods that add the has_affliction_notable tag."""
    return mod.domain == CLUSTER_DOMAIN and NOTABLE_TAG in mod.adds_tags


@dataclass(frozen=True)
class ClusterNotables:
    item: BaseItem
    """The base with the enchant tag added."""
    ilvl: int
    enchant: dict[str, Any] | None
    pool: tuple[PoolEntry, ...]
    """Every mod the enchanted base can roll (notables and small-passive mods)."""
    notables: tuple[PoolEntry, ...]
    pairs: tuple[frozenset[str], ...]
    """Unordered notable pairs one rare can hold, sorted by their sorted ids."""
    warnings: tuple[str, ...]
    assumptions: tuple[str, ...]


def _with_tags(item: BaseItem, mod: ModDef) -> BaseItem:
    return replace(item, tags=item.tags | mod.adds_tags)


def pair_problem(
    item: BaseItem, ilvl: int, a: ModDef, b: ModDef, model: CountModel = RARE_JEWEL
) -> str | None:
    """Why one rare `item` cannot hold both `a` and `b`, or None when it can.

    Checks mod groups, the rare side limits and generation weights after the other notable's
    adds_tags apply, in either rolling order."""
    if a.id == b.id:
        return f"{a.id} is listed twice; a pair needs two different notables"
    shared = a.groups & b.groups
    if shared:
        return f"{a.id} and {b.id} share mod group {', '.join(sorted(shared))}"
    for side, cap in (("prefix", model.max_prefix), ("suffix", model.max_suffix)):
        if (a.generation_type == side) + (b.generation_type == side) > cap:
            return f"{a.id} and {b.id} are both {side}es; a rare {item.name} holds {cap}"
    reasons = []
    for first, second in ((a, b), (b, a)):
        if isinstance(entry := entry_for(first, item, ilvl), str):
            return entry
        entry = entry_for(second, _with_tags(item, first), ilvl)
        if isinstance(entry, PoolEntry):
            return None
        reasons.append(f"after {first.id}: {entry}")
    return "; ".join(reasons)


def cluster_notables(
    data: GameData, base: str, ilvl: int, enchant: str | None, passives: int | None = None
) -> ClusterNotables:
    """Notables a cluster jewel with `enchant` can roll at `ilvl`, and the legal pairs."""
    if not 1 <= ilvl <= 100:
        raise GameDataError(f"ilvl must be between 1 and 100 (got {ilvl})")
    item, warnings = resolve_base(data, base)
    if item.domain != CLUSTER_DOMAIN:
        raise GameDataError(f"{item.name} is not a cluster jewel; combos need a cluster jewel")
    enchanted = apply_enchant(data, item, enchant, passives)
    item = enchanted.item
    warnings += enchanted.warnings
    if data.stale_warning:
        warnings.insert(0, data.stale_warning)
    pool = tuple(build_pool(data, item, ilvl))
    notables = tuple(sorted((e for e in pool if is_notable(e.mod)), key=lambda e: e.mod.id))
    pairs = tuple(
        frozenset({a.mod.id, b.mod.id})
        for a, b in combinations(notables, 2)
        if pair_problem(item, ilvl, a.mod, b.mod) is None
    )
    if not notables:
        warnings.append(f"no notable can roll on this {item.name} at item level {ilvl}")
    elif not pairs:
        small = " (a Small Cluster Jewel holds one notable)" if SMALL_TAG in item.tags else ""
        warnings.append(f"no two notables fit on one rare {item.name}{small}, so no pairs")
    return ClusterNotables(
        item=item,
        ilvl=ilvl,
        enchant=enchanted.report,
        pool=pool,
        notables=notables,
        pairs=pairs,
        warnings=tuple(warnings),
        assumptions=enchanted.assumptions or CLUSTER_ASSUMPTIONS,
    )
