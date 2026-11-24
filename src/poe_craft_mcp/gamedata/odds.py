"""`mod_odds`: single-step chance of hitting a target mod from spawn weights."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from poe_craft_mcp.gamedata.draws import Candidate, Estimate, Start, hit_probability
from poe_craft_mcp.gamedata.lookup import resolve_base, resolve_mods
from poe_craft_mcp.gamedata.method import CraftSetup, parse_method
from poe_craft_mcp.gamedata.models import GameData, GameDataError
from poe_craft_mcp.gamedata.pool import PoolEntry, build_pool, entry_for

TABLE_TOP = 15
GENERAL_ASSUMPTIONS = (
    "Each affix is drawn in turn with probability proportional to its weight among mods still "
    "allowed (no second mod from the same mod group, side not full); this is the community model "
    "of item generation, not a rule published by GGG",
    "Spawn weight is the first entry in the mod's spawn_weights whose tag the base has, times the "
    "first matching generation_weights entry / 100 when the mod has one",
    "Only the base's own tags are used: influence, tags added by rolled mods (adds_tags), bench "
    "crafts and metamods are ignored",
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


def _table(pool: Sequence[PoolEntry], targets: set[str]) -> dict[str, Any]:
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


def _calculation(estimate: Estimate) -> dict[str, Any]:
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


def _start(setup: CraftSetup) -> Start:
    start = Start(
        groups=frozenset(g for m in setup.forced for g in m.groups),
        prefixes=sum(m.generation_type == "prefix" for m in setup.forced),
        suffixes=sum(m.generation_type == "suffix" for m in setup.forced),
    )
    if start.prefixes > setup.model.max_prefix or start.suffixes > setup.model.max_suffix:
        raise GameDataError(f"forced mod(s) of {setup.label} exceed this base's affix limits")
    return start


def mod_odds(
    data: GameData,
    base: str,
    ilvl: int,
    target_mods: Sequence[str],
    method: str,
    *,
    force_monte_carlo: bool = False,
) -> dict[str, Any]:
    """Chance per attempt of hitting any of `target_mods` on `base` at `ilvl` with `method`.

    `chance_per_attempt` is None when sampling saw no hit; `calculation.upper_bound_95` then
    bounds it."""
    if not 1 <= ilvl <= 100:
        raise GameDataError(f"ilvl must be between 1 and 100 (got {ilvl})")
    if not target_mods:
        raise GameDataError("target_mods must list at least one mod id, e.g. ['IncreasedLife7']")
    item, warnings = resolve_base(data, base)
    targets = resolve_mods(data, dict.fromkeys(target_mods))
    setup = parse_method(data, item, method)
    warnings += list(setup.warnings)
    start = _start(setup)
    roll_ilvl = min(ilvl, setup.ilvl_cap) if setup.ilvl_cap is not None else ilvl
    pool = build_pool(data, item, roll_ilvl, setup.fossils, blocked_groups=start.groups)
    target_ids = {m.id for m in targets}
    pooled = {e.mod.id for e in pool}
    forced_ids = {m.id for m in setup.forced}
    added = frozenset(m for f in setup.fossils for m in f.added_mods)
    for mod in targets:
        if mod.id in forced_ids or mod.id in pooled:
            continue
        reason = entry_for(mod, item, roll_ilvl, setup.fossils, added)
        if isinstance(reason, PoolEntry):
            reason = f"{mod.id} shares a mod group with the forced mod, so it cannot roll"
        elif roll_ilvl < ilvl:
            reason += f"; {setup.label} caps item level at {roll_ilvl} for the other mods"
        warnings.append(f"target cannot roll: {reason}")

    if target_ids & forced_ids:
        estimate = Estimate(1.0, "forced")
    else:
        candidates = [
            Candidate(
                e.mod.id,
                e.side == "prefix",
                e.mod.groups or frozenset({e.mod.id}),
                e.weight,
                e.mod.id in target_ids,
            )
            for e in pool
        ]
        estimate = hit_probability(candidates, setup.model, start, force_monte_carlo)

    p: float | None = estimate.probability if estimate.resolved else None
    if not estimate.resolved:
        warnings.append(
            f"no hit in {estimate.samples} Monte Carlo samples; the chance is unresolved and "
            f"below {estimate.upper_bound_95:.2e} per attempt (95% one-sided bound)"
        )
    if data.stale_warning:
        warnings.insert(0, data.stale_warning)
    return {
        "base": {"id": item.id, "name": item.name, "item_class": item.item_class},
        "ilvl": ilvl,
        "method": setup.label,
        "target_mods": [m.id for m in targets],
        "chance_per_attempt": None if p is None else round(p, 8),
        "expected_attempts": round(1 / p, 2) if p else None,
        "n90": attempts_for(p) if p is not None else None,
        "calculation": _calculation(estimate),
        "forced_mods": sorted(forced_ids),
        "weight_table": _table(pool, target_ids),
        "affix_limits": {"prefix": setup.model.max_prefix, "suffix": setup.model.max_suffix},
        "assumptions": [*GENERAL_ASSUMPTIONS, *setup.assumptions],
        "export_version": data.game_version,
        "warnings": warnings,
    }
