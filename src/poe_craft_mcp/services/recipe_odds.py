"""Success chance of a repeated recipe step: a guide/user number or `mod_odds` from weights.

An unresolved or unsupported chance is reported as an error, never replaced by a guess.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {"step": self.step, "p_success": self.p, "source": self.source}
        if self.error:
            out["error"] = self.error
        if self.details:
            out.update(self.details)
        return out


async def resolve_chance(ctx: AppContext, recipe: Recipe, step: str, chance: Chance) -> StepChance:
    if isinstance(chance, GuideChance | UserChance):
        extra = {"url": chance.source_url} if isinstance(chance, GuideChance) else {}
        return StepChance(step, chance.value, source(f"{chance.p_source} chance", **extra))
    return await _weights(ctx, recipe, step, chance)


async def _weights(ctx: AppContext, recipe: Recipe, step: str, chance: WeightsChance) -> StepChance:
    lookup = recipe.base.lookup
    base = chance.base or (lookup.name if lookup else None)
    ilvl = chance.ilvl or (lookup.ilvl_min if lookup else None)
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
        return StepChance(step, None, origin, f"mod odds unresolved: {why}", details)
    if p < MIN_PROBABILITY:
        error = f"mod odds give a chance of {p:g}, below the supported 1e-12; target cannot roll"
        return StepChance(step, None, origin, error, details)
    return StepChance(step, p, origin, details=details)
