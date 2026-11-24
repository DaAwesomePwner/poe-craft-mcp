"""Parse raw RePoE export records into the dataclasses mod odds use."""

from __future__ import annotations

from typing import Any

from poe_craft_mcp.gamedata.models import (
    ClusterEnchant,
    ClusterJewel,
    Fossil,
    ModDef,
    TagWeights,
)

AFFIX_LIMIT_STATS = ("local_maximum_prefixes_allowed_+", "local_maximum_suffixes_allowed_+")


def tag_weights(raw: list[dict[str, Any]]) -> TagWeights:
    return tuple((str(w["tag"]), int(w["weight"])) for w in raw)


def parse_mod(mod_id: str, raw: dict[str, Any]) -> ModDef:
    return ModDef(
        id=mod_id,
        domain=raw["domain"],
        generation_type=raw["generation_type"],
        required_level=int(raw.get("required_level", 0)),
        groups=frozenset(raw.get("groups", ())),
        tags=frozenset(raw.get("implicit_tags", ())),
        spawn_weights=tag_weights(raw.get("spawn_weights", [])),
        generation_weights=tag_weights(raw.get("generation_weights", [])),
        is_essence_only=bool(raw.get("is_essence_only", False)),
        text=str(raw.get("text") or raw.get("name") or mod_id),
        adds_tags=frozenset(raw.get("adds_tags", ())),
    )


def parse_fossil(fossil_id: str, raw: dict[str, Any]) -> Fossil:
    unmodelled = []
    if raw.get("rolls_lucky"):
        unmodelled.append("lucky values and more common high-level mods (no exported weight rule)")
    if raw.get("corrupted_essence_chance"):
        unmodelled.append(f"{raw['corrupted_essence_chance']}% corrupted essence mod chance")
    if raw.get("mirrors"):
        unmodelled.append("fractures a random modifier")
    if raw.get("sell_price_mods"):
        unmodelled.append("adds vendor sell-price mods")
    weights = tag_weights(raw["positive_mod_weights"] + raw["negative_mod_weights"])
    added, forced = tuple(raw.get("added_mods", ())), tuple(raw.get("forced_mods", ()))
    if not (weights or added or forced or unmodelled):
        descriptions = raw.get("descriptions", {}).values()
        text = "; ".join(" ".join(str(d).split()) for d in descriptions)
        unmodelled.append(f"effect not expressed as mod weights ({text or 'undescribed'})")
    return Fossil(
        id=fossil_id,
        name=raw["name"],
        weights=weights,
        added_mods=added,
        forced_mods=forced,
        allowed_tags=frozenset(raw.get("allowed_tags", ())),
        forbidden_tags=frozenset(raw.get("forbidden_tags", ())),
        unmodelled=tuple(unmodelled),
    )


def affix_deltas(implicits: list[str], raw_mods: dict[str, Any]) -> tuple[int, int]:
    deltas = {stat: 0 for stat in AFFIX_LIMIT_STATS}
    for implicit in implicits:
        for stat in raw_mods.get(implicit, {}).get("stats", []):
            if stat["id"] in deltas:
                deltas[stat["id"]] += int(stat["min"])
    prefix_stat, suffix_stat = AFFIX_LIMIT_STATS
    return deltas[prefix_stat], deltas[suffix_stat]


def parse_clusters(raw: dict[str, Any]) -> dict[str, ClusterJewel]:
    """cluster_jewels.json: per base, the passive-count range and the enchants it can drop with."""
    return {
        base_id: ClusterJewel(
            base_id=base_id,
            name=str(entry["name"]),
            min_skills=int(entry["min_skills"]),
            max_skills=int(entry["max_skills"]),
            enchants=tuple(
                ClusterEnchant(
                    id=str(skill["id"]),
                    name=str(skill["name"]),
                    tag=str(skill["tag"]),
                    stat_text=tuple(str(t) for t in skill.get("stat_text", ())),
                )
                for skill in entry["passive_skills"]
            ),
        )
        for base_id, entry in raw.items()
    }
