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
ATTRIBUTE_ENCHANT_TAGS = frozenset(
    {"affliction_strength", "affliction_dexterity", "affliction_intelligence"}
)


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
        stat_ids=tuple(str(stat["id"]) for stat in raw.get("stats", ())),
    )


def parse_trade_stats(raw: list[dict[str, Any]]) -> dict[str, str]:
    """stat_translations.json: stat id -> explicit trade stat id, for translations of exactly
    one stat with exactly one explicit trade stat (others are ambiguous and left out)."""
    out: dict[str, str] = {}
    for entry in raw:
        ids = entry.get("ids") or []
        explicit = [t["id"] for t in entry.get("trade_stats") or [] if t.get("type") == "explicit"]
        if len(ids) == 1 and len(explicit) == 1:
            out[str(ids[0])] = str(explicit[0])
    return out


def parse_explicit_trade_ids(raw: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """stat_translations.json: stat id -> every explicit trade stat id of the translations
    that include it, ambiguous ones too (e.g. Evil Eye's two ids, shared with another
    notable). For excluding a mod from a search, never for searching for it."""
    found: dict[str, set[str]] = {}
    for entry in raw:
        explicit = {
            str(t["id"]) for t in entry.get("trade_stats") or [] if t.get("type") == "explicit"
        }
        for stat in entry.get("ids") or []:
            found.setdefault(str(stat), set()).update(explicit)
    return {stat: tuple(sorted(ids)) for stat, ids in found.items() if ids}


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


def enchant_trade_option(position: int, tag: str) -> int | None:
    """Trade option of a cluster enchant: its 1-based `position` across every
    cluster_jewels.json passive_skills list in file order (Large, Medium, Small), or None for
    the attribute enchants (Strength, Dexterity, Intelligence; told apart by tag, not position).

    Evidence: https://www.pathofexile.com/api/trade/data/stats, filter enchant.stat_3948993189
    ("Added Small Passive Skills grant: #"), has options 1-55 in this order for the RePoE
    3.30.0.4.3 export, with no option for the three attribute enchants (positions 51-53);
    checked 2026-11-24."""
    return None if tag in ATTRIBUTE_ENCHANT_TAGS else position


def parse_clusters(raw: dict[str, Any]) -> dict[str, ClusterJewel]:
    """cluster_jewels.json: per base, the passive-count range and the enchants it can drop with."""
    clusters: dict[str, ClusterJewel] = {}
    position = 0
    for base_id, entry in raw.items():
        enchants = []
        for skill in entry["passive_skills"]:
            position += 1
            tag = str(skill["tag"])
            enchants.append(
                ClusterEnchant(
                    id=str(skill["id"]),
                    name=str(skill["name"]),
                    tag=tag,
                    stat_text=tuple(str(t) for t in skill.get("stat_text", ())),
                    trade_option=enchant_trade_option(position, tag),
                )
            )
        clusters[base_id] = ClusterJewel(
            base_id=base_id,
            name=str(entry["name"]),
            min_skills=int(entry["min_skills"]),
            max_skills=int(entry["max_skills"]),
            enchants=tuple(enchants),
        )
    return clusters
