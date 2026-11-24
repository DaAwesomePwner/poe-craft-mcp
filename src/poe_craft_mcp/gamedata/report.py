"""Shape `mod_odds` results: weight table, calculation details, attempt counts."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from poe_craft_mcp.gamedata.draws import Estimate
from poe_craft_mcp.gamedata.pool import PoolEntry

TABLE_TOP = 15
GENERAL_ASSUMPTIONS = (
    "Each affix is drawn in turn with probability proportional to its weight among mods still "
    "allowed (no second mod from the same mod group, side not full); this is the community model "
    "of item generation, not a rule published by GGG",
    "Spawn weight is the first entry in the mod's spawn_weights whose tag the base has, times the "
    "first matching generation_weights entry / 100 when the mod has one",
    "Only the base's own tags are used, plus a cluster jewel's enchant tag and, for 'regal', the "
    "tags existing_mods add: influence, tags added by mods rolled in the same step (adds_tags), "
    "bench crafts and metamods are ignored",
    "Chance is per currency use for at least one of target_mods; mod tiers/values beyond the ids "
    "given are not considered",
)


def attempts_for(probability: float, confidence: float = 0.9) -> int | None:
    """Attempts needed to hit at least once with `confidence` (n90 for 0.9)."""
    if probability <= 0:
        return None
    if probability >= 1:
        return 1
    return math.ceil(math.log(1 - confidence) / math.log(1 - probability))


def weight_table(pool: Sequence[PoolEntry], targets: set[str]) -> dict[str, Any]:
    ranked = sorted(pool, key=lambda e: (-e.weight, e.mod.id))
    shown = ranked[:TABLE_TOP] + [e for e in ranked[TABLE_TOP:] if e.mod.id in targets]

    def total(entries: Sequence[PoolEntry]) -> float:
        return round(sum(e.weight for e in entries), 4)

    return {
        "entries": [
            {
                "id": e.mod.id,
                "text": e.mod.text,
                "side": e.side,
                "groups": sorted(e.mod.groups),
                "required_level": e.mod.required_level,
                "spawn_weight": e.spawn_weight,
                "multiplier": round(e.multiplier, 4),
                "weight": round(e.weight, 4),
                "target": e.mod.id in targets,
            }
            for e in shown
        ],
        "shown": len(shown),
        "eligible_mods": len(pool),
        "totals": {
            "prefix": total([e for e in pool if e.side == "prefix"]),
            "suffix": total([e for e in pool if e.side == "suffix"]),
            "all": total(pool),
            "targets": total([e for e in pool if e.mod.id in targets]),
        },
    }


def calculation(estimate: Estimate) -> dict[str, Any]:
    calc: dict[str, Any] = {"method": estimate.method, "resolved": estimate.resolved}
    if estimate.method == "monte_carlo":
        calc.update(samples=estimate.samples, seed=estimate.seed)
        if estimate.resolved:
            calc["std_error"] = round(estimate.std_error or 0.0, 8)
            calc["ci95"] = [round(x, 8) for x in estimate.ci95 or ()]
        else:
            calc["upper_bound_95"] = round(estimate.upper_bound_95 or 0.0, 8)
    if estimate.detail:
        calc["chance_by_affix_count"] = {k: round(v, 6) for k, v in estimate.detail.items()}
    return calc


def expected_materials(kind: str, label: str, p: float | None, augs: float) -> dict | None:
    """Currency used per success for the single-currency magic methods (None otherwise)."""
    if not p:
        return None
    if label == "alt":
        return {"alteration": round(1 / p, 4)}
    if label == "alt_aug":
        return {"alteration": round(1 / p, 4), "augmentation": round(augs / p, 4)}
    if kind == "regal":
        return {"regal": round(1 / p, 4)}
    return None
