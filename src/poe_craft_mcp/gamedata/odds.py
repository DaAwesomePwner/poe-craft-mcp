"""`mod_odds`: single-step chance of hitting a target mod from spawn weights."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from poe_craft_mcp.gamedata import magic
from poe_craft_mcp.gamedata.cluster import apply_enchant
from poe_craft_mcp.gamedata.draws import MAGIC, Candidate, Estimate, Start, hit_probability
from poe_craft_mcp.gamedata.lookup import resolve_base, resolve_mods
from poe_craft_mcp.gamedata.method import CraftSetup, parse_method
from poe_craft_mcp.gamedata.models import BaseItem, GameData, GameDataError, ModDef
from poe_craft_mcp.gamedata.pool import PoolEntry, build_pool, entry_for
from poe_craft_mcp.gamedata.report import (
    GENERAL_ASSUMPTIONS,
    attempts_for,
    calculation,
    expected_materials,
    weight_table,
)

__all__ = ["GENERAL_ASSUMPTIONS", "attempts_for", "mod_odds"]


def _start(setup: CraftSetup, existing: Sequence[ModDef]) -> Start:
    placed = (*setup.forced, *existing)
    start = Start(
        groups=frozenset(g for m in placed for g in m.groups),
        prefixes=sum(m.generation_type == "prefix" for m in placed),
        suffixes=sum(m.generation_type == "suffix" for m in placed),
    )
    if start.prefixes > setup.model.max_prefix or start.suffixes > setup.model.max_suffix:
        raise GameDataError(f"forced mod(s) of {setup.label} exceed this base's affix limits")
    return start


def _unrollable(
    targets: Sequence[ModDef],
    pool: Sequence[PoolEntry],
    placed: Sequence[ModDef],
    item: BaseItem,
    ilvl: tuple[int, int],
    setup: CraftSetup,
    full_sides: set[str],
) -> list[str]:
    """Why each target that is not in the pool cannot roll."""
    roll_ilvl, ilvl_in = ilvl
    holder = "existing mod(s)" if setup.kind == "regal" else "forced mod(s)"
    pooled = {e.mod.id for e in pool}
    placed_ids = {m.id for m in placed}
    added = frozenset(m for f in setup.fossils for m in f.added_mods)
    out = []
    for mod in targets:
        if mod.id in placed_ids or mod.id in pooled:
            continue
        reason = entry_for(mod, item, roll_ilvl, setup.fossils, added)
        if isinstance(reason, PoolEntry) and reason.side in full_sides:
            side = reason.side
            reason = f"{mod.id} is a {side} and the {holder} fill every {side} slot"
        elif isinstance(reason, PoolEntry):
            reason = f"{mod.id} shares a mod group with the {holder}, so it cannot roll"
        elif roll_ilvl < ilvl_in:
            reason += f"; {setup.label} caps item level at {roll_ilvl} for the other mods"
        out.append(f"target cannot roll: {reason}")
    return out


def mod_odds(
    data: GameData,
    base: str,
    ilvl: int,
    target_mods: Sequence[str],
    method: str,
    *,
    enchant: str | None = None,
    passives: int | None = None,
    existing_mods: Sequence[str] | None = None,
    force_monte_carlo: bool = False,
) -> dict[str, Any]:
    """Chance per attempt of hitting any of `target_mods` on `base` at `ilvl` with `method`.

    `enchant` (and optionally `passives`) is a cluster jewel's enchant; `existing_mods` are the
    mods on the magic item a 'regal' upgrades. `chance_per_attempt` is None when sampling saw no
    hit; `calculation.upper_bound_95` then bounds it."""
    if not 1 <= ilvl <= 100:
        raise GameDataError(f"ilvl must be between 1 and 100 (got {ilvl})")
    if not target_mods:
        raise GameDataError("target_mods must list at least one mod id, e.g. ['IncreasedLife7']")
    item, warnings = resolve_base(data, base)
    enchanted = apply_enchant(data, item, enchant, passives)
    item = enchanted.item
    warnings += enchanted.warnings
    targets = resolve_mods(data, dict.fromkeys(target_mods))
    setup = parse_method(data, item, method)
    warnings += list(setup.warnings)
    existing = resolve_mods(data, dict.fromkeys(existing_mods or ()))
    if setup.kind == "regal":
        warnings += magic.check_existing(item, existing, MAGIC, setup.model)
        item = replace(item, tags=item.tags | {t for m in existing for t in m.adds_tags})
        already = [m.id for m in targets if m.id in {e.id for e in existing}]
        if already:
            raise GameDataError(f"target(s) already among existing_mods: {', '.join(already)}")
    elif existing:
        warnings.append("existing_mods ignored: only the 'regal' method uses them")
        existing = []
    start = _start(setup, existing)
    model = setup.model
    if setup.label == "alt_aug":
        model = magic.alt_aug_model(model)
    elif setup.kind == "regal":
        model = magic.regal_model(model, start)
    roll_ilvl = min(ilvl, setup.ilvl_cap) if setup.ilvl_cap is not None else ilvl
    full_sides = {
        side
        for side, used, cap in (
            ("prefix", start.prefixes, model.max_prefix),
            ("suffix", start.suffixes, model.max_suffix),
        )
        if used >= cap
    }
    pool = [
        e
        for e in build_pool(data, item, roll_ilvl, setup.fossils, blocked_groups=start.groups)
        if e.side not in full_sides
    ]
    placed = (*setup.forced, *existing)
    warnings += _unrollable(targets, pool, placed, item, (roll_ilvl, ilvl), setup, full_sides)

    target_ids = {m.id for m in targets}
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
    if target_ids & {m.id for m in setup.forced}:
        estimate = Estimate(1.0, "forced")
    else:
        estimate = hit_probability(candidates, model, start, force_monte_carlo)
        if setup.kind == "regal" or setup.label == "alt_aug":
            estimate = replace(estimate, detail={})

    p: float | None = estimate.probability if estimate.resolved else None
    if not estimate.resolved:
        warnings.append(
            f"no hit in {estimate.samples} Monte Carlo samples; the chance is unresolved and "
            f"below {estimate.upper_bound_95:.2e} per attempt (95% one-sided bound)"
        )
    if data.stale_warning:
        warnings.insert(0, data.stale_warning)
    augs = magic.augs_per_cycle(candidates) if setup.label == "alt_aug" else 0.0
    return {
        "base": {"id": item.id, "name": item.name, "item_class": item.item_class},
        "ilvl": ilvl,
        "method": setup.label,
        "enchant": enchanted.report,
        "target_mods": [m.id for m in targets],
        "existing_mods": [m.id for m in existing],
        "chance_per_attempt": None if p is None else round(p, 8),
        "expected_attempts": round(1 / p, 2) if p else None,
        "n90": attempts_for(p) if p is not None else None,
        "expected_materials": expected_materials(setup.kind, setup.label, p, augs),
        "calculation": calculation(estimate),
        "forced_mods": sorted(m.id for m in setup.forced),
        "weight_table": weight_table(pool, target_ids),
        "affix_limits": {"prefix": model.max_prefix, "suffix": model.max_suffix},
        "assumptions": [*GENERAL_ASSUMPTIONS, *setup.assumptions, *enchanted.assumptions],
        "export_version": data.game_version,
        "warnings": warnings,
    }
