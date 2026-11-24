# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja and test_trade_pricing)
"""Recipe -> engine mapping, checked by hand on the fixtures.

Prices used (tests/fixtures/ninja, tests/fixtures/trade): Astral Plate ilvl >= 85 3.0c,
Imperial Bow ilvl >= 85 2.2c, Orb of Alteration 0.2118c, every trade outcome p25 5.0c with
30 listings (sample 30). Undercut 5%, so a 5.0c outcome is worth 4.75c.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from test_ninja import NinjaApi, api, ctx, load
from test_trade import SAVED_URL, blob, recorded
from test_trade_pricing import TRADE, fetch_body, mock_trade, trade

from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.trade import TradeClient
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.rank import rank
from poe_craft_mcp.gamedata.loader import load_gamedata
from poe_craft_mcp.recipes.loader import RecipeStore
from poe_craft_mcp.services import recipe_odds
from poe_craft_mcp.services.evaluate import NO_SALE, Evaluation, evaluate_recipe
from poe_craft_mcp.services.recipe_inputs import Pricer, TimeBudgetSpent, check_overrides

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
    assert inputs["Orb of Alteration"].volume == pytest.approx(157730 / 0.2118)
    assert inputs["Augmentation"].status == "fixed"
    hit = inputs["life plate"]
    assert (hit.chaos, hit.listing_count) == (5.0, 30)
    assert hit.source["name"] == "pathofexile.com trade" and hit.source["search_id"]
    assert [o.is_failure for o in b.outcomes] == [False, True]


async def test_recipe_trade_inputs_use_instant_buyout_only(
    ctx: AppContext, api: NinjaApi, trade: TradeClient
) -> None:
    search, _ = mock_trade(api)
    pricer = Pricer(ctx)
    for kind, status in (("base", "any"), ("outcome", "online"), ("salvage", None)):
        query: dict[str, Any] = {"type": f"{kind} item"}
        if status:
            query["status"] = {"option": status}
        url = f"{TRADE}/trade/search/Settlers/{blob(json.dumps({'query': query}).encode())}"
        priced = await pricer.trade(kind, kind, url)
        sent = json.loads(search.calls.last.request.content)
        assert sent["query"] == {"type": f"{kind} item", "status": {"option": "securable"}}
        assert priced.status == "ok" and priced.source["instant_buyout"] is True
    exchange = f"{TRADE}/trade/exchange/Covens/Ab12Cd"
    refused = await pricer.trade("outcome", "bulk", exchange)
    assert (refused.status, refused.chaos) == ("error", None)
    assert refused.error == "bad trade URL: exchange listings are whisper-based; use an item search"
    assert search.call_count == 3


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
    assert any("use the recipe's item level 5" in w for w in ev.warnings)


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


async def test_failure_outcomes_keep_their_own_values_and_overrides(
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
    assert [o.value for o in ev.breakdown.outcomes] == pytest.approx([4.75, 1.0, 0.0])
    assert [o.contribution for o in ev.breakdown.outcomes] == pytest.approx([3.8, 0.1, 0.0])
    assert ev.breakdown.v == pytest.approx(3.9) and ev.breakdown.p_fail == pytest.approx(0.2)

    overridden = await run(ctx, store, "life-plate", {"bricked": 0.3})
    assert overridden.breakdown is not None
    assert overridden.breakdown.v == pytest.approx(0.9 * 4.75 + 0.1 * 0.3)
    inputs = by_name(overridden)
    assert inputs["bricked"].status == "override" and "salvage" not in inputs
    assert not any("matches no input" in w for w in overridden.warnings)


async def test_recipe_without_a_sale_outcome_is_not_rankable(
    ctx: AppContext, api: NinjaApi, store: RecipeStore
) -> None:
    recipe = store.get("deprecated-flip")
    assert recipe is not None
    verified = recipe.model_copy(update={"status": "verified"})
    ev = await evaluate_recipe(ctx, verified, Pricer(ctx), 0.05)
    assert ev.breakdown is not None and ev.breakdown.p_fail == 1
    assert ev.unrankable == [NO_SALE] and ev.rank_blockers() == [NO_SALE]
    assert NO_SALE in ev.warnings


async def test_p_success_without_repeat_until_is_costed_once(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    recipe = store.get("bow-flip")
    assert recipe is not None
    chance = {"p_source": "user", "value": 0.5}
    step = recipe.steps[0].model_validate({**recipe.steps[0].model_dump(), "p_success": chance})
    ev = await evaluate_recipe(ctx, recipe.model_copy(update={"steps": [step]}), Pricer(ctx), 0.05)
    assert ev.breakdown is not None
    assert (ev.breakdown.c, ev.breakdown.c90) == pytest.approx((2.5, 2.5))
    assert ev.breakdown.steps[0].p_success is None and ev.chances == []
    assert "step '1. bench_craft': p_success ignored: step is not repeated" in ev.warnings


async def test_weights_odds_use_the_selected_base_item_level(
    ctx: AppContext,
    api: NinjaApi,
    trade: TradeClient,
    store: RecipeStore,
    gamedata: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_trade(api)
    seen: list[tuple[str, int]] = []

    def fake_odds(data: Any, base: str, ilvl: int, mods: Any, method: str) -> dict[str, Any]:
        seen.append((base, ilvl))
        return {
            "chance_per_attempt": 0.5,
            "method": method,
            "target_mods": mods,
            "export_version": "3.30",
            "assumptions": [],
            "warnings": [],
        }

    monkeypatch.setattr(recipe_odds, "mod_odds", fake_odds)
    recipe = store.get("regalia-weights")
    assert recipe is not None
    lookup = {"source": "ninja", "type": "BaseType", "name": "Stygian Vise", "ilvl_min": 82}
    chance = recipe.steps[0].p_success.model_copy(update={"base": None, "ilvl": None})  # type: ignore[union-attr]
    vise = recipe.model_validate(
        {
            **recipe.to_document(),
            "base": {"lookup": lookup},
            "steps": [{**recipe.steps[0].model_dump(), "p_success": chance.model_dump()}],
        }
    )
    ev = await evaluate_recipe(ctx, vise, Pricer(ctx), 0.05)
    assert by_name(ev)["Stygian Vise"].chaos == 92.4  # cheapest ilvl >= 82 is the ilvl 83 row
    assert seen == [("Stygian Vise", 83)]
    assert not any("item level" in w for w in ev.warnings)


async def test_exchange_volume_is_converted_to_units(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    body = load("exchange_essence.json")
    body["lines"] = [{"id": "test-essence", "primaryValue": 20, "volumePrimaryValue": 200}]
    api.change("Essence", body)
    recipe = store.get("bow-flip")
    assert recipe is not None
    essence = {"source": "ninja", "category": "Essence", "id": "test-essence", "qty": 5}
    step = recipe.steps[0].model_validate({"action": "essence", "materials": [essence]})
    ev = await evaluate_recipe(ctx, recipe.model_copy(update={"steps": [step]}), Pricer(ctx), 0.05)
    assert by_name(ev)["test-essence"].volume == pytest.approx(10)  # 200c of volume at 20c
    assert ev.breakdown is not None
    ranked = rank([ev.breakdown], 1, 195)
    assert ranked.items[0].flags == ("low volume: test-essence volume 10 < 10 x expected qty 5",)
    alt = await run(ctx, store, "life-plate")
    assert by_name(alt)["Orb of Alteration"].volume == pytest.approx(157730 / 0.2118)


async def test_cached_quotes_are_served_after_the_time_budget(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, clock: FakeClock
) -> None:
    search, fetch = mock_trade(api)
    urls = {r.id: r.outcomes[0].trade_url for r in store.recipes.values()}
    await Pricer(ctx).trade("outcome", "warm", urls["bow-flip"])
    late = Pricer(ctx, deadline=clock(), clock=clock)
    cached = await late.trade("outcome", "crafted bow", urls["bow-flip"])
    assert (cached.chaos, cached.status) == (5.0, "ok")
    with pytest.raises(TimeBudgetSpent):
        await late.trade("outcome", "life plate", urls["life-plate"])
    assert (search.call_count, fetch.call_count) == (1, 3)


async def test_no_trade_request_starts_after_the_time_budget(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, clock: FakeClock
) -> None:
    def slow_search(request: httpx.Request) -> httpx.Response:
        clock.advance(10)
        return recorded("search")

    search = api.router.post(f"{TRADE}/api/trade/search/Covens").mock(side_effect=slow_search)
    fetch = api.router.get(url__startswith=f"{TRADE}/api/trade/fetch/").mock(
        return_value=recorded("fetch")
    )
    pricer = Pricer(ctx, deadline=clock() + 5, clock=clock)
    with pytest.raises(TimeBudgetSpent):
        await pricer.trade("outcome", "crafted bow", SAVED_URL)
    assert (search.call_count, fetch.call_count) == (1, 0)


async def test_trade_age_includes_the_currency_rate_age(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, clock: FakeClock
) -> None:
    mock_trade(api, fetch=fetch_body([(0.05, "divine")] * 10))
    await run(ctx, store, "bow-flip")
    clock.advance(40 * 60)
    api.endpoints["Currency"].status = 500  # rates now come from the 40-minute-old cache
    ev = await run(ctx, store, "bow-flip")
    bow = by_name(ev)["crafted bow"]
    assert bow.chaos == pytest.approx(0.05 * 195) and bow.stale
    assert bow.source["age_seconds"] == 0 and 2400 <= (bow.age_seconds or 0) < 2460
    assert "quote age: crafted bow price is 40 min old" in ev.warnings


async def test_overridden_sale_price_is_shown_but_not_rankable(
    ctx: AppContext, api: NinjaApi, store: RecipeStore
) -> None:
    recipe = store.get("bow-flip")
    assert recipe is not None
    outcome = recipe.outcomes[0].model_copy(update={"trade_url": "https://x/trade/<id>"})
    placeholder = recipe.model_copy(update={"outcomes": [outcome]})
    pricer = Pricer(ctx, check_overrides({"crafted bow": 100}))
    ev = await evaluate_recipe(ctx, placeholder, pricer, 0.05)
    assert ev.breakdown is not None and ev.breakdown.v == pytest.approx(95)
    assert ev.rank_blockers() == ["sale price overridden: no live listings for 'crafted bow'"]


async def test_limiter_wait_past_the_budget_counts_as_time_budget(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    search, _ = mock_trade(api)
    trade.limiter.paused_until = clock() + 2  # a short wait the limiter would normally make
    with pytest.raises(TimeBudgetSpent):
        await Pricer(ctx, deadline=clock() + 1, clock=clock).trade("outcome", "x", SAVED_URL)
    trade.limiter.paused_until = clock() + 90  # a real rate limit, longer than any wait
    real = await Pricer(ctx, deadline=clock() + 60, clock=clock).trade("outcome", "x", SAVED_URL)
    assert real.error is not None and real.error.startswith("price unavailable")
    assert search.call_count == 0


async def test_stale_rates_do_not_age_chaos_only_listings(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore, clock: FakeClock
) -> None:
    mock_trade(api)
    await run(ctx, store, "bow-flip")
    clock.advance(40 * 60)
    api.endpoints["Currency"].status = 500  # rates stale, but no listing needed converting
    ev = await run(ctx, store, "bow-flip")
    bow = by_name(ev)["crafted bow"]
    assert (bow.stale, bow.age_seconds) == (False, 0)
    assert not any("crafted bow" in w for w in ev.warnings)


async def test_in_flight_cancellation_is_time_budget_whatever_the_next_wait(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    sent: list[httpx.Request] = []

    async def last_slot(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        trade.limiter.paused_until = clock() + 90  # the next request would wait past the cap
        await asyncio.sleep(30)  # the response would arrive after the ranking deadline
        return recorded("search")

    api.router.post(f"{TRADE}/api/trade/search/Covens").mock(side_effect=last_slot)
    pricer = Pricer(ctx, deadline=clock() + 0.05, clock=clock)
    with pytest.raises(TimeBudgetSpent):
        await pricer.trade("outcome", "x", SAVED_URL)
    assert len(sent) == 1


async def test_server_429_just_before_the_budget_is_a_price_error(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, clock: FakeClock
) -> None:
    search = api.router.post(f"{TRADE}/api/trade/search/Covens").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "2"})
    )
    pricer = Pricer(ctx, deadline=clock() + 1, clock=clock)  # "second 59" of the budget
    priced = await pricer.trade("outcome", "x", SAVED_URL)
    assert priced.error is not None and priced.error.startswith("price unavailable")
    assert "HTTP 429" in priced.error and search.call_count == 1


async def test_salvage_lookup_uses_its_poe_ninja_type(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    recipe = store.get("life-plate")
    assert recipe is not None
    lookup = {"source": "ninja", "type": "Currency", "name": "Divine Orb"}
    divine = recipe.model_validate({**recipe.to_document(), "salvage": {"lookup": lookup}})
    ev = await evaluate_recipe(ctx, divine, Pricer(ctx), 0.05)
    quote = await get_ninja(ctx).price("Currency", "Divine Orb")
    assert quote is not None and quote["chaos"] > 1
    salvage = by_name(ev)["salvage"]
    assert salvage.status == "ok" and salvage.chaos == pytest.approx(quote["chaos"])


async def test_salvage_basetype_lookup_keeps_its_filters_in_any_case(
    ctx: AppContext, api: NinjaApi, trade: TradeClient, store: RecipeStore
) -> None:
    mock_trade(api)
    recipe = store.get("life-plate")
    assert recipe is not None
    lookup = {"source": "ninja", "type": "basetype", "name": "Stygian Vise", "ilvl_min": 82}
    vise = recipe.model_validate({**recipe.to_document(), "salvage": {"lookup": lookup}})
    ev = await evaluate_recipe(ctx, vise, Pricer(ctx), 0.05)
    assert by_name(ev)["salvage"].chaos == 92.4  # cheapest ilvl >= 82 row, as find_base picks
