"""Evaluate knowledge-base recipes one after another and rank them with `engine.rank`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError, divine_chaos
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.rank import RISKS, rank
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.http.fetch import FetchError
from poe_craft_mcp.recipes.loader import RecipeStore
from poe_craft_mcp.services.craft_report import collect_sources, full_report
from poe_craft_mcp.services.evaluate import Evaluation, evaluate_recipe
from poe_craft_mcp.services.recipe_inputs import Pricer, TimeBudgetSpent

TIME_BUDGET_S = 60.0


@dataclass(frozen=True)
class RankRequest:
    budget_div: float
    min_listings: int
    max_minutes: float | None
    risk: str
    limit: int
    include_unverified: bool
    tags: list[str] | None


def check_request(req: RankRequest) -> None:
    """Reject bad arguments before any lookup is made; raises ValueError."""
    if req.risk not in RISKS:
        raise ValueError(f"risk must be one of {', '.join(RISKS)}, got {req.risk!r}")
    if req.limit < 1:
        raise ValueError("limit must be at least 1")
    if not req.budget_div >= 0:
        raise ValueError("budget_div must be >= 0")
    if req.min_listings < 0:
        raise ValueError("min_listings must be >= 0")
    if req.max_minutes is not None and not req.max_minutes > 0:
        raise ValueError("max_minutes must be positive")


def _item(ev: Evaluation, flags: tuple[str, ...], per_divine: float) -> dict[str, Any]:
    return {**full_report(ev, per_divine), "flags": list(flags)}


def _note(store: RecipeStore, candidates: int, hidden: int, items: int, note: str | None) -> str:
    if not store.recipes:
        return f"no recipes loaded: {store.problem or store.directory}"
    parts = []
    if not candidates:
        parts.append("no recipes match the given tags")
    elif not items:
        parts.append(note or f"all {candidates} candidates were excluded; see `excluded`")
    if hidden:
        plural = "s" if hidden != 1 else ""
        parts.append(
            f"{hidden} recipe{plural} hidden as unverified; "
            "pass include_unverified=true to rank them"
        )
    return "; ".join(parts)


async def rank_recipes(
    ctx: AppContext, store: RecipeStore, req: RankRequest
) -> tuple[dict[str, Any], list[Source]]:
    """Rank crafts by profit per hour; returns (payload, envelope sources). Raises ValueError
    for bad arguments or when the divine rate (needed for the budget) is unavailable."""
    check_request(req)
    try:
        currency = await get_ninja(ctx).dataset("Currency")
        per_divine = divine_chaos(currency.rows)
    except (FetchError, NinjaError) as exc:
        raise ValueError(f"cannot convert the budget: no poe.ninja divine rate: {exc}") from None
    listing = store.list(tags=req.tags, include_unverified=req.include_unverified)
    clock = ctx.cache.clock
    pricer = Pricer(ctx, deadline=clock() + TIME_BUDGET_S, clock=clock)
    undercut = ctx.config.undercut_pct / 100
    excluded: list[dict[str, str]] = []
    evaluated: dict[str, Evaluation] = {}
    seen: list[Evaluation] = []
    for recipe in sorted(listing.recipes, key=lambda r: r.id):
        try:
            ev = await evaluate_recipe(ctx, recipe, pricer, undercut)
        except TimeBudgetSpent:
            reason = f"time budget: trade lookups stopped after {TIME_BUDGET_S:g} s; retry later"
            excluded.append({"id": recipe.id, "reason": reason})
            continue
        seen.append(ev)
        blockers = ev.rank_blockers()
        if blockers:
            excluded.append({"id": recipe.id, "reason": "; ".join(blockers)})
        else:
            evaluated[recipe.id] = ev
    result = rank(
        [ev.breakdown for ev in evaluated.values() if ev.breakdown],
        req.budget_div,
        per_divine,
        req.min_listings,
        req.max_minutes,
        req.risk,
        req.limit,
    )
    excluded += [{"id": e.id, "reason": e.reason} for e in result.excluded]
    items = [_item(evaluated[r.breakdown.id], r.flags, per_divine) for r in result.items]
    payload: dict[str, Any] = {
        "budget_div": req.budget_div,
        "budget_chaos": req.budget_div * per_divine,
        "chaos_per_divine": per_divine,
        "undercut": undercut,
        "risk": req.risk,
        "min_listings": req.min_listings,
        "items": items,
        "excluded": sorted(excluded, key=lambda e: e["id"]),
        "omitted_by_limit": result.omitted_by_limit,
        "hidden_unverified": listing.hidden_unverified,
    }
    note = _note(store, len(listing.recipes), listing.hidden_unverified, len(items), result.note)
    if note:
        payload["note"] = note
    return payload, collect_sources(seen, *currency.sources())
