# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja, test_trade_pricing and others)
from __future__ import annotations

import httpx
from test_cluster_combos import T_ES, T_WP, FakeTrade, data, fake, scenario
from test_ninja import NinjaApi, api, ctx
from test_tools_prices import call, session
from test_trade_pricing import trade

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.gamedata.models import GameData

ARGS = {
    "base": "Medium Cluster Jewel",
    "enchant": "Chaos Damage over Time",
    "passives": 4,
    "ilvl": 84,
}


async def test_cluster_combos_is_listed_read_only_open_world(ctx: AppContext) -> None:
    async with session(ctx) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    tool = tools["cluster_combos"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is True
    assert "sellable" in (tool.description or "").lower()
    assert {"base", "enchant", "passives", "ilvl"} <= set(tool.input_schema["required"])


async def test_cluster_combos_envelope(ctx: AppContext, fake: FakeTrade, data: GameData) -> None:
    ctx.services["gamedata"] = data
    scenario(fake)
    async with session(ctx) as client:
        out = await call(client, "cluster_combos", **ARGS)
    assert [r["trade_ids"] for r in out["sellable_pairs"]][0] == [T_ES, T_WP]
    assert out["economics"]["profit_per_attempt"] is not None
    names = [s["name"] for s in out["sources"]]
    assert names[0].startswith("RePoE export")
    assert "poe.ninja Currency" in names and names.count("pathofexile.com trade") == 7
    assert out["data_age_seconds"] is not None
    assert out["assumptions"] and isinstance(out["warnings"], list)


async def test_cluster_combos_small_cluster(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    ctx.services["gamedata"] = data
    async with session(ctx) as client:
        small = {"base": "Small Cluster Jewel", "enchant": "Life", "passives": 2}
        out = await call(client, "cluster_combos", **{**ARGS, **small})
    assert out["legal_pairs"] == 0 and "no pairs" in out["note"]
    assert [s["name"] for s in out["sources"]][0].startswith("RePoE export")


async def test_cluster_combos_errors_are_tool_errors(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    ctx.services["gamedata"] = data
    async with session(ctx) as client:
        bad = await client.call_tool("cluster_combos", {**ARGS, "min_listings": 0})
        wrong = await client.call_tool("cluster_combos", {**ARGS, "passives": 9})
        fake.fail["below"] = httpx.Response(429, headers={"Retry-After": "30"})
        trade_down = await client.call_tool("cluster_combos", ARGS)
    assert bad.is_error and "min_listings" in str(bad.content)
    assert wrong.is_error and "adds 4-6 passive skills" in str(wrong.content)
    assert trade_down.is_error
    text = str(trade_down.content)
    assert "Price unavailable" in text and "Try again in about" in text
    assert "Traceback" not in text
