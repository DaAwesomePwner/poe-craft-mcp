"""`list_recipes` and `get_recipe`: browse the curated recipe knowledge base."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope, source
from poe_craft_mcp.recipes.loader import RecipeStore
from poe_craft_mcp.recipes.models import Recipe

SERVICE_NAME = "recipes"


def get_recipe_store(ctx: AppContext) -> RecipeStore:
    """The shared recipe store, loaded from disk on first use."""
    return ctx.service(SERVICE_NAME, lambda _: RecipeStore.load())


def summarize(recipe: Recipe, *, budget_filtered: bool = False) -> dict[str, Any]:
    warnings = recipe.warnings()
    if budget_filtered and recipe.budget_div is None:
        warnings.append("budget_div is not set, so the budget filter could not be applied.")
    return {
        "id": recipe.id,
        "name": recipe.name,
        "tags": recipe.tags,
        "status": recipe.status,
        "verified_patch": recipe.verified_patch,
        "budget_div": recipe.budget_div,
        "requires": recipe.requires,
        "warnings": warnings,
    }


def _empty_reason(store: RecipeStore, hidden_unverified: int) -> str:
    if not store.recipes:
        skipped = f" ({len(store.errors)} files skipped as invalid)" if store.errors else ""
        return f"no valid recipes loaded from {store.directory}{skipped}"
    if hidden_unverified:
        plural = "s" if hidden_unverified != 1 else ""
        return (
            f"{hidden_unverified} recipe{plural} hidden as unverified; "
            "pass include_unverified=true to see them"
        )
    return "no recipes match the given filters"


def register(server: MCPServer, ctx: AppContext) -> None:
    read_only = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    @server.tool(annotations=read_only)
    async def list_recipes(
        max_budget_div: float | None = None,
        tags: list[str] | None = None,
        include_unverified: bool | None = None,
    ) -> dict[str, Any]:
        """List crafts from the recipe knowledge base.

        max_budget_div keeps recipes whose rough per-attempt budget (divines) fits; recipes
        with no budget estimate are kept with a warning. tags keeps recipes that have any
        of the given tags. Unverified recipes (not verified for patch 3.30+) are hidden
        unless include_unverified is true (default comes from config); deprecated recipes
        are never listed. Recipes shown unverified carry warnings: pass them on to the user.
        """
        store = get_recipe_store(ctx)
        show = ctx.config.include_unverified if include_unverified is None else include_unverified
        listing = store.list(max_budget_div=max_budget_div, tags=tags, include_unverified=show)
        budget_filtered = max_budget_div is not None
        payload: dict[str, Any] = {
            "recipes": [summarize(r, budget_filtered=budget_filtered) for r in listing.recipes],
            "hidden_unverified": listing.hidden_unverified,
        }
        if not listing.recipes:
            payload["reason"] = _empty_reason(store, listing.hidden_unverified)
        if store.errors:
            payload["load_errors"] = [{"file": e.file, "reason": e.reason} for e in store.errors]
        return envelope(payload, sources=[source("recipe knowledge base")])

    @server.tool(annotations=read_only)
    async def get_recipe(id: str) -> dict[str, Any]:
        """Return one full recipe (steps, materials, odds, outcomes, salvage) by id, with
        warnings. Works for unverified and deprecated recipes too; always relay the warnings."""
        store = get_recipe_store(ctx)
        recipe = store.get(id)
        if recipe is None:
            known = ", ".join(store.ids()) or "none loaded"
            raise ToolError(f"unknown recipe id {id!r}; known ids: {known}")
        payload = {"recipe": recipe.model_dump(mode="json"), "warnings": recipe.warnings()}
        return envelope(payload, sources=[source("recipe knowledge base")])
