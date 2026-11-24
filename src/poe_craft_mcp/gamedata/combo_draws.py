"""Exact draws for the combo strategy: the alt_aug anchor cycle, then one Regal Orb.

Same sequential-draw model as `draws` and `magic`: each affix is drawn from the mods still
allowed (no shared mod group, side not full) in proportion to weight.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace

from poe_craft_mcp.gamedata.combo_pairs import is_notable
from poe_craft_mcp.gamedata.draws import MAGIC, Candidate, CountModel
from poe_craft_mcp.gamedata.method import RARE_JEWEL
from poe_craft_mcp.gamedata.models import BaseItem, GameData, ModDef
from poe_craft_mcp.gamedata.pool import PoolEntry, build_pool

ONE_AFFIX, TWO_AFFIX = dict(MAGIC.counts)[1], dict(MAGIC.counts)[2]
NONE: frozenset[str] = frozenset()
PoolCache = dict[frozenset[str], list[tuple[Candidate, bool]]]
"""Regal pools by item tags: each rollable mod as a Candidate, and whether it is a notable."""


def candidate(entry: PoolEntry, targets: frozenset[str]) -> Candidate:
    mod = entry.mod
    return Candidate(
        mod.id,
        entry.side == "prefix",
        mod.groups or frozenset({mod.id}),
        entry.weight,
        mod.id in targets,
    )


def _fits(c: Candidate, held: Sequence[Candidate], model: CountModel) -> bool:
    if any(c.id == h.id or c.groups & h.groups for h in held):
        return False
    used = sum(h.is_prefix == c.is_prefix for h in held)
    return used < (model.max_prefix if c.is_prefix else model.max_suffix)


def magic_states(candidates: Sequence[Candidate]) -> dict[frozenset[str], float]:
    """Chance per alt_aug cycle of each magic jewel that ends the cycle (holds a target).

    The Alteration's first affix X1 is drawn from the whole pool, X2 from the mods still
    allowed. X1 a target: half the time (1-affix Alteration) the jewel is {X1} and is not
    augmented, otherwise {X1, X2} ({X1} if nothing is allowed). X1 not a target: success only
    when X2 (second Alteration affix or the Augmentation) is a target, giving {X1, X2}."""
    live = [c for c in candidates if c.weight > 0]
    total = sum(c.weight for c in live)
    states: defaultdict[frozenset[str], float] = defaultdict(float)
    if total <= 0:
        return {}
    for x1 in live:
        p1 = x1.weight / total
        rest = [c for c in live if _fits(c, (x1,), MAGIC)]
        rest_total = sum(c.weight for c in rest)
        if x1.is_target:
            states[frozenset({x1.id})] += p1 * (ONE_AFFIX + (TWO_AFFIX if rest_total <= 0 else 0))
            if rest_total <= 0:
                continue
            for x2 in rest:
                states[frozenset({x1.id, x2.id})] += p1 * TWO_AFFIX * x2.weight / rest_total
        elif rest_total > 0:
            for x2 in rest:
                if x2.is_target:
                    states[frozenset({x1.id, x2.id})] += p1 * x2.weight / rest_total
    return dict(states)


def regal_notables(
    data: GameData,
    item: BaseItem,
    ilvl: int,
    held: Sequence[ModDef],
    cache: PoolCache,
) -> dict[str | None, float]:
    """Chance the Regal adds each notable to the magic jewel `held`; key None = no notable
    (a small-passive mod, or nothing when no mod is allowed).

    The pool is rebuilt as `mod_odds` regal does: item tags plus the held mods' adds_tags,
    their mod groups blocked, rare jewel side limits (RARE_JEWEL). Pools are cached by tags."""
    tags = item.tags.union(*(m.adds_tags for m in held))
    if tags not in cache:
        pool = build_pool(data, replace(item, tags=tags), ilvl)
        cache[tags] = [(candidate(e, NONE), is_notable(e.mod)) for e in pool if e.weight > 0]
    placed = [candidate(PoolEntry(m, 1, 1.0), NONE) for m in held]
    weights: dict[str | None, float] = {}
    total = 0.0
    for c, notable in cache[tags]:
        if not _fits(c, placed, RARE_JEWEL):
            continue
        key = c.id if notable else None
        weights[key] = weights.get(key, 0.0) + c.weight
        total += c.weight
    if total <= 0:
        return {None: 1.0}
    return {key: weight / total for key, weight in weights.items()}
