"""Map a knowledge-base recipe plus live prices and mod odds onto the pure profit engine.

Sales are priced only by `trade_url` outcomes. `fixed_chaos` and `use_salvage` outcomes are
failure values: each is worth its own amount (no undercut, no listings needed), and a price
override on the outcome's name replaces it. A recipe without a sale outcome is not rankable.
A step repeats only with `repeat_until`; otherwise it is costed once and `p_success` ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.profit import evaluate
from poe_craft_mcp.engine.types import Breakdown, Craft, Material, Outcome, Step
from poe_craft_mcp.recipes.models import Outcome as RecipeOutcome
from poe_craft_mcp.recipes.models import Recipe
from poe_craft_mcp.recipes.models import Step as RecipeStep
from poe_craft_mcp.services.priced import Priced
from poe_craft_mcp.services.recipe_inputs import Pricer
from poe_craft_mcp.services.recipe_odds import StepChance, resolve_chance

NO_SALE = (
    "no priced sale outcome: every outcome is a failure value (fixed_chaos/use_salvage); "
    "add an outcome with a trade_url"
)


@dataclass
class Evaluation:
    """Everything known about one recipe. `breakdown` is None when `problems` block it;
    `unrankable` lists reasons the craft cannot be ranked even with a breakdown."""

    recipe: Recipe
    inputs: list[Priced]
    chances: list[StepChance]
    problems: list[str]
    unrankable: list[str]
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

    def rank_blockers(self) -> list[str]:
        """Why this craft cannot be ranked, before the engine's own filters run."""
        failed = [f"{i.kind} '{i.name}': {i.error}" for i in self.failed_inputs]
        return [*self.problems, *self.unrankable, *failed]


def _material(priced: Priced, qty: float = 1.0) -> Material:
    return Material(
        priced.name, qty, priced.chaos, priced.source, priced.age_seconds, priced.volume
    )


class _Builder:
    """Collects priced inputs, odds, problems and warnings while mapping one recipe."""

    def __init__(self, ctx: AppContext, recipe: Recipe, pricer: Pricer) -> None:
        self.ctx, self.recipe, self.pricer = ctx, recipe, pricer
        self.inputs: list[Priced] = []
        self.chances: list[StepChance] = []
        self.problems: list[str] = []
        self.warnings: list[str] = list(recipe.warnings())
        self.salvage: Priced | None = None

    def add(self, priced: Priced) -> Priced:
        self.inputs.append(priced)
        return priced

    async def step(self, number: int, step: RecipeStep, base_ilvl: int | None) -> Step:
        name = f"{number}. {step.action}"
        materials = []
        for m in step.materials:
            priced = self.add(await self.pricer.material(m))
            materials.append(_material(priced, getattr(m, "qty", 1.0)))
        if step.p_success is None:
            return Step(name, tuple(materials))
        if step.repeat_until is None:
            self.warnings.append(f"step '{name}': p_success ignored: step is not repeated")
            return Step(name, tuple(materials))
        chance = await resolve_chance(self.ctx, self.recipe, name, step.p_success, base_ilvl)
        self.chances.append(chance)
        self.warnings += chance.warnings
        if chance.error:
            self.problems.append(f"step '{name}': {chance.error}")
        return Step(name, tuple(materials), chance.p)

    async def outcome(self, o: RecipeOutcome) -> Outcome:
        if o.trade_url is not None:
            p = self.add(await self.pricer.trade("outcome", o.name, o.trade_url))
            return Outcome(o.name, o.probability, p.chaos, p.source, p.age_seconds, p.listing_count)
        own = self.pricer.override("outcome", o.name)
        if own is None and o.use_salvage:  # valued at the craft's salvage by the engine
            if self.salvage is None:
                self.salvage = self.add(await self.pricer.salvage(self.recipe.salvage))
            return Outcome(o.name, o.probability, None, self.salvage.source, is_failure=True)
        priced = self.add(own or self.pricer.fixed("outcome", o.name, o.fixed_chaos or 0.0))
        return Outcome(
            o.name, o.probability, None, priced.source, is_failure=True, failure_value=priced.chaos
        )


async def evaluate_recipe(
    ctx: AppContext, recipe: Recipe, pricer: Pricer, undercut: float
) -> Evaluation:
    """Price every input of `recipe` and run the profit engine on it.

    Raises only `TimeBudgetSpent` (from the pricer); every other failure is reported.
    """
    b = _Builder(ctx, recipe, pricer)
    base = b.add(await pricer.base(recipe.base))
    steps = [await b.step(n, s, base.ilvl) for n, s in enumerate(recipe.steps, 1)]
    outcomes = [await b.outcome(o) for o in recipe.outcomes]
    unrankable = [] if any(not o.is_failure for o in outcomes) else [NO_SALE]

    for name in pricer.unused_overrides():
        b.warnings.append(f"price override {name!r} matches no input of this recipe; ignored")
    b.warnings += unrankable
    for i in b.inputs:
        if i.failed:
            b.warnings.append(f"{i.kind} '{i.name}': {i.error}")
        elif i.stale:
            b.warnings.append(f"{i.kind} '{i.name}' is priced from an old cached copy (stale)")

    breakdown = None
    if not b.problems:
        salvage = _material(b.salvage) if b.salvage else None
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
            b.problems.append(str(exc))
        else:
            b.warnings += breakdown.warnings
    return Evaluation(
        recipe,
        b.inputs,
        b.chances,
        b.problems,
        unrankable,
        list(dict.fromkeys(b.warnings)),
        breakdown,
    )
