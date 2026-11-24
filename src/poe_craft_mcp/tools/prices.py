"""`get_prices` and `get_base_price`: poe.ninja price lookups."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError, match_base, match_rows
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.envelope import envelope
from poe_craft_mcp.http.fetch import FetchError

MAX_ROWS = 500
BASE_HINT = "No matching poe.ninja BaseType row; price this base with a trade query instead."


def register(server: MCPServer, ctx: AppContext) -> None:
    ninja = get_ninja(ctx)
    read_only = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    @server.tool(name="get_prices", annotations=read_only)
    async def get_prices(category: str, names: list[str] | None = None) -> dict[str, Any]:
        """poe.ninja prices for one type (e.g. Currency, Essence, Fossil, Oil, BaseType, SkillGem).

        `names` filters by item name or id (case-insensitive); without it the most valuable
        rows are returned, at most 500. Rows: name, id, chaos, divine, volume_or_listings,
        sparkline_7d. Quote `data_age_seconds` when it is older than 30 minutes.
        """
        try:
            ds = await ninja.dataset(category)
        except (NinjaError, FetchError) as exc:
            raise ToolError(str(exc)) from exc
        not_found: list[str] = []
        if names:
            rows = []
            for name in names:
                hits = match_rows(ds.rows, name)
                rows.extend(hits)
                if not hits:
                    not_found.append(name)
        else:
            rows = sorted(ds.rows, key=lambda r: r["chaos"] or 0, reverse=True)
        total = len(rows)
        payload: dict[str, Any] = {
            "category": ds.type,
            "league": ds.league,
            "total_rows": total,
            "truncated": total > MAX_ROWS,
            "not_found": not_found,
            "rows": rows[:MAX_ROWS],
        }
        if not ds.rows:
            payload["hint"] = f"poe.ninja returned no rows for {ds.type} (empty or retired type)."
        return envelope(payload, [ds.source()])

    @server.tool(name="get_base_price", annotations=read_only)
    async def get_base_price(
        base: str, ilvl_min: int | None = None, influence: str | None = None
    ) -> dict[str, Any]:
        """Cheapest poe.ninja BaseType row for `base` (e.g. "Astral Plate"), or null.

        `ilvl_min`: minimum item level. `influence`: omit for a plain base, "any" for every
        variant, or e.g. "Shaper" / "Shaper/Hunter". A null `row` carries a hint to use a
        trade query instead.
        """
        try:
            ds = await ninja.dataset("BaseType")
        except (NinjaError, FetchError) as exc:
            raise ToolError(str(exc)) from exc
        row = match_base(ds.rows, base, ilvl_min, influence)
        payload: dict[str, Any] = {"league": ds.league, "row": row}
        if row is None:
            payload["hint"] = BASE_HINT
        return envelope(payload, [ds.source()])
