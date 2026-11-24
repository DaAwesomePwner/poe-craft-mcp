# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja, test_trade_pricing, test_evaluate)
"""`evaluate_recipe` and `rank_crafts` through the MCP server (no network).

Hand-checked on the fixtures (see test_evaluate): chaos per divine 195.
bow-flip:        C = C90 = 2.2 + 0.3 = 2.5, V = 4.75, profit 2.25, 1 min -> 135/hr.
unverified-bow:  C = 2.3, profit 2.45 -> 147/hr.
life-plate:      C = 3.6236, C90 = 4.2472, V = 4.375, profit 0.7514, 2 min -> 22.542/hr.
regalia-weights: C = 3 + 0.2118 / 0.3214..., profit ~1.0910, 2 min -> ~32.73/hr.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from test_evaluate import gamedata, store
from test_ninja import NinjaApi, api, ctx
from test_tools_prices import call, session
from test_trade import recorded
from test_trade_pricing import TRADE, mock_trade, trade

from poe_craft_mcp.clients.trade import TradeClient
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.recipes.loader import RecipeStore

if TYPE_CHECKING:
    from conftest import FakeClock

VERIFIED_ORDER = ["bow-flip", "regalia-weights", "life-plate"]


async def rank(ctx: AppContext, **args: Any) -> dict[str, Any]:
    async with session(ctx) as client:
        return await call(client, "rank_crafts", **args)


def ids(data: dict[str, Any]) -> list[str]:
    return [item["id"] for item in data["items"]]


async def test_both_tools_are_listed_read_only(ctx: AppContext, store: RecipeStore) -> None:
    async with session(ctx) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in ("evaluate_recipe", "rank_crafts"):
        annotations = tools[name].annotations
        assert annotations is not None and annotations.read_only_hint is True


async def test_evaluate_recipe_reports_numbers_with_sources(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    async with session(ctx) as client:
        data = await call(client, "evaluate_recipe", id="bow-flip")
    assert data["complete"] and data["evaluable"] and data["incomplete_reasons"] == []
    assert data["c"] == pytest.approx(2.5) and data["c90"] == pytest.approx(2.5)
    assert data["v"] == pytest.approx(4.75) and data["profit"] == pytest.approx(2.25)
    assert data["profit_per_hour"] == pytest.approx(135) and data["roi"] == pytest.approx(0.9)
    assert data["chaos_per_divine"] == 195.0
    assert data["divine"]["profit"] == pytest.approx(2.25 / 195)
    assert {i["name"]: i["status"] for i in data["inputs"]} == {
        "Imperial Bow": "ok",
        "Bench craft": "fixed",
        "crafted bow": "ok",
    }
    assert all("source" in i and "age_seconds" in i for i in data["inputs"])
    names = {s["name"] for s in data["sources"]}
    assert {"poe.ninja BaseType", "poe.ninja Currency", "pathofexile.com trade"} <= names
    assert data["breakdown"]["base"]["unit_chaos"] == 2.2


async def test_evaluate_recipe_overrides_and_errors(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    async with session(ctx) as client:
        data = await call(
            client, "evaluate_recipe", id="bow-flip", price_overrides={"crafted bow": 20}
        )
        unknown = await client.call_tool("evaluate_recipe", {"id": "nope"})
        negative = await client.call_tool(
            "evaluate_recipe", {"id": "bow-flip", "price_overrides": {"Bench craft": -1}}
        )
    assert data["v"] == pytest.approx(19.0)
    assert unknown.is_error and "known ids: bow-flip, deprecated-flip" in str(unknown.content)
    assert negative.is_error and "finite and >= 0" in str(negative.content)


async def test_evaluate_recipe_reports_blocking_odds(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    async with session(ctx) as client:
        data = await call(client, "evaluate_recipe", id="regalia-weights")
    assert data["evaluable"] is False and data["complete"] is False and data["c"] is None
    assert "mod odds unavailable" in data["incomplete_reasons"][0]
    assert data["odds"][0]["p_success"] is None


async def test_rank_hides_unverified_by_default(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, gamedata: None
) -> None:
    mock_trade(api)
    data = await rank(ctx, budget_div=1)
    assert ids(data) == VERIFIED_ORDER
    assert data["hidden_unverified"] == 1 and data["excluded"] == []
    first = data["items"][0]
    assert first["profit_per_hour"] == pytest.approx(135) and first["c90"] == pytest.approx(2.5)
    assert data["items"][2]["profit"] == pytest.approx(0.7514)
    assert "1 recipe hidden as unverified" in data["note"]
    with_unverified = await rank(ctx, budget_div=1, include_unverified=True)
    assert ids(with_unverified) == ["unverified-bow", *VERIFIED_ORDER]
    assert with_unverified["items"][0]["warnings"][0].startswith("Unverified")


async def test_rank_risk_levels_use_c90_c_or_nothing(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, gamedata: None
) -> None:
    mock_trade(api)
    budget = 4.0 / 195  # above every C (<= 3.66), below life-plate and regalia C90 (4.25, 4.27)
    low = await rank(ctx, budget_div=budget, risk="low")
    medium = await rank(ctx, budget_div=budget, risk="medium")
    high = await rank(ctx, budget_div=0, risk="high")
    assert ids(low) == ["bow-flip"]  # regalia C90 = 3 + 6 x 0.2118 = 4.27
    assert [e["id"] for e in low["excluded"]] == ["life-plate", "regalia-weights"]
    assert all("(risk=low)" in e["reason"] for e in low["excluded"])
    assert ids(medium) == VERIFIED_ORDER
    assert ids(high) == VERIFIED_ORDER
    assert ids(await rank(ctx, budget_div=budget, risk="medium")) == ids(medium)


async def test_rank_excludes_unresolved_odds_and_failed_prices(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    def search(request: httpx.Request) -> httpx.Response:
        if b"Imperial Bow" in request.content:  # life-plate's outcome query
            return httpx.Response(400, json={"error": {"code": 2, "message": "Invalid query"}})
        return recorded("search")

    api.router.post(f"{TRADE}/api/trade/search/Covens").mock(side_effect=search)
    api.router.get(url__startswith=f"{TRADE}/api/trade/fetch/").mock(return_value=recorded("fetch"))
    data = await rank(ctx, budget_div=1)
    assert ids(data) == ["bow-flip"]
    reasons = {e["id"]: e["reason"] for e in data["excluded"]}
    assert reasons["life-plate"].startswith("outcome 'life plate': price unavailable")
    assert "mod odds unavailable" in reasons["regalia-weights"]


async def test_rank_stops_trade_lookups_after_time_budget(
    ctx: AppContext,
    api: NinjaApi,
    trade: TradeClient,
    store: RecipeStore,
    gamedata: None,
    clock: FakeClock,
) -> None:
    fetches: list[httpx.Request] = []

    def slow_fetch(request: httpx.Request) -> httpx.Response:
        fetches.append(request)
        if len(fetches) == 3:  # the last of bow-flip's 3 fetches (sample 30, 10 per fetch)
            clock.advance(61)
        return recorded("fetch")

    api.router.post(f"{TRADE}/api/trade/search/Covens").mock(return_value=recorded("search"))
    api.router.get(url__startswith=f"{TRADE}/api/trade/fetch/").mock(side_effect=slow_fetch)
    data = await rank(ctx, budget_div=1)
    reasons = {e["id"]: e["reason"] for e in data["excluded"]}
    assert reasons["life-plate"].startswith("time budget")
    assert len(fetches) == 3
    assert ids(data) == ["bow-flip", "regalia-weights"]  # regalia reuses bow-flip's price


@pytest.mark.parametrize(
    ("args", "note"),
    [
        ({"tags": ["nope"]}, "no recipes match the given tags"),
        ({"max_minutes": 0.5}, "all 3 candidates were excluded; see `excluded` for each reason"),
    ],
)
async def test_empty_rank_explains_why(
    ctx: AppContext,
    api: NinjaApi,
    trade: TradeClient,
    store: RecipeStore,
    gamedata: None,
    args: dict[str, Any],
    note: str,
) -> None:
    mock_trade(api)
    data = await rank(ctx, budget_div=1, **args)
    assert data["items"] == [] and data["note"].startswith(note)


async def test_rank_with_no_recipes_loaded(ctx: AppContext, api: NinjaApi, tmp_path: Any) -> None:
    ctx.services["recipes"] = RecipeStore.load(tmp_path / "missing")
    data = await rank(ctx, budget_div=1)
    assert data["items"] == [] and data["note"].startswith("no recipes loaded")


async def test_rank_rejects_bad_arguments(ctx: AppContext, store: RecipeStore) -> None:
    async with session(ctx) as client:
        bad_risk = await client.call_tool("rank_crafts", {"budget_div": 1, "risk": "yolo"})
        bad_limit = await client.call_tool("rank_crafts", {"budget_div": 1, "limit": 0})
    assert bad_risk.is_error and "risk must be one of" in str(bad_risk.content)
    assert bad_limit.is_error and "limit must be at least 1" in str(bad_limit.content)
