# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja and test_trade_pricing)
"""Recipe -> engine mapping, checked by hand on the fixtures.

Prices used (tests/fixtures/ninja, tests/fixtures/trade): Astral Plate ilvl >= 85 3.0c,
Imperial Bow ilvl >= 85 2.2c, Orb of Alteration 0.2118c, every trade outcome p25 5.0c with
30 listings (sample 30). Undercut 5%, so a 5.0c outcome is worth 4.75c.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from test_ninja import NinjaApi, api, ctx
from test_trade_pricing import mock_trade, trade

from poe_craft_mcp.clients.trade import TradeClient
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.gamedata.loader import load_gamedata
from poe_craft_mcp.recipes.loader import RecipeStore
from poe_craft_mcp.services import recipe_odds
from poe_craft_mcp.services.evaluate import Evaluation, evaluate_recipe
from poe_craft_mcp.services.recipe_inputs import Pricer, check_overrides

if TYPE_CHECKING:
    from conftest import FakeClock

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CRAFTS = FIXTURES / "crafts"
ALT_P = 0.5 * (1000 / 7000) + 0.5 * 0.5  # FireResist1 on Vaal Regalia ilvl 5 (test_odds)


@pytest.fixture
def store(ctx: AppContext) -> RecipeStore:
    loaded = RecipeStore.load(CRAFTS)
    ctx.services["recipes"] = loaded
    return loaded


@pytest.fixture
def gamedata(ctx: AppContext) -> None:
    ctx.services["gamedata"] = load_gamedata(FIXTURES / "gamedata")


async def run(
    ctx: AppContext, store: RecipeStore, recipe_id: str, overrides: dict[str, Any] | None = None
) -> Evaluation:
    recipe = store.get(recipe_id)
    assert recipe is not None
    return await evaluate_recipe(ctx, recipe, Pricer(ctx, check_overrides(overrides)), 0.05)


def by_name(ev: Evaluation) -> dict[str, Any]:
    return {i.name: i for i in ev.inputs}


async def test_full_recipe_matches_hand_calculation(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    ev = await run(ctx, store, "life-plate")
    b = ev.breakdown
    assert b is not None and b.complete and not ev.problems
    per_attempt = 0.2118 + 0.1  # alteration + fixed augmentation; p = 0.5 -> 2 attempts, n90 4
    assert b.c == pytest.approx(3.0 + 2 * per_attempt)
    assert b.c90 == pytest.approx(3.0 + 4 * per_attempt)
    assert b.v == pytest.approx(0.9 * 4.75 + 0.1 * 1.0)  # failure valued at salvage 1c
    assert b.profit == pytest.approx(4.375 - 3.6236)
    assert b.profit_per_hour == pytest.approx((4.375 - 3.6236) * 30)
    assert b.roi == pytest.approx(0.7514 / 3.6236)
    inputs = by_name(ev)
    assert inputs["Astral Plate"].source["type"] == "BaseType"
    assert inputs["Astral Plate"].source["id"] == "astral-plate-85"
    assert inputs["Orb of Alteration"].source["id"] == "alt"
    assert inputs["Orb of Alteration"].volume == 157730
    assert inputs["Augmentation"].status == "fixed"
    hit = inputs["life plate"]
    assert (hit.chaos, hit.listing_count) == (5.0, 30)
    assert hit.source["name"] == "pathofexile.com trade" and hit.source["search_id"]
    assert [o.is_failure for o in b.outcomes] == [False, True]


async def test_overrides_replace_prices_and_skip_lookups(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    search, _ = mock_trade(api)
    overrides = {"Orb of Alteration": 1.0, "life plate": 10, "salvage": 2, "nope": 3}
    ev = await run(ctx, store, "life-plate", overrides)
    b = ev.breakdown
    assert b is not None and search.call_count == 0
    assert b.c == pytest.approx(3.0 + 2 * 1.1)
    assert b.v == pytest.approx(0.9 * 9.5 + 0.1 * 2)
    inputs = by_name(ev)
    for name in ("Orb of Alteration", "life plate", "salvage"):
        assert inputs[name].status == "override" and inputs[name].source["name"] == "override"
    assert "price override 'nope' matches no input of this recipe; ignored" in ev.warnings


@pytest.mark.parametrize("bad", [-1, math.inf, math.nan, True, "5"])
def test_overrides_must_be_finite_non_negative_numbers(bad: object) -> None:
    with pytest.raises(ValueError, match="price override for 'x'"):
        check_overrides({"x": bad})  # type: ignore[dict-item]


async def test_weights_chance_comes_from_mod_odds(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, gamedata: None
) -> None:
    mock_trade(api)
    ev = await run(ctx, store, "regalia-weights")
    b = ev.breakdown
    assert b is not None and b.complete
    assert b.steps[0].p_success == pytest.approx(ALT_P, abs=1e-8)
    assert b.c == pytest.approx(3.0 + 0.2118 / ALT_P, rel=1e-7)
    odds = ev.chances[0].report()
    assert odds["base"] == "Vaal Regalia" and odds["ilvl"] == 5
    assert odds["source"]["name"].startswith("RePoE export")


async def test_unresolved_odds_block_the_craft(
    ctx: AppContext,
    api: NinjaApi,
    trade: TradeClient,
    store: RecipeStore,
    gamedata: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_trade(api)
    unresolved = {
        "chance_per_attempt": None,
        "method": "alt",
        "target_mods": ["FireResist1"],
        "export_version": "3.30",
        "assumptions": [],
        "warnings": ["no hit in 200000 Monte Carlo samples"],
    }
    monkeypatch.setattr(recipe_odds, "mod_odds", lambda *a, **k: unresolved)
    ev = await run(ctx, store, "regalia-weights")
    assert ev.breakdown is None
    assert ev.problems == [
        "step '1. alteration_spam': mod odds unresolved: no hit in 200000 Monte Carlo samples"
    ]


async def test_missing_game_data_blocks_weights_recipes(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    ev = await run(ctx, store, "regalia-weights")
    assert ev.breakdown is None and "mod odds unavailable" in ev.problems[0]


async def test_unavailable_trade_price_is_reported_not_raised(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, clock: FakeClock
) -> None:
    mock_trade(api)
    trade.limiter.paused_until = clock() + 90
    ev = await run(ctx, store, "life-plate")
    hit = by_name(ev)["life plate"]
    assert hit.status == "error" and hit.chaos is None
    assert hit.error is not None and hit.error.startswith("price unavailable")
    assert ev.breakdown is not None and ev.breakdown.outcomes[0].p25 is None
    assert any(w.startswith("outcome 'life plate': price unavailable") for w in ev.warnings)


async def test_old_quotes_carry_their_age(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, clock: FakeClock
) -> None:
    mock_trade(api)
    await run(ctx, store, "bow-flip")
    clock.advance(40 * 60)
    trade.limiter.paused_until = clock() + 90
    ev = await run(ctx, store, "bow-flip")
    assert "quote age: crafted bow price is 40 min old" in ev.warnings
    assert "outcome 'crafted bow' is priced from an old cached copy (stale)" in ev.warnings


async def test_recipe_warnings_and_placeholder_urls(
    ctx: AppContext, api: NinjaApi, store: RecipeStore
) -> None:
    recipe = store.get("bow-flip")
    assert recipe is not None
    outcome = recipe.outcomes[0].model_copy(update={"trade_url": "https://x/trade/<id>"})
    placeholder = recipe.model_copy(update={"status": "unverified", "outcomes": [outcome]})
    ev = await evaluate_recipe(ctx, placeholder, Pricer(ctx), 0.05)
    assert ev.warnings[:2] == placeholder.warnings()
    assert by_name(ev)["crafted bow"].error == "placeholder trade URL; fill in a real saved search"


async def test_target_that_cannot_roll_blocks_the_craft(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, gamedata: None
) -> None:
    mock_trade(api)
    recipe = store.get("regalia-weights")
    assert recipe is not None
    chance = recipe.steps[0].p_success.model_copy(update={"target_mods": ["ChaosResist1"]})  # type: ignore[union-attr]
    step = recipe.steps[0].model_copy(update={"p_success": chance})
    ev = await evaluate_recipe(ctx, recipe.model_copy(update={"steps": [step]}), Pricer(ctx), 0.05)
    assert ev.breakdown is None
    assert "chance of 0, below the supported 1e-12" in ev.problems[0]
    assert ev.chances[0].report()["odds_warnings"][0].startswith("target cannot roll")


async def test_different_failure_values_use_their_weighted_average(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    recipe = store.get("life-plate")
    assert recipe is not None
    hit, bricked = recipe.outcomes
    lost = bricked.model_copy(update={"name": "lost", "use_salvage": False, "fixed_chaos": 0.0})
    outcomes = [hit.model_copy(update={"probability": 0.8}), bricked, lost]
    ev = await evaluate_recipe(
        ctx, recipe.model_copy(update={"outcomes": outcomes}), Pricer(ctx), 0.05
    )
    assert ev.breakdown is not None
    assert ev.breakdown.v == pytest.approx(0.8 * 4.75 + 0.1 * 1.0 + 0.1 * 0.0)
    assert ev.breakdown.p_fail == pytest.approx(0.2)
    assert any("probability-weighted average 0.5c" in w for w in ev.warnings)
