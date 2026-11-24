"""Success chance of a repeated recipe step: a guide/user number or `mod_odds` from weights.

An unresolved or unsupported chance is reported as an error, never replaced by a guess.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.profit import MIN_PROBABILITY
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.envelope import source
from poe_craft_mcp.gamedata.loader import get_gamedata
from poe_craft_mcp.gamedata.models import GameDataError
from poe_craft_mcp.gamedata.odds import mod_odds
from poe_craft_mcp.recipes.models import Chance, GuideChance, Recipe, UserChance, WeightsChance
from poe_craft_mcp.tools.odds import export_source


@dataclass
class StepChance:
    """`p` is the per-attempt success chance, or None with an `error`."""

    step: str
    p: float | None
    source: Source
    error: str | None = None
    details: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {"step": self.step, "p_success": self.p, "source": self.source}
        if self.error:
            out["error"] = self.error
        if self.details:
            out.update(self.details)
        return out


async def resolve_chance(
    ctx: AppContext, recipe: Recipe, step: str, chance: Chance, base_ilvl: int | None = None
) -> StepChance:
    """`base_ilvl` is the item level of the base actually selected (poe.ninja row), used for
    weights odds on the recipe's own base."""
    if isinstance(chance, GuideChance | UserChance):
        extra = {"url": chance.source_url} if isinstance(chance, GuideChance) else {}
        return StepChance(step, chance.value, source(f"{chance.p_source} chance", **extra))
    return await _weights(ctx, recipe, step, chance, base_ilvl)


def _item_level(
    recipe: Recipe, chance: WeightsChance, base: str | None, base_ilvl: int | None
) -> tuple[int | None, list[str]]:
    """The selected base's real item level when known, else the recipe's (with a warning)."""
    lookup = recipe.base.lookup
    own_base = lookup is not None and (base or "").lower() == lookup.name.lower()
    if own_base and base_ilvl is not None:
        return base_ilvl, []
    ilvl = chance.ilvl or (lookup.ilvl_min if lookup else None)
    if ilvl is None:
        return None, []
    return ilvl, [
        f"odds for {base} use the recipe's item level {ilvl}; the actual base's item level "
        "is unknown and may roll different mods"
    ]


async def _weights(
    ctx: AppContext, recipe: Recipe, step: str, chance: WeightsChance, base_ilvl: int | None
) -> StepChance:
    lookup = recipe.base.lookup
    base = chance.base or (lookup.name if lookup else None)
    ilvl, notes = _item_level(recipe, chance, base, base_ilvl)
    unresolved = source("mod_odds")
    if base is None or ilvl is None:
        missing = "base" if base is None else "ilvl"
        error = f"weights odds need a {missing}: set p_success.{missing} in the recipe"
        return StepChance(step, None, unresolved, error)
    try:
        data = await asyncio.to_thread(get_gamedata, ctx)
        result = await asyncio.to_thread(
            mod_odds, data, base, ilvl, chance.target_mods, chance.method
        )
    except GameDataError as exc:
        return StepChance(step, None, unresolved, f"mod odds unavailable: {exc}")
    details = {
        "base": base,
        "ilvl": ilvl,
        "method": result["method"],
        "target_mods": result["target_mods"],
        "export_version": result["export_version"],
        "assumptions": result["assumptions"],
        "odds_warnings": result["warnings"],
    }
    p = result["chance_per_attempt"]
    origin = export_source(data)
    if p is None:
        why = result["warnings"][-1] if result["warnings"] else "no estimate"
        return StepChance(step, None, origin, f"mod odds unresolved: {why}", details, notes)
    if p < MIN_PROBABILITY:
        error = f"mod odds give a chance of {p:g}, below the supported 1e-12; target cannot roll"
        return StepChance(step, None, origin, error, details, notes)
    return StepChance(step, p, origin, details=details, warnings=notes)
