"""Exact maths for steps on magic items: Alteration+Augmentation cycles and Regal Orbs.

Both reuse the sequential-draw model in `draws`: an alt_aug cycle hits exactly when a 2-affix
Alteration would (the Augmentation draws like the Alteration's second affix), and a Regal is one
more draw on top of the existing mods.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from poe_craft_mcp.gamedata.draws import MAGIC, Candidate, CountModel, Start
from poe_craft_mcp.gamedata.models import AFFIX_TYPES, BaseItem, GameDataError, ModDef

ONE_AFFIX_SHARE = dict(MAGIC.counts)[1]
"""Share of Alterations that give a single affix (assumed 1/2, see MAGIC_COUNT)."""


def alt_aug_model(model: CountModel) -> CountModel:
    """Per cycle the item always ends with two affix draws (alt's second, or the aug)."""
    return replace(model, counts=((2, 1.0),))


def regal_model(model: CountModel, start: Start) -> CountModel:
    return replace(model, counts=((start.occupied + 1, 1.0),))


def augs_per_cycle(candidates: Sequence[Candidate]) -> float:
    """Augmentations used per cycle: the Alteration gave one affix, it is not a target, and a
    target can still roll on the open side (other side, no shared mod group)."""
    live = [c for c in candidates if c.weight > 0]
    total = sum(c.weight for c in live)
    targets = [c for c in live if c.is_target]
    useful = sum(
        c.weight
        for c in live
        if not c.is_target
        and any(t.is_prefix != c.is_prefix and not t.groups & c.groups for t in targets)
    )
    return ONE_AFFIX_SHARE * useful / total if total > 0 else 0.0


def check_existing(
    item: BaseItem, existing: Sequence[ModDef], magic: CountModel, rare: CountModel
) -> list[str]:
    """Validate a magic item's mods before a Regal; returns warnings."""
    if not existing:
        raise GameDataError(
            "method 'regal' needs existing_mods: the mod ids already on the magic item"
        )
    warnings: list[str] = []
    for mod in existing:
        if mod.generation_type not in AFFIX_TYPES:
            raise GameDataError(
                f"existing mod {mod.id} is a {mod.generation_type} mod, not an affix"
            )
        if mod.domain != item.domain:
            warnings.append(
                f"existing mod {mod.id} has domain {mod.domain!r}; {item.name} rolls "
                f"{item.domain!r} mods"
            )
    groups = [g for m in existing for g in m.groups]
    if len(groups) != len(set(groups)):
        raise GameDataError("existing_mods share a mod group, which one item cannot hold")
    for side, limit in (("prefix", magic.max_prefix), ("suffix", magic.max_suffix)):
        count = sum(m.generation_type == side for m in existing)
        if count > limit:
            raise GameDataError(
                f"a magic item holds at most {limit} {side}(es); existing_mods has {count}"
            )
    for side, limit in (("prefix", rare.max_prefix), ("suffix", rare.max_suffix)):
        if sum(m.generation_type == side for m in existing) > limit:
            raise GameDataError(f"existing_mods exceed {item.name}'s {side} limit of {limit}")
    return warnings
