# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja, test_trade_pricing and others)
"""cluster_combo_survey: enchant selection, provisional values, ranking, cache-first order,
deadline and rate-limit stops, the Small path and the tool.

The trade fake answers by (enchant option, query shape). Fixture Medium enchants: Chaos Damage
over Time (option 4, four priceable notables), Damage over Time (only an unpriceable pair),
Aura Effect (legacy tag), Herald and Minion Life (no pairs) and Critical Chance (option 8; its
two notables get made-up trade ids here).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from test_cluster_combos import (
    BRUSH_STAT,
    DIV,
    ES,
    FL,
    GAMEDATA,
    LT,
    T_BR,
    T_ES,
    T_FL,
    T_LT,
    T_WP,
    WP,
    FakeTrade,
    pair,
)
from test_ninja import NinjaApi, api, ctx
from test_tools_prices import call, session
from test_trade_pricing import trade

from poe_craft_mcp.clients.trade import TradeClient, get_trade
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.gamedata.combos import combo_odds
from poe_craft_mcp.gamedata.loader import load_gamedata
from poe_craft_mcp.gamedata.models import GameData, GameDataError
from poe_craft_mcp.services.combo_survey import SurveyRequest, cluster_combo_survey
from poe_craft_mcp.services.combo_survey_enchant import provisional_value, select_enchants
from poe_craft_mcp.services.combo_survey_report import rank

if TYPE_CHECKING:
    from conftest import FakeClock

PP, OM = "AfflictionNotablePressurePoints__", "AfflictionNotableOverwhelmingMalice"
T_PP, T_OM = "explicit.stat_1000000011", "explicit.stat_1000000012"
CDOT, CRIT = 4, 8  # trade options
CDOT_ID, CRIT_ID = "affliction_chaos_damage_over_time_multiplier", "affliction_critical_chance"
MEDIUM = "Medium Cluster Jewel"


@pytest.fixture(scope="module")
def data() -> GameData:
    """test_cluster_combos' game data (made-up ids for Low Tolerance and Flow of Life, an
    exclusion-only id for Brush with Death) plus ids for the Critical Chance notables."""
    base = load_gamedata(GAMEDATA)
    extra = {
        "local_affliction_notable_low_tolerance": T_LT,
        "local_affliction_notable_flow_of_life": T_FL,
        "local_affliction_notable_pressure_points": T_PP,
        "local_affliction_notable_overwhelming_malice": T_OM,
    }
    return replace(
        base,
        trade_stats={**base.trade_stats, **extra},
        explicit_trade_ids={**base.explicit_trade_ids, BRUSH_STAT: (T_BR,)},
    )


class EnchantTrade(FakeTrade):
    @staticmethod
    def key(body: dict[str, Any]) -> Any:
        option = body["query"]["stats"][0]["filters"][0]["value"]["option"]
        return (option, FakeTrade.key(body))


@pytest.fixture
def fake(api: NinjaApi, trade: TradeClient) -> EnchantTrade:
    return EnchantTrade(api)


def scenario(fake: EnchantTrade) -> None:
    """CDoT: LT+FL not sellable; candidates ES+WP (4 listings at or above T, 3rd cheapest 3 div),
    ES+LT (200c: 190c after the undercut, under T) and WP+FL (1.5 div). Crit: PP+OM 1.1 div."""
    fake.responses[(CDOT, "below")] = [
        (50, "chaos", (T_LT, T_FL)),
        (55, "chaos", (T_LT, T_FL)),
        (60, "chaos", (T_LT, T_FL)),
        (150, "chaos", (T_WP, T_FL)),
        (160, "chaos", (T_WP, T_FL)),
    ]
    fake.responses[(CDOT, "above")] = [
        (2, "divine", (T_ES, T_WP)),
        (2.5, "divine", (T_ES, T_WP)),
        (3, "divine", (T_ES, T_WP)),
        (4, "divine", (T_ES, T_WP)),
        (200, "chaos", (T_ES, T_LT)),
        (1.5, "divine", (T_WP, T_FL)),
    ]
    fake.responses[(CRIT, "below")] = []
    fake.responses[(CRIT, "above")] = [(1.1, "divine", (T_PP, T_OM))]


def request(**overrides: Any) -> SurveyRequest:
    args: dict[str, Any] = {"base": MEDIUM, "passives": 4, "ilvl": 84}
    return SurveyRequest(**{**args, **overrides})


def p_hit(data: GameData, enchant: str, values: dict[frozenset[str], float]) -> float:
    return combo_odds(data, MEDIUM, 84, enchant, 4, values).p_hit


# --- selection and rules ----------------------------------------------------------------------


def test_enchant_selection(data: GameData) -> None:
    clusters = {c.name: c for c in data.clusters.values()}
    selected, skipped = select_enchants(clusters[MEDIUM], None)
    assert [e.name for e in selected] == [
        "Chaos Damage over Time",
        "Damage over Time",
        "Minion Damage while you have a Herald",
        "Critical Chance",
        "Minion Life",
    ]
    assert [s["enchant"] for s in skipped] == ["Aura Effect"]
    assert "legacy" in skipped[0]["reason"]
    _, small_skipped = select_enchants(clusters["Small Cluster Jewel"], None)
    attribute = [s for s in small_skipped if s["enchant"] == "Strength"]
    assert attribute and "attribute enchant" in attribute[0]["reason"]
    named, none_skipped = select_enchants(clusters[MEDIUM], ["Minion Life", CRIT_ID, "crit"])
    assert [e.name for e in named] == ["Critical Chance", "Minion Life"] and not none_skipped
    with pytest.raises(GameDataError, match="unknown enchant"):
        select_enchants(clusters[MEDIUM], ["Lightning Damage"])


async def test_passives_must_fit_the_base(ctx: AppContext, data: GameData) -> None:
    with pytest.raises(ValueError, match="adds 4-6 passive skills"):
        await cluster_combo_survey(ctx, data, request(passives=9))
    with pytest.raises(ValueError, match="limit"):
        await cluster_combo_survey(ctx, data, request(limit=0))


def test_provisional_value_rule() -> None:
    assert provisional_value([100, 200, 300, 400], 3, 0.05) == pytest.approx(285.0)
    # Fewer than min_listings listed: a seller must undercut the cheapest competing listing.
    assert provisional_value([100, 200], 3, 0.05) == pytest.approx(95.0)
    assert provisional_value([250], 1, 0.0) == 250


def scored(enchant_id: str, p: float, n: int) -> tuple[float, dict[str, Any]]:
    row = {"enchant_id": enchant_id, "p_any_provisional": round(p, 6)}
    return p, {**row, "provisional_sellable_pairs": n}


def test_rank_order() -> None:
    """p descending, then sellable pairs descending, then enchant id."""
    rows = [
        scored("a", 0.1, 1),
        scored("b", 0.2, 1),
        scored("e", 0.1, 3),
        scored("d", 0.0, 0),
        scored("c", 0.1, 3),
    ]
    assert [r["enchant_id"] for r in rank(rows)] == ["b", "c", "e", "a", "d"]


def test_rank_uses_unrounded_chances() -> None:
    """0.10000004 and 0.10000003 display alike but must not fall back to the pair count."""
    rows = [scored("a", 0.10000003, 5), scored("b", 0.10000004, 1)]
    assert rows[0][1]["p_any_provisional"] == rows[1][1]["p_any_provisional"]
    assert [r["enchant_id"] for r in rank(rows)] == ["b", "a"]


# --- the survey -------------------------------------------------------------------------------


async def test_survey_ranks_enchants(ctx: AppContext, fake: EnchantTrade, data: GameData) -> None:
    scenario(fake)
    payload, sources = await cluster_combo_survey(ctx, data, request())
    assert len(fake.searches) == 4  # two discovery searches per surveyed enchant, nothing else
    assert {fake.key(b)[1] for b in fake.searches} == {"below", "above"}
    assert payload["threshold"] == {"divine": 1.0, "chaos": DIV}
    skipped = {s["enchant"]: s["reason"] for s in payload["skipped"]}
    assert set(skipped) == {
        "Aura Effect",
        "Damage over Time",
        "Minion Damage while you have a Herald",
        "Minion Life",
    }
    assert "own trade ids" in skipped["Damage over Time"]
    assert "no pairs" in skipped["Minion Life"]

    cdot_values = {pair(ES, WP): 3 * DIV * 0.95, pair(FL, WP): 1.5 * DIV * 0.95}
    p_cdot = p_hit(data, CDOT_ID, cdot_values)
    p_crit = p_hit(data, CRIT_ID, {pair(OM, PP): 1.1 * DIV * 0.95})
    rows = {r["enchant"]: r for r in payload["ranking"]}
    expected = sorted(
        ["Chaos Damage over Time", "Critical Chance"],
        key=lambda n: -{"Chaos Damage over Time": p_cdot, "Critical Chance": p_crit}[n],
    )
    assert [r["enchant"] for r in payload["ranking"]] == expected
    cdot = rows["Chaos Damage over Time"]
    assert cdot["enchant_id"] == CDOT_ID
    assert (cdot["provisional_sellable_pairs"], cdot["candidates"], cdot["not_sellable"]) == (
        2,
        3,
        1,
    )
    assert cdot["unpriceable_pairs"] == 4
    assert cdot["p_any_provisional"] == pytest.approx(p_cdot, rel=1e-5)
    assert cdot["expected_attempts_per_hit"] == pytest.approx(1 / p_cdot, abs=0.05)
    hit = combo_odds(data, MEDIUM, 84, CDOT_ID, 4, cdot_values).expected_hit_value
    assert cdot["provisional_hit_value_per_attempt"]["chaos"] == pytest.approx(hit, abs=0.01)
    assert cdot["provisional_hit_value_per_attempt"]["divine"] == pytest.approx(hit / DIV, abs=1e-3)
    assert cdot["top_pairs"] == [
        {
            "pair": "Eternal Suffering + Wicked Pall",
            "provisional_value_div": 2.85,
            "listings_at_or_above": 4,
        },
        {
            "pair": "Flow of Life + Wicked Pall",
            "provisional_value_div": 1.425,
            "listings_at_or_above": 1,
        },
    ]
    assert cdot["from_cache"] is False
    assert payload["pending"] == [] and payload["stopped"] is None and "note" not in payload
    assert payload["discovery"]["enchants_surveyed"] == 2
    assert payload["discovery"]["truncated"] == []
    assert "cluster_combos" in payload["next_step"]
    assert any("PROVISIONAL" in a for a in payload["assumptions"])
    assert any("Only instant-buyout" in a for a in payload["assumptions"])
    names = [s["name"] for s in sources]
    assert names.count("pathofexile.com trade") == 4 and "poe.ninja Currency" in names
    assert all(s.get("instant_buyout") for s in sources if s["name"].startswith("pathofexile"))


async def test_limit_and_truncation(ctx: AppContext, fake: EnchantTrade, data: GameData) -> None:
    scenario(fake)
    fake.totals[(CRIT, "above")] = 40
    payload, _ = await cluster_combo_survey(ctx, data, request(limit=1))
    assert len(payload["ranking"]) == 1 and payload["ranked_count"] == 2
    assert any("capped at limit=1" in w for w in payload["warnings"])
    assert payload["discovery"]["truncated"] == ["Critical Chance: at or above T fetched 1 of 40"]
    assert "truncation_effect" in payload["discovery"]


async def test_cached_enchants_go_first_then_live(
    ctx: AppContext, fake: EnchantTrade, data: GameData
) -> None:
    scenario(fake)
    await cluster_combo_survey(ctx, data, request(enchants=("Critical Chance",)))
    assert [fake.key(b)[0] for b in fake.searches] == [CRIT, CRIT]
    payload, _ = await cluster_combo_survey(ctx, data, request())
    assert [fake.key(b)[0] for b in fake.searches] == [CRIT, CRIT, CDOT, CDOT]
    rows = {r["enchant"]: r for r in payload["ranking"]}
    assert rows["Critical Chance"]["from_cache"] is True
    assert rows["Chaos Damage over Time"]["from_cache"] is False
    assert payload["discovery"]["from_cache"] == 1
    again, _ = await cluster_combo_survey(ctx, data, request())
    assert len(fake.searches) == 4 and again["discovery"]["from_cache"] == 2


async def test_deadline_leaves_pending_and_continues(
    ctx: AppContext, fake: EnchantTrade, data: GameData
) -> None:
    """Searches are paced 2 s apart: CDoT at 0 s and 2 s; Crit would start at 4 s, past 3 s."""
    scenario(fake)
    payload, _ = await cluster_combo_survey(ctx, data, request(max_seconds=3))
    assert [r["enchant"] for r in payload["ranking"]] == ["Chaos Damage over Time"]
    assert payload["pending"] == [{"enchant": "Critical Chance", "enchant_id": CRIT_ID}]
    assert payload["stopped"].startswith("max_seconds time budget spent")
    assert payload["retry_after_seconds"] is not None
    assert "call again" in payload["note"] and "20 minutes" in payload["note"]
    assert any("survey stopped early" in w for w in payload["warnings"])

    second, _ = await cluster_combo_survey(ctx, data, request())
    assert second["pending"] == [] and second["discovery"]["from_cache"] == 1
    assert fake.searched((CDOT, "below")) == 1 and fake.searched((CRIT, "below")) == 1


async def test_rate_limit_stops_with_retry_hint(
    ctx: AppContext, fake: EnchantTrade, data: GameData, clock: FakeClock
) -> None:
    scenario(fake)
    fake.fail[(CRIT, "below")] = httpx.Response(429, headers={"Retry-After": "60"})
    payload, _ = await cluster_combo_survey(ctx, data, request())
    assert payload["stopped"].startswith("trade site rate limited; retry in about")
    assert payload["retry_after_seconds"] >= 60  # the limiter adds its own margin
    assert [p["enchant"] for p in payload["pending"]] == ["Critical Chance"]
    assert [r["enchant"] for r in payload["ranking"]] == ["Chaos Damage over Time"]

    del fake.fail[(CRIT, "below")]
    clock.advance(payload["retry_after_seconds"] + 1)
    second, _ = await cluster_combo_survey(ctx, data, request())
    assert second["pending"] == [] and len(second["ranking"]) == 2
    assert fake.searched((CDOT, "below")) == 1


async def test_stale_discovery_is_never_used(
    ctx: AppContext, fake: EnchantTrade, data: GameData, clock: FakeClock
) -> None:
    scenario(fake)
    await cluster_combo_survey(ctx, data, request())
    clock.advance(25 * 60)  # discovery expired; the trade site now refuses
    fake.fail[(CDOT, "below")] = httpx.Response(429, headers={"Retry-After": "60"})
    payload, _ = await cluster_combo_survey(ctx, data, request())
    assert payload["ranking"] == []
    assert payload["stopped"].startswith("the trade site served only a stale cached copy")
    assert payload["retry_after_seconds"] >= 60  # the 429's pause, from the limiter
    assert [p["enchant"] for p in payload["pending"]] == [
        "Chaos Damage over Time",
        "Critical Chance",
    ]
    # The stale band A stops at once: band B and the next enchant are not searched again.
    assert fake.searched((CDOT, "below")) == 2 and fake.searched((CDOT, "above")) == 1
    assert fake.searched((CRIT, "below")) == 1


async def test_cache_phase_never_makes_a_request(
    ctx: AppContext,
    fake: EnchantTrade,
    data: GameData,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each cache read ages the cache by 11 minutes, so both bands expire right after the
    cache phase read them: a second lookup would search live."""
    scenario(fake)
    await cluster_combo_survey(ctx, data, request(enchants=("Critical Chance",)))
    trade = get_trade(ctx)
    real = trade.listings_for

    async def ageing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        found = await real(*args, **kwargs)
        if kwargs.get("cache_only"):
            clock.advance(11 * 60)
        return found

    monkeypatch.setattr(trade, "listings_for", ageing)
    payload, _ = await cluster_combo_survey(ctx, data, request(enchants=("Critical Chance",)))
    assert len(fake.searches) == 2
    assert [r["enchant"] for r in payload["ranking"]] == ["Critical Chance"]
    assert payload["ranking"][0]["from_cache"] is True
    assert payload["discovery"]["from_cache"] == 1 and payload["pending"] == []


async def test_listings_exactly_at_threshold_are_not_below(
    ctx: AppContext, fake: EnchantTrade, data: GameData
) -> None:
    """Both trade bounds include T: three PP+OM listings at exactly 1 divine come back in
    both searches but count only at or above T, so the pair is a candidate, not not_sellable."""
    scenario(fake)
    at_t = [(1, "divine", (T_PP, T_OM))] * 3
    fake.responses[(CRIT, "below")] = at_t
    fake.responses[(CRIT, "above")] = at_t
    payload, _ = await cluster_combo_survey(ctx, data, request(enchants=("Critical Chance",)))
    row = payload["ranking"][0]
    assert (row["candidates"], row["not_sellable"]) == (1, 0)
    # Its provisional value is 195c less the 5% undercut, under T: not provisionally sellable.
    assert row["provisional_sellable_pairs"] == 0 and row["p_any_provisional"] == 0


async def test_unidentifiable_notable_skips_the_enchant(
    ctx: AppContext, fake: EnchantTrade, data: GameData
) -> None:
    blind = replace(
        data,
        explicit_trade_ids={k: v for k, v in data.explicit_trade_ids.items() if k != BRUSH_STAT},
    )
    scenario(fake)
    payload, _ = await cluster_combo_survey(ctx, blind, request())
    skipped = {s["enchant"]: s["reason"] for s in payload["skipped"]}
    assert skipped["Chaos Damage over Time"].startswith("cannot rule out jewels carrying Brush")
    assert [fake.key(b)[0] for b in fake.searches] == [CRIT, CRIT]


async def test_small_cluster_has_no_pairs(
    ctx: AppContext, fake: EnchantTrade, data: GameData
) -> None:
    payload, sources = await cluster_combo_survey(
        ctx, data, request(base="Small Cluster Jewel", passives=2)
    )
    assert payload["ranking"] == [] and "no pairs" in payload["note"]
    assert sources == [] and fake.searches == []


# --- the tool ---------------------------------------------------------------------------------

ARGS = {"base": MEDIUM, "passives": 4, "ilvl": 84}


async def test_tool_is_listed_read_only_open_world(ctx: AppContext) -> None:
    async with session(ctx) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    tool = tools["cluster_combo_survey"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is True
    assert "provisional" in (tool.description or "").lower()
    assert set(tool.input_schema["required"]) == {"base", "passives", "ilvl"}


async def test_tool_envelope_and_errors(
    ctx: AppContext, fake: EnchantTrade, data: GameData
) -> None:
    ctx.services["gamedata"] = data
    scenario(fake)
    async with session(ctx) as client:
        out = await call(client, "cluster_combo_survey", **ARGS, enchants=["Critical Chance"])
        small = await call(
            client, "cluster_combo_survey", **{**ARGS, "base": "Small Cluster Jewel"}
        )
        bad = await client.call_tool("cluster_combo_survey", {**ARGS, "min_listings": 0})
    assert [r["enchant"] for r in out["ranking"]] == ["Critical Chance"]
    names = [s["name"] for s in out["sources"]]
    assert names[0].startswith("RePoE export") and names.count("pathofexile.com trade") == 2
    assert out["data_age_seconds"] is not None
    assert small["ranking"] == [] and "no pairs" in small["note"]
    assert bad.is_error and "min_listings" in str(bad.content)
