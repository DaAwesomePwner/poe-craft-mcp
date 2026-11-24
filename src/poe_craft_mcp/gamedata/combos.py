"""Sellable cluster notable combos: exact odds and expected value of one crafting attempt.

Strategy: Alteration (and Augmentation) until any anchor notable appears, then one Regal Orb.
Anchors are the notables in any sellable pair. The rare is a hit when its notables hold a
sellable pair (credited to the most valuable one it holds) and a miss otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from typing import Any

from poe_craft_mcp.gamedata import magic
from poe_craft_mcp.gamedata.combo_draws import PoolCache, candidate, magic_states, regal_notables
from poe_craft_mcp.gamedata.combo_pairs import (
    ClusterNotables,
    cluster_notables,
    is_notable,
    pair_problem,
)
from poe_craft_mcp.gamedata.method import (
    AUG_SPENDING_RULE,
    JEWEL_LIMITS,
    MAGIC_COUNT,
    REGAL_RULE,
)
from poe_craft_mcp.gamedata.models import GameData, GameDataError
from poe_craft_mcp.gamedata.pool import entry_for

__all__ = ["ComboOdds", "best_pair", "cluster_notables", "combo_odds"]

Pair = frozenset[str]

COMBO_ASSUMPTIONS = (
    MAGIC_COUNT,
    "Anchor cycle: Alteration; its first affix X1 is drawn from the whole pool, a second affix "
    "X2 from the mods still allowed. If X1 is an anchor (a notable in any sellable pair), a "
    "1-affix Alteration (1/2) stops there without an Augmentation, a 2-affix one gives "
    "{X1, X2}. Otherwise the cycle succeeds only when X2 (the second affix or the Augmentation) "
    "is an anchor",
    AUG_SPENDING_RULE,
    REGAL_RULE + "; adds_tags apply only at the Regal step (as in mod_odds), not to the "
    "magic jewel's second affix",
    JEWEL_LIMITS,
    "The rare is a hit when its notables include a sellable pair, credited to the highest-value "
    "sellable pair it holds; any other rare (including one whose two notables are not a "
    "sellable pair) is a miss worth miss_value (0 when not given)",
    "Probabilities are exact (enumerated), not sampled",
)


@dataclass(frozen=True)
class ComboOdds:
    base: str
    ilvl: int
    enchant: dict[str, Any] | None
    anchors: tuple[str, ...]
    p_cycle_success: float
    """Chance one alt_aug cycle shows an anchor."""
    magic_states: dict[Pair, float]
    """Magic jewel when the cycle stops (mod ids), given that it stopped."""
    expected_alterations: float
    expected_augmentations: float
    regals: float
    p_hit: float
    per_pair: dict[Pair, float]
    """Chance per attempt of a rare credited to each sellable pair."""
    expected_hit_value: float
    p_miss: float
    miss_value: float | None
    expected_miss_value: float
    expected_value: float
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...]


def best_pair(notables: frozenset[str], values: Mapping[Pair, float]) -> Pair | None:
    """The highest-value sellable pair among `notables` (ties: first by sorted ids)."""
    held = [frozenset(p) for p in combinations(sorted(notables), 2) if frozenset(p) in values]
    return max(held, key=lambda p: values[p]) if held else None


def _finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value)


def _check_sellable(
    data: GameData, info: ClusterNotables, sellable: Mapping[Pair, float]
) -> dict[Pair, float]:
    if not sellable:
        raise GameDataError("sellable must list at least one notable pair with its chaos value")
    rollable = {e.mod.id for e in info.notables}
    legal = set(info.pairs)
    values: dict[Pair, float] = {}
    for raw_pair, value in sellable.items():
        pair = frozenset(raw_pair)
        if len(pair) != 2:
            raise GameDataError(f"sellable pair {sorted(pair)} must name two different notables")
        for mod_id in sorted(pair - rollable):
            mod = data.mods.get(mod_id)
            if mod is None:
                raise GameDataError(f"unknown mod id {mod_id!r} in sellable pair {sorted(pair)}")
            reason = entry_for(mod, info.item, info.ilvl)
            why = reason if isinstance(reason, str) else f"{mod_id} is not a cluster notable"
            raise GameDataError(f"sellable pair {sorted(pair)} cannot roll: {why}")
        if pair not in legal:
            a, b = (data.mods[m] for m in sorted(pair))
            why = pair_problem(info.item, info.ilvl, a, b) or "not a legal pair"
            raise GameDataError(f"sellable pair {sorted(pair)} cannot be on one rare: {why}")
        if not _finite_number(value):
            raise GameDataError(f"sellable pair {sorted(pair)} needs a finite chaos value")
        values[pair] = float(value)
    return values


def combo_odds(
    data: GameData,
    base: str,
    ilvl: int,
    enchant: str | None,
    passives: int | None,
    sellable: Mapping[Pair, float],
    miss_value: float | None = None,
) -> ComboOdds:
    """Exact odds and expected value of one anchor-cycle + Regal attempt (see module doc)."""
    info = cluster_notables(data, base, ilvl, enchant, passives)
    values = _check_sellable(data, info, sellable)
    if miss_value is not None and not _finite_number(miss_value):
        raise GameDataError("miss_value must be a finite chaos value")
    anchors = frozenset().union(*values)
    candidates = [candidate(e, anchors) for e in info.pool]
    raw_states = magic_states(candidates)
    p_cycle = sum(raw_states.values())
    states = {s: p / p_cycle for s, p in raw_states.items()}

    cache: PoolCache = {}
    per_pair = dict.fromkeys(sorted(values, key=sorted), 0.0)
    p_miss = 0.0
    for state, p_state in states.items():
        held = [data.mods[m] for m in state]
        notables = frozenset(m.id for m in held if is_notable(m))
        for added, p_add in regal_notables(data, info.item, ilvl, held, cache).items():
            rare = notables | {added} if added else notables
            pair = best_pair(rare, values)
            if pair is None:
                p_miss += p_state * p_add
            else:
                per_pair[pair] += p_state * p_add

    hit_value = sum(p * values[pair] for pair, p in per_pair.items())
    miss_ev = p_miss * (miss_value or 0.0)
    return ComboOdds(
        base=info.item.name,
        ilvl=ilvl,
        enchant=info.enchant,
        anchors=tuple(sorted(anchors)),
        p_cycle_success=p_cycle,
        magic_states=states,
        expected_alterations=1 / p_cycle,
        expected_augmentations=magic.augs_per_cycle(candidates) / p_cycle,
        regals=1.0,
        p_hit=sum(per_pair.values()),
        per_pair=per_pair,
        expected_hit_value=hit_value,
        p_miss=p_miss,
        miss_value=miss_value,
        expected_miss_value=miss_ev,
        expected_value=hit_value + miss_ev,
        assumptions=(*COMBO_ASSUMPTIONS, *info.assumptions),
        warnings=info.warnings,
    )
