"""Map a knowledge-base recipe plus live prices and mod odds onto the pure profit engine.

Failure outcomes (`use_salvage` or `fixed_chaos`) are valued without undercut and need no
listings; the engine values every failure at one salvage amount, so when they differ the
probability-weighted average is used (with a warning). The recipe's `salvage` is only
priced when some outcome uses it.
"""

from __future__ import annotations

from dataclasses import dataclass

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.profit import evaluate
from poe_craft_mcp.engine.types import Breakdown, Craft, Material, Outcome, Step
from poe_craft_mcp.envelope import source
from poe_craft_mcp.recipes.models import Recipe
from poe_craft_mcp.services.recipe_inputs import SALVAGE, Priced, Pricer
from poe_craft_mcp.services.recipe_odds import StepChance, resolve_chance


@dataclass
class Evaluation:
    """Everything known about one recipe. `breakdown` is None when `problems` block it."""

    recipe: Recipe
    inputs: list[Priced]
    chances: list[StepChance]
    problems: list[str]
    warnings: list[str]
    breakdown: Breakdown | None

    @property
    def failed_inputs(self) -> list[Priced]:
        return [i for i in self.inputs if i.failed]

    def incomplete_reasons(self) -> list[str]:
        reasons = list(self.problems)
        if self.breakdown is not None and not self.breakdown.complete:
            reasons += [
                f"no price for {i.kind} '{i.name}': {i.error or 'unknown'}"
                for i in self.inputs
                if i.kind in ("base", "material") and i.chaos is None
            ]
        return reasons


def _material(priced: Priced, qty: float = 1.0) -> Material:
    return Material(
        priced.name, qty, priced.chaos, priced.source, priced.age_seconds, priced.volume
    )


def _salvage(failures: list[tuple[float, Priced]], warnings: list[str]) -> Material | None:
    if not failures:
        return None
    values = {p.chaos for _, p in failures}
    first = failures[0][1]
    if len(values) == 1:
        return Material(SALVAGE, 1.0, first.chaos, first.source, first.age_seconds, None)
    if None in values:
        warnings.append("no salvage price; failures using salvage are valued at 0")
    total = sum(prob for prob, _ in failures)
    value = sum(prob * (p.chaos or 0.0) for prob, p in failures) / total
    warnings.append(
        f"failure outcomes have different values; each is shown at their "
        f"probability-weighted average {value:g}c (the total is exact)"
    )
    parts = [{"name": p.name, "chaos": p.chaos, "probability": prob} for prob, p in failures]
    ages = [p.age_seconds for _, p in failures if p.age_seconds is not None]
    return Material(
        SALVAGE,
        1.0,
        value,
        source("probability-weighted failure value", parts=parts),
        max(ages) if ages else None,
    )


async def evaluate_recipe(
    ctx: AppContext, recipe: Recipe, pricer: Pricer, undercut: float
) -> Evaluation:
    """Price every input of `recipe` and run the profit engine on it.

    Raises only `TimeBudgetSpent` (from the pricer); every other failure is reported.
    """
    warnings = list(recipe.warnings())
    inputs: list[Priced] = []
    problems: list[str] = []
    chances: list[StepChance] = []

    base = await pricer.base(recipe.base)
    inputs.append(base)
    steps: list[Step] = []
    for number, step in enumerate(recipe.steps, 1):
        name = f"{number}. {step.action}"
        materials = []
        for m in step.materials:
            priced = await pricer.material(m)
            inputs.append(priced)
            materials.append(_material(priced, getattr(m, "qty", 1.0)))
        p = None
        if step.p_success is not None:
            chance = await resolve_chance(ctx, recipe, name, step.p_success)
            chances.append(chance)
            if chance.error:
                problems.append(f"step '{name}': {chance.error}")
            p = chance.p
        steps.append(Step(name, tuple(materials), p))

    outcomes: list[Outcome] = []
    failures: list[tuple[float, Priced]] = []
    salvage_price = None
    for o in recipe.outcomes:
        if o.trade_url is not None:
            priced = await pricer.trade("outcome", o.name, o.trade_url)
            inputs.append(priced)
            outcomes.append(
                Outcome(
                    o.name,
                    o.probability,
                    priced.chaos,
                    priced.source,
                    priced.age_seconds,
                    priced.listing_count,
                )
            )
            continue
        if o.use_salvage:
            if salvage_price is None:
                salvage_price = await pricer.salvage(recipe.salvage)
                inputs.append(salvage_price)
            priced = salvage_price
        else:
            priced = pricer.fixed("outcome", o.name, o.fixed_chaos or 0.0)
            inputs.append(priced)
        failures.append((o.probability, priced))
        outcomes.append(Outcome(o.name, o.probability, None, priced.source, is_failure=True))

    for name in pricer.unused_overrides():
        warnings.append(f"price override {name!r} matches no input of this recipe; ignored")
    for i in inputs:
        if i.failed:
            warnings.append(f"{i.kind} '{i.name}': {i.error}")
        elif i.source.get("stale"):
            warnings.append(f"{i.kind} '{i.name}' is priced from an old cached copy (stale)")
    salvage = _salvage(failures, warnings)

    breakdown = None
    if not problems:
        craft = Craft(
            recipe.id,
            recipe.name,
            recipe.minutes,
            tuple(steps),
            tuple(outcomes),
            _material(base),
            salvage,
        )
        try:
            breakdown = evaluate(craft, undercut)
        except ValueError as exc:
            problems.append(str(exc))
        else:
            warnings += breakdown.warnings
    return Evaluation(recipe, inputs, chances, problems, list(dict.fromkeys(warnings)), breakdown)
