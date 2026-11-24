"""Eligible mod pool for one base item: spawn-weight matching, ilvl gating, fossil multipliers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from poe_craft_mcp.gamedata.models import (
    AFFIX_TYPES,
    BaseItem,
    Fossil,
    GameData,
    ModDef,
    TagWeights,
)


@dataclass(frozen=True)
class PoolEntry:
    mod: ModDef
    spawn_weight: int
    multiplier: float

    @property
    def weight(self) -> float:
        return self.spawn_weight * self.multiplier

    @property
    def side(self) -> str:
        return self.mod.generation_type


def first_match(weights: TagWeights, tags: Iterable[str]) -> int | None:
    """Weight of the first entry whose tag the item has (list order decides, not tag order)."""
    tag_set = set(tags)
    for tag, weight in weights:
        if tag in tag_set:
            return weight
    return None


def fossil_multiplier(mod: ModDef, fossils: Sequence[Fossil]) -> float:
    multiplier = 1.0
    for fossil in fossils:
        for tag, weight in fossil.weights:
            if tag in mod.tags:
                multiplier *= weight / 100
    return multiplier


def entry_for(
    mod: ModDef,
    base: BaseItem,
    ilvl: int,
    fossils: Sequence[Fossil] = (),
    added: frozenset[str] = frozenset(),
) -> PoolEntry | str:
    """The pool entry for `mod` on `base`, or the reason it cannot roll."""
    is_added = mod.id in added
    if mod.generation_type not in AFFIX_TYPES:
        return f"{mod.id} is a {mod.generation_type} mod, not a prefix or suffix"
    if mod.domain != base.domain and not is_added:
        return f"{mod.id} has domain {mod.domain!r}; {base.name} rolls {base.domain!r} mods"
    if mod.is_essence_only and not is_added:
        return f"{mod.id} is essence-only and cannot roll from currency"
    if mod.required_level > ilvl:
        return f"{mod.id} needs item level {mod.required_level} (got {ilvl})"
    spawn = first_match(mod.spawn_weights, base.tags) or 0
    if spawn <= 0:
        return f"{mod.id} has spawn weight 0 on {base.name} (tags: {', '.join(sorted(base.tags))})"
    generation = first_match(mod.generation_weights, base.tags)
    multiplier = (1.0 if generation is None else generation / 100) * fossil_multiplier(mod, fossils)
    if generation == 0:
        tags = ", ".join(sorted(base.tags))
        return f"{mod.id} has generation weight 0 on this {base.name} (tags: {tags})"
    if multiplier <= 0:
        names = ", ".join(f.name for f in fossils) or "generation weights"
        return f"{mod.id} is blocked by {names} (weight multiplier 0)"
    return PoolEntry(mod, spawn, multiplier)


def build_pool(
    data: GameData,
    base: BaseItem,
    ilvl: int,
    fossils: Sequence[Fossil] = (),
    blocked_groups: frozenset[str] = frozenset(),
) -> list[PoolEntry]:
    """Every mod that can roll on `base` with positive weight, minus mods in blocked groups."""
    added = frozenset(m for f in fossils for m in f.added_mods)
    pool: list[PoolEntry] = []
    for mod in data.mods.values():
        if mod.groups & blocked_groups:
            continue
        entry = entry_for(mod, base, ilvl, fossils, added)
        if isinstance(entry, PoolEntry):
            pool.append(entry)
    return pool
