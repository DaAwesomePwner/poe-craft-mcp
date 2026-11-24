"""`evaluate_recipe` and `rank_crafts`: expected profit of knowledge-base crafts."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError, divine_chaos
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope
from poe_craft_mcp.http.fetch import FetchError
from poe_craft_mcp.services.craft_report import collect_sources, full_report
from poe_craft_mcp.services.evaluate import evaluate_recipe as evaluate
from poe_craft_mcp.services.ranking import RankRequest, rank_recipes
from poe_craft_mcp.services.recipe_inputs import Pricer, check_overrides
from poe_craft_mcp.tools.recipes import get_recipe_store


def register(server: MCPServer, ctx: AppContext) -> None:
    annotations = ToolAnnotations(read_only_hint=True, open_world_hint=True)

    @server.tool(name="evaluate_recipe", annotations=annotations)
    async def evaluate_recipe(
        id: str, price_overrides: dict[str, float] | None = None
    ) -> dict[str, Any]:
        """Expected profit of one knowledge-base recipe, with every input price and its source.

        Prices come from poe.ninja (base, materials) and the trade site (p25 of the cheapest
        instant-buyout listings, for outcomes; whisper listings are never used); step odds
        from the recipe or `mod_odds`. `price_overrides` maps an input name to a chaos price
        that replaces the fetched one (source "override"): a material name/id or fixed
        label, an outcome name (sale or failure), the base name ("base" for a trade-URL
        base), or "salvage". Returns C (expected cost), C90 (bad-run cost), V (value),
        profit, profit_per_hour and ROI in chaos plus divine equivalents, the engine
        breakdown, `inputs` (each with source and age), `odds`, `complete` with
        `incomplete_reasons`, `rankable` with its reasons (a craft needs a trade-priced sale
        outcome), and warnings. Relay the warnings, and quote ages older than 30 minutes.
        """
        store = get_recipe_store(ctx)
        recipe = store.get(id)
        if recipe is None:
            known = ", ".join(store.ids()) or "none loaded"
            raise ToolError(f"unknown recipe id {id!r}; known ids: {known}")
        try:
            overrides = check_overrides(price_overrides)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        pricer = Pricer(ctx, overrides)
        ev = await evaluate(ctx, recipe, pricer, ctx.config.undercut_pct / 100)
        extra: list[dict[str, Any]] = []
        per_divine: float | None = None
        try:
            currency = await get_ninja(ctx).dataset("Currency")
            per_divine = divine_chaos(currency.rows)
            extra = currency.sources()
        except (FetchError, NinjaError) as exc:
            ev.warnings.append(f"divine equivalents unavailable: {exc}")
        return envelope(full_report(ev, per_divine), collect_sources([ev], *extra))

    @server.tool(name="rank_crafts", annotations=annotations)
    async def rank_crafts(
        budget_div: float,
        min_listings: int | None = None,
        max_minutes: float | None = None,
        risk: str = "medium",
        limit: int = 5,
        include_unverified: bool | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Knowledge-base crafts ranked by expected profit per hour (chaos, divines alongside).

        `budget_div`: what you can spend per craft, in divines. `risk`: low keeps crafts whose
        bad-run cost C90 fits the budget, medium (default) those whose expected cost C fits,
        high ignores the budget. `min_listings` (default from config): outcomes with fewer
        instant-buyout trade listings are dropped as illiquid. `max_minutes`: per attempt.
        `limit`: items to return. Unverified recipes are left out unless
        `include_unverified` is true; `tags` keeps recipes with any of the tags. Returns
        `items` (profit, profit_per_hour, ROI, C, C90, V, flags, warnings, inputs with
        sources), `excluded` [{id, reason}] and a `note` explaining an empty result. Never
        suggest crafts that are not in this list.
        """
        req = RankRequest(
            budget_div=budget_div,
            min_listings=ctx.config.min_listings if min_listings is None else min_listings,
            max_minutes=max_minutes,
            risk=risk,
            limit=limit,
            include_unverified=(
                ctx.config.include_unverified if include_unverified is None else include_unverified
            ),
            tags=tags,
        )
        try:
            payload, sources = await rank_recipes(ctx, get_recipe_store(ctx), req)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return envelope(payload, sources)
