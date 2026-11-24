"""Cluster jewel enchants: which notables and small-passive mods a cluster jewel can roll.

Rule (from the export): the "Added Small Passive Skills grant: ..." enchant carries a tag
(cluster_jewels.json `passive_skills[].tag`, e.g. `affliction_chaos_damage_over_time_multiplier`).
That tag joins the jewel's base tags, and mods.json `spawn_weights` of affliction_jewel mods
reference it (e.g. AfflictionNotableWickedPall_: affliction_chaos_damage 259,
affliction_chaos_damage_over_time_multiplier 348, default 0), so the enchant picks the pool.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, replace
from typing import Any

from poe_craft_mcp.gamedata.lookup import normalize
from poe_craft_mcp.gamedata.models import (
    FETCH_HINT,
    BaseItem,
    ClusterEnchant,
    ClusterJewel,
    GameData,
    GameDataError,
)

CLUSTER_DOMAIN = "affliction_jewel"
WIKI_URL = "https://www.poewiki.net/wiki/Cluster_jewel"
_ENCHANT_PREFIX = normalize("Added Small Passive Skills grant:")
LEGACY_TAG_PREFIX = "old_do_not_use_"

CLUSTER_ASSUMPTIONS = (
    "The cluster enchant adds its tag (RePoE cluster_jewels.json passive_skills[].tag) to the "
    "jewel's tags; affliction_jewel mods' spawn_weights reference these tags, so the enchant "
    "decides which notables and small-passive mods can roll",
    "Notable limits follow from the export plus the affix limits: a notable adds the tag "
    "has_affliction_notable (adds_tags), whose generation weight 0 is matched before "
    "expansion_jewel_small, so a Small Cluster Jewel holds one notable; Medium (both notables "
    f"prefixes) and Large (two prefixes, one suffix) are capped by the side limits ({WIKI_URL})",
    "The number of added passives does not change mod weights (the export has no such rule)",
)


@dataclass(frozen=True)
class EnchantResult:
    item: BaseItem
    """The base with the enchant tag added (unchanged for non-cluster bases)."""
    report: dict[str, Any] | None
    warnings: list[str]
    assumptions: tuple[str, ...]


def _keys(enchant: ClusterEnchant) -> set[str]:
    keys = {enchant.id, enchant.name, enchant.tag, *enchant.stat_text}
    return {normalize(k) for k in keys}


def resolve_enchant(cluster: ClusterJewel, query: str) -> ClusterEnchant:
    """Match an enchant id, name ("Chaos Damage over Time"), tag or stat text, case-insensitive;
    an unambiguous fragment of a name or stat text also matches."""
    key = normalize(query).removeprefix(_ENCHANT_PREFIX)
    if not key:
        raise GameDataError("enchant must not be empty")
    exact = [e for e in cluster.enchants if key in _keys(e)]
    partial = [e for e in cluster.enchants if any(key in k for k in _keys(e))]
    for matches in (exact, partial):
        if len(matches) == 1:
            return matches[0]
        if matches:
            names = ", ".join(f"{e.name!r} ({e.id})" for e in matches)
            raise GameDataError(
                f"enchant {query!r} is ambiguous on {cluster.name}: {names}; use the enchant id"
            )
    names = [e.name for e in cluster.enchants]
    close = difflib.get_close_matches(query, names, n=5, cutoff=0.4)
    hint = f" Closest: {', '.join(close)}." if close else ""
    raise GameDataError(
        f"unknown enchant {query!r} for {cluster.name}.{hint} Enchants: {', '.join(names)}"
    )


def _cluster(data: GameData, item: BaseItem) -> ClusterJewel:
    cluster = data.clusters.get(item.id)
    if cluster is None:
        missing = "is not in the export" if not data.clusters else f"has no {item.name}"
        raise GameDataError(
            f"{item.name} is a cluster jewel but cluster_jewels.json {missing}; the enchant "
            f"rules come from that file — {FETCH_HINT}"
        )
    return cluster


def apply_enchant(
    data: GameData, item: BaseItem, enchant: str | None, passives: int | None
) -> EnchantResult:
    """Add a cluster jewel's enchant tag to its tags. Non-cluster bases ignore the enchant."""
    if item.domain != CLUSTER_DOMAIN:
        ignored = [
            f"{name} ignored: {item.name} is not a cluster jewel"
            for name, value in (("enchant", enchant), ("passives", passives))
            if value is not None
        ]
        return EnchantResult(item, None, ignored, ())
    cluster = _cluster(data, item)
    if enchant is None or not enchant.strip():
        names = ", ".join(e.name for e in cluster.enchants)
        raise GameDataError(
            f"{item.name} needs `enchant` (the 'Added Small Passive Skills grant' enchant decides "
            f"which notables can roll); one of: {names}"
        )
    chosen = resolve_enchant(cluster, enchant)
    if passives is not None and not cluster.min_skills <= passives <= cluster.max_skills:
        raise GameDataError(
            f"{item.name} adds {cluster.min_skills}-{cluster.max_skills} passive skills "
            f"(got passives={passives})"
        )
    warnings = []
    if chosen.tag.startswith(LEGACY_TAG_PREFIX):
        warnings.append(
            f"enchant {chosen.name!r} uses the legacy tag {chosen.tag!r}; it is from an older "
            "patch and new jewels may not drop with it"
        )
    report = {
        "id": chosen.id,
        "name": chosen.name,
        "tag": chosen.tag,
        "text": list(chosen.stat_text),
        "passives": passives,
        "passive_range": [cluster.min_skills, cluster.max_skills],
    }
    tagged = replace(item, tags=item.tags | {chosen.tag})
    return EnchantResult(tagged, report, warnings, CLUSTER_ASSUMPTIONS)
