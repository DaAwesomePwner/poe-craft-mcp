"""Which cluster notables can be priced on the trade site, and how pairs are reported."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from poe_craft_mcp.gamedata.combo_pairs import ClusterNotables
from poe_craft_mcp.gamedata.models import (
    GameData,
    GameDataError,
    ModDef,
    all_explicit_trade_ids,
    trade_stat_for,
)

Pair = frozenset[str]
NOTABLE_PREFIX = "1 Added Passive Skill is "


def notable_name(mod: ModDef) -> str:
    return mod.text.removeprefix(NOTABLE_PREFIX) or mod.id


@dataclass(frozen=True)
class Priceable:
    trade_ids: dict[str, str]
    """Mod id -> explicit trade stat id, for notables with exactly one (unshared) id."""
    pairs: tuple[Pair, ...]
    unpriceable_pairs: tuple[Pair, ...]
    warnings: tuple[str, ...]

    @property
    def mod_by_trade(self) -> dict[str, str]:
        return {trade: mod for mod, trade in self.trade_ids.items()}


def priceable(data: GameData, info: ClusterNotables) -> Priceable:
    """Split notables into those with one trade id of their own and the rest.

    A notable without a single explicit trade id (trade_stat_for raises), or one sharing its
    id with another notable here, cannot be searched for on its own; its pairs are reported
    as unpriceable."""
    reasons: dict[str, str] = {}
    by_trade: defaultdict[str, list[str]] = defaultdict(list)
    for entry in info.notables:
        try:
            by_trade[trade_stat_for(data, entry.mod)].append(entry.mod.id)
        except GameDataError as exc:
            reasons[entry.mod.id] = str(exc)
    trade_ids: dict[str, str] = {}
    for trade, mods in by_trade.items():
        if len(mods) == 1:
            trade_ids[mods[0]] = trade
        else:
            for mod_id in mods:
                reasons[mod_id] = f"{mod_id} shares trade id {trade} with {', '.join(mods)}"
    pairs = tuple(p for p in info.pairs if p <= trade_ids.keys())
    unpriced = tuple(p for p in info.pairs if not p <= trade_ids.keys())
    warnings = []
    for mod_id, reason in sorted(reasons.items()):
        n = sum(mod_id in p for p in info.pairs)
        name = notable_name(data.mods[mod_id])
        warnings.append(f"{name} cannot be priced ({reason}); its {n} pair(s) are unpriceable")
    return Priceable(trade_ids, pairs, unpriced, tuple(warnings))


def exclusion_ids(data: GameData, *infos: ClusterNotables) -> tuple[frozenset[str], list[str]]:
    """Every explicit trade id of every notable in `infos` (pass the pool at the top item
    level too: sale searches accept any ilvl), shared or ambiguous ids included, so a `not`
    group or the discovery grouping can rule out jewels with a third notable. Warns about
    notables with no explicit trade id at all, which no search can exclude."""
    ids: set[str] = set()
    missing: dict[str, str] = {}
    for info in infos:
        for entry in info.notables:
            found = all_explicit_trade_ids(data, entry.mod)
            ids.update(found)
            if not found:
                missing[entry.mod.id] = notable_name(entry.mod)
    warnings = [
        f"{name} has no explicit trade id, so pair and miss listings may include it as a "
        "third notable"
        for _, name in sorted(missing.items())
    ]
    return frozenset(ids), warnings


def enchant_option(data: GameData, info: ClusterNotables) -> int:
    """The trade filter option for the jewel's enchant; ValueError when it has none."""
    enchant_id = (info.enchant or {}).get("id")
    cluster = data.clusters.get(info.item.id)
    found = next((e for e in cluster.enchants if e.id == enchant_id), None) if cluster else None
    if found is None or found.trade_option is None:
        raise ValueError(
            f"enchant {enchant_id!r} has no trade site filter option, so its jewels cannot be "
            "searched for"
        )
    return found.trade_option


def pair_row(data: GameData, pair: Pair, trade_ids: Mapping[str, str]) -> dict[str, Any]:
    mods = sorted(pair)
    return {
        "pair": " + ".join(notable_name(data.mods[m]) for m in mods),
        "mod_ids": mods,
        "trade_ids": [trade_ids.get(m) for m in mods],
    }


def pair_names(data: GameData, pairs: Iterable[Pair], limit: int) -> list[str]:
    names = sorted(" + ".join(sorted(notable_name(data.mods[m]) for m in p)) for p in pairs)
    return names[:limit]


def pool_change(info: ClusterNotables, other: ClusterNotables) -> str | None:
    """How the mod pool at `other.ilvl` differs from `info`'s, or None when the same.

    Every weighted entry counts, notable or not: any change moves the anchor odds."""
    mine = {e.mod.id: e.weight for e in info.pool if e.weight > 0}
    theirs = {e.mod.id: e.weight for e in other.pool if e.weight > 0}
    if mine == theirs:
        return None
    added = sorted(theirs.keys() - mine.keys())
    gone = sorted(mine.keys() - theirs.keys())
    reweighted = sorted(m for m in mine.keys() & theirs.keys() if mine[m] != theirs[m])
    parts = [
        f"{label}: {', '.join(ids)}"
        for label, ids in (("adds", added), ("loses", gone), ("reweights", reweighted))
        if ids
    ]
    return "; ".join(parts)
