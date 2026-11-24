# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja and test_trade_pricing)
"""cluster_combos service: queries, discovery, confirmation, sellable rule and profit maths.

The trade site is a respx fake that answers each search by its query shape (discovery below or
above the threshold, an exact pair, the miss search or the white base) with listings carrying
explicit trade stat ids. Fixture game data: Medium Cluster Jewel, Chaos Damage over Time, five
prefix notables; Brush with Death has no single trade id here (only an exclusion id), so its
pairs are unpriceable.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from test_ninja import NinjaApi, api, ctx
from test_trade_pricing import TRADE, trade

from poe_craft_mcp.clients.trade import TradeClient
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.gamedata.combo_pairs import is_notable
from poe_craft_mcp.gamedata.combos import ComboOdds, cluster_notables, combo_odds
from poe_craft_mcp.gamedata.loader import load_gamedata
from poe_craft_mcp.gamedata.models import GameData
from poe_craft_mcp.services.cluster_combos import ComboRequest, cluster_combos
from poe_craft_mcp.services.combo_confirm import is_sellable, order_candidates
from poe_craft_mcp.services.combo_costs import economics
from poe_craft_mcp.services.combo_discovery import Tally, classify, tally
from poe_craft_mcp.services.combo_notables import pool_change, priceable
from poe_craft_mcp.services.combo_queries import (
    JewelFilter,
    base_query,
    discovery_query,
    miss_query,
    pair_query,
)

if TYPE_CHECKING:
    from conftest import FakeClock

GAMEDATA = Path(__file__).resolve().parent / "fixtures" / "gamedata"
WP, ES = "AfflictionNotableWickedPall_", "AfflictionNotableEternalSuffering"
LT, FL = "AfflictionNotableLowTolerance", "AfflictionNotableFlowofLife_"
BRUSH = "AfflictionNotableBrushwithDeath"
BRUSH_STAT = "local_affliction_notable_brush_with_death"
T_BR = "explicit.stat_1000000003"  # exclusion-only: Brush with Death stays unpriceable
T_WP, T_ES = "explicit.stat_1616734644", "explicit.stat_2144634814"
T_LT, T_FL = "explicit.stat_1000000001", "explicit.stat_1000000002"
JEWEL = JewelFilter("Medium Cluster Jewel", 4, 4)
DIV = 195.0  # chaos per divine in the ninja fixture
ORBS = {"transmutation": 0.04234, "alteration": 0.2118, "augmentation": 0.2549, "regal": 0.2965}


def pair(*ids: str) -> frozenset[str]:
    return frozenset(ids)


def pkey(a: str, b: str) -> tuple[str, ...]:
    """FakeTrade key of an exact pair search (its trade ids are sorted)."""
    return ("pair", *sorted((a, b)))


@pytest.fixture(scope="module")
def data() -> GameData:
    """Fixture export plus made-up trade ids for Low Tolerance and Flow of Life, and an
    exclusion-only id for Brush with Death (unpriceable, but searches can rule it out)."""
    base = load_gamedata(GAMEDATA)
    extra = {
        "local_affliction_notable_low_tolerance": T_LT,
        "local_affliction_notable_flow_of_life": T_FL,
    }
    brush = {BRUSH_STAT: (T_BR,)}
    return replace(
        base,
        trade_stats={**base.trade_stats, **extra},
        explicit_trade_ids={**base.explicit_trade_ids, **brush},
    )


Row = tuple[float, str, tuple[str, ...]]
"""(amount, currency, explicit trade stat ids) of one fake listing."""


class FakeTrade:
    """Answers trade searches by query shape; records every search body."""

    def __init__(self, api: NinjaApi, league: str = "Covens") -> None:
        self.responses: dict[Any, list[Row]] = {}
        self.totals: dict[Any, int] = {}
        self.fail: dict[Any, httpx.Response] = {}
        self.searches: list[dict[str, Any]] = []
        self._results: dict[str, list[Row]] = {}
        api.router.post(f"{TRADE}/api/trade/search/{league}").mock(side_effect=self._search)
        api.router.get(url__startswith=f"{TRADE}/api/trade/fetch/").mock(side_effect=self._fetch)

    @staticmethod
    def key(body: dict[str, Any]) -> Any:
        query = body["query"]
        rarity = query["filters"]["type_filters"]["filters"]["rarity"]["option"]
        if rarity == "normal":
            return "base"
        group = query["stats"][1]
        ids = tuple(f["id"] for f in group["filters"])
        if group["type"] == "and":
            return ("pair", *ids)
        if group["value"].get("max") == 1:
            return "miss"
        price = query["filters"]["trade_filters"]["filters"]["price"]
        return "below" if "max" in price else "above"

    def searched(self, key: Any) -> int:
        return sum(self.key(b) == key for b in self.searches)

    def _search(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.searches.append(body)
        key = self.key(body)
        if key in self.fail:
            return self.fail[key]
        # Like the trade site, a `not` group drops listings holding any of its stats.
        stats = body["query"]["stats"]
        excluded = {f["id"] for g in stats if g["type"] == "not" for f in g["filters"]}
        rows = [r for r in self.responses.get(key, []) if not excluded & set(r[2])]
        search_id = f"q{len(self.searches)}"
        self._results[search_id] = rows
        result = [f"{search_id}x{i}" for i in range(len(rows))]
        total = self.totals.get(key, len(rows))
        return httpx.Response(200, json={"id": search_id, "result": result, "total": total})

    def _fetch(self, request: httpx.Request) -> httpx.Response:
        ids = request.url.path.rsplit("/", 1)[1].split(",")
        entries = []
        for item_id in ids:
            search_id, index = item_id.split("x")
            amount, currency, stats = self._results[search_id][int(index)]
            entries.append(
                {
                    "id": item_id,
                    "listing": {
                        "indexed": "2026-11-23T00:00:00Z",
                        "price": {"amount": amount, "currency": currency},
                        "fee": 100,
                    },
                    "item": {"extended": {"hashes": {"explicit": [[s, [0]] for s in stats]}}},
                }
            )
        return httpx.Response(200, json={"result": entries})


def scenario(fake: FakeTrade) -> None:
    """Discovery: LT+FL not sellable (3 below), WP+LT thin, ES+FL unlisted; candidates ES+WP,
    ES+LT, WP+FL. Exact searches: ES+WP 2 div (sellable), ES+LT only 2 listings, WP+FL 250c
    (sellable after the 5% undercut: 237.5 >= 195)."""
    fake.responses["below"] = [
        (50, "chaos", (T_LT, T_FL)),
        (55, "chaos", (T_LT, T_FL)),
        (60, "chaos", (T_LT, T_FL, "explicit.stat_42")),
        (100, "chaos", (T_WP, T_LT)),
        (150, "chaos", (T_WP, T_FL)),
        (160, "chaos", (T_WP, T_FL)),
        (90, "chaos", (T_ES, T_WP, T_LT)),  # three notables: never attributed
        (10, "nonsense", (T_ES, T_WP)),  # unknown currency: dropped
    ]
    fake.responses["above"] = [
        (2, "divine", (T_ES, T_WP)),
        (2.5, "divine", (T_ES, T_WP)),
        (300, "chaos", (T_ES, T_LT)),
        (1.5, "divine", (T_WP, T_FL)),
    ]
    fake.responses[pkey(T_ES, T_WP)] = [(2, "divine", (T_ES, T_WP))] * 5
    fake.responses[pkey(T_ES, T_LT)] = [(300, "chaos", (T_ES, T_LT))] * 2
    fake.responses[pkey(T_FL, T_WP)] = [(250, "chaos", (T_WP, T_FL))] * 4
    fake.responses["miss"] = [(20, "chaos", (T_ES,))] * 10
    fake.responses["base"] = [(5, "chaos", ())] * 10


def request(**overrides: Any) -> ComboRequest:
    args: dict[str, Any] = {
        "base": "Medium Cluster Jewel",
        "enchant": "Chaos Damage over Time",
        "passives": 4,
        "ilvl": 84,
    }
    return ComboRequest(**{**args, **overrides})


@pytest.fixture
def fake(api: NinjaApi, trade: TradeClient) -> FakeTrade:
    return FakeTrade(api)


# --- query building ---------------------------------------------------------------------------

ENCHANT_GROUP = {
    "type": "and",
    "filters": [
        {"id": "enchant.stat_3948993189", "value": {"option": 4}},
        {"id": "enchant.stat_3086156145", "value": {"min": 4, "max": 4}},
    ],
}


def rare_filters(**extra: Any) -> dict[str, Any]:
    return {
        "misc_filters": {"filters": {"corrupted": {"option": "false"}}},
        "type_filters": {"filters": {"rarity": {"option": "rare"}}},
        **extra,
    }


def body(stats: list[dict[str, Any]], filters: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": {
            "status": {"option": "securable"},
            "type": "Medium Cluster Jewel",
            "stats": [ENCHANT_GROUP, *stats],
            "filters": filters,
        },
        "sort": {"price": "asc"},
    }


def test_discovery_queries() -> None:
    count = {"type": "count", "value": {"min": 2}, "filters": [{"id": T_WP}, {"id": T_ES}]}
    count["filters"].sort(key=lambda f: f["id"])
    below = discovery_query(JEWEL, [T_WP, T_ES], below=195.004)
    price_max = {"trade_filters": {"filters": {"price": {"max": 195.0}}}}
    assert below == body([count], rare_filters(**price_max))
    above = discovery_query(JEWEL, [T_ES, T_WP], at_least=195)
    price_min = {"trade_filters": {"filters": {"price": {"min": 195}}}}
    assert above == body([count], rare_filters(**price_min))
    assert "option" not in above["query"]["filters"]["trade_filters"]["filters"]["price"]
    with pytest.raises(ValueError):
        discovery_query(JEWEL, [T_WP], below=1, at_least=2)


def test_pair_miss_and_base_queries() -> None:
    both = {"type": "and", "filters": [{"id": T_WP}, {"id": T_ES}]}  # sorted trade ids
    assert pair_query(JEWEL, T_WP, T_ES) == body([both], rare_filters())
    one = {"type": "count", "value": {"min": 1, "max": 1}, "filters": [{"id": T_WP}, {"id": T_ES}]}
    assert miss_query(JEWEL, [T_WP, T_ES]) == body([one], rare_filters())
    white = {
        "misc_filters": {
            "filters": {"corrupted": {"option": "false"}, "ilvl": {"min": 75, "max": 83}}
        },
        "type_filters": {"filters": {"rarity": {"option": "normal"}}},
    }
    assert base_query(JEWEL, 75) == body([], white)
    high = base_query(JEWEL, 84)["query"]["filters"]["misc_filters"]["filters"]["ilvl"]
    assert high == {"min": 84, "max": 100}


# --- discovery --------------------------------------------------------------------------------


def test_tally_groups_pairs_and_counts_drops() -> None:
    mods = {T_WP: WP, T_ES: ES, T_LT: LT}
    listings = [
        {"amount": 1, "currency": "divine", "explicit_stats": [T_ES, T_WP]},
        {"amount": 100, "currency": "chaos", "explicit_stats": [T_WP, T_ES, "explicit.stat_9"]},
        {"amount": 5, "currency": "chaos", "explicit_stats": [T_ES, T_WP, T_LT]},
        {"amount": 5, "currency": "nonsense", "explicit_stats": [T_ES, T_WP]},
        {"amount": 5, "currency": "chaos", "explicit_stats": [T_ES]},
        {"amount": 5, "currency": "chaos"},
    ]
    found = tally(listings, {"chaos": 1.0, "divine": DIV}, mods, total=40)
    assert found.pairs == {pair(ES, WP): [100.0, DIV]}
    assert found.multi_notable_listings == 1
    assert found.unmatched_listings == 2
    assert found.dropped_unknown_currency == {"nonsense": 1}
    assert (found.fetched, found.total, found.truncated) == (6, 40, True)
    assert found.report()["pair_listings"] == 2


def test_classify() -> None:
    a, b, c, d = pair("a", "b"), pair("a", "c"), pair("b", "c"), pair("c", "d")
    e = pair("a", "d")
    below = Tally(pairs={a: [1, 2, 3], b: [1, 2], c: [1]})
    above = Tally(pairs={a: [500], b: [400], e: [300]})
    assert classify([a, b, c, d, e], below, above, 3) == {
        a: "not_sellable",
        b: "candidate",
        c: "thin",
        d: "unlisted",
        e: "candidate",
    }


def test_candidates_ordered_by_weight_product() -> None:
    weights = {"a": 1, "b": 3, "c": 2, "d": 3}
    pairs = [pair("a", "b"), pair("c", "d"), pair("b", "d"), pair("a", "c")]
    order = order_candidates(pairs, weights)
    assert order == [pair("b", "d"), pair("c", "d"), pair("a", "b"), pair("a", "c")]


def test_sellable_rule() -> None:
    assert is_sellable(195.0, 3, 195.0, 3)
    assert not is_sellable(194.99, 10, 195.0, 3)
    assert not is_sellable(500.0, 2, 195.0, 3)
    assert not is_sellable(None, 10, 195.0, 3)


def test_unpriceable_and_shared_trade_ids(data: GameData) -> None:
    info = cluster_notables(data, "Medium Cluster Jewel", 84, "Chaos Damage over Time", 4)
    split = priceable(data, info)
    assert set(split.trade_ids) == {WP, ES, LT, FL}
    assert len(split.pairs) == 6 and len(split.unpriceable_pairs) == 4
    assert all(BRUSH in p for p in split.unpriceable_pairs)
    assert any(w.startswith("Brush with Death cannot be priced") for w in split.warnings)
    shared = replace(
        data, trade_stats={**data.trade_stats, "local_affliction_notable_low_tolerance": T_FL}
    )
    split = priceable(shared, info)
    assert set(split.trade_ids) == {WP, ES}
    assert split.pairs == (pair(ES, WP),)
    assert any("shares trade id" in w for w in split.warnings)


# --- profit maths -----------------------------------------------------------------------------


def odds_fixture(**overrides: Any) -> ComboOdds:
    values: dict[str, Any] = {
        "base": "Medium Cluster Jewel",
        "ilvl": 84,
        "enchant": None,
        "anchors": ("a", "b"),
        "p_cycle_success": 1 / 3,
        "magic_states": {},
        "expected_alterations": 3.0,
        "expected_augmentations": 0.6,
        "regals": 1.0,
        "p_hit": 0.25,
        "per_pair": {pair("a", "b"): 0.25},
        "expected_hit_value": 25.0,
        "p_miss": 0.75,
        "miss_value": 20 / 3,
        "expected_miss_value": 5.0,
        "expected_value": 30.0,
        "assumptions": (),
        "warnings": (),
    }
    return ComboOdds(**{**values, **overrides})


def test_economics_known_numbers() -> None:
    """Cost = 5 + 1 + 3 x 0.5 + 0.6 x 0.25 + 2 = 9.65; profit 30 - 9.65 = 20.35 per attempt,
    x 60/4 = 305.25 per hour (1.565 div at 195); 1/0.25 = 4 attempts per hit."""
    orbs = {"transmutation": 1.0, "alteration": 0.5, "augmentation": 0.25, "regal": 2.0}
    econ = economics(
        odds_fixture(),
        miss_value=20 / 3,
        base=5.0,
        orbs=orbs,
        minutes_per_attempt=4,
        chaos_per_divine=DIV,
    )
    assert econ["cost_per_attempt"] == 9.65
    assert econ["profit_per_attempt"] == 20.35
    assert econ["profit_per_hour"] == 305.25
    assert econ["profit_per_hour_div"] == 1.565
    assert econ["expected_attempts_per_hit"] == 4
    assert econ["p_any_sellable"] == 0.25 and econ["expected_value"] == 30
    assert econ["costs"] == {"base": 5.0, **orbs}


def test_economics_missing_inputs_are_null() -> None:
    orbs = {"transmutation": 1.0, "alteration": 0.5, "augmentation": 0.25, "regal": None}
    econ = economics(
        odds_fixture(),
        miss_value=None,
        base=5.0,
        orbs=orbs,
        minutes_per_attempt=4,
        chaos_per_divine=DIV,
    )
    assert econ["expected_value"] is None and econ["cost_per_attempt"] is None
    assert econ["profit_per_attempt"] is None and econ["profit_per_hour"] is None
    assert econ["expected_hit_value"] == 25.0 and econ["p_any_sellable"] == 0.25
    none_hit = economics(
        odds_fixture(p_hit=0.0),
        miss_value=1.0,
        base=None,
        orbs=orbs,
        minutes_per_attempt=4,
        chaos_per_divine=DIV,
    )
    assert none_hit["expected_attempts_per_hit"] is None


# --- the whole service ------------------------------------------------------------------------


async def test_sellable_path(ctx: AppContext, fake: FakeTrade, data: GameData) -> None:
    scenario(fake)
    payload, sources = await cluster_combos(ctx, data, request())
    assert payload["threshold"] == {"divine": 1.0, "chaos": DIV}
    assert payload["notables"] == {
        "count": 5,
        "priceable": 4,
        "legal_pairs": 10,
        "priceable_pairs": 6,
        "unpriceable_pairs": [
            "Brush with Death + Eternal Suffering",
            "Brush with Death + Flow of Life",
            "Brush with Death + Low Tolerance",
            "Brush with Death + Wicked Pall",
        ],
    }
    disc = payload["discovery"]
    assert disc["classes"] == {"not_sellable": 1, "candidate": 3, "thin": 1, "unlisted": 1}
    below = disc["below_threshold"]
    assert below["multi_notable_listings"] == 1  # ES+WP+LT; a non-notable stat is ignored
    assert below["dropped_unknown_currency"] == {"nonsense": 1}
    # The pair searches ran in weight order: ES+WP (348^2), then ES+LT before WP+FL (tie).
    order = [fake.key(b) for b in fake.searches if fake.key(b)[0] == "pair"]
    assert order == [pkey(T_ES, T_WP), pkey(T_ES, T_LT), pkey(T_FL, T_WP)]
    conf = payload["confirmation"]
    assert (conf["candidates"], conf["live_searches"], conf["sellable"]) == (3, 3, 2)
    assert conf["unconfirmed"] == [] and conf["stopped"] is None and "hint" not in conf

    rows = payload["sellable_pairs"]
    names = [r["pair"] for r in rows]
    assert names == ["Eternal Suffering + Wicked Pall", "Flow of Life + Wicked Pall"]
    top = rows[0]
    assert top["mod_ids"] == [ES, WP] and top["trade_ids"] == [T_ES, T_WP]
    assert (top["p25"], top["value_chaos"], top["listings"]) == (390.0, 370.5, 5)
    assert top["value_div"] == 1.9

    values = {pair(ES, WP): 370.5, pair(FL, WP): 237.5}
    odds = combo_odds(data, "Medium Cluster Jewel", 84, "Chaos Damage over Time", 4, values, 19.0)
    assert top["p_credited"] == pytest.approx(odds.per_pair[pair(ES, WP)], rel=1e-5)
    econ = payload["economics"]
    cost = (
        5
        + ORBS["transmutation"]
        + odds.expected_alterations * ORBS["alteration"]
        + odds.expected_augmentations * ORBS["augmentation"]
        + ORBS["regal"]
    )
    assert econ["p_any_sellable"] == pytest.approx(odds.p_hit, rel=1e-5)
    assert econ["expected_attempts_per_hit"] == pytest.approx(1 / odds.p_hit, rel=1e-5)
    assert econ["miss_value"] == 19.0
    assert econ["costs"]["base"] == 5.0
    assert econ["expected_value"] == pytest.approx(odds.expected_value, abs=0.01)
    assert econ["cost_per_attempt"] == pytest.approx(cost, abs=0.01)
    assert econ["profit_per_attempt"] == pytest.approx(odds.expected_value - cost, abs=0.01)
    assert econ["profit_per_hour"] == pytest.approx(15 * (odds.expected_value - cost), abs=0.01)
    assert (payload["miss_listings"], payload["base_listings"]) == (10, 10)
    assert fake.searched("miss") == 1 and fake.searched("base") == 1
    miss = next(b for b in fake.searches if fake.key(b) == "miss")
    anchors = [f["id"] for f in miss["query"]["stats"][1]["filters"]]
    assert anchors == sorted([T_ES, T_FL, T_WP])

    assert any("Brush with Death cannot be priced" in w for w in payload["warnings"])
    assert any("Only instant-buyout" in a for a in payload["assumptions"])
    assert any("Anchor cycle" in a for a in payload["assumptions"])
    names = [s["name"] for s in sources]
    assert names[:2] == ["poe.ninja leagues", "poe.ninja Currency"]
    assert names.count("pathofexile.com trade") == 7  # 2 discovery + 3 pairs + miss + base
    assert all(s.get("instant_buyout") for s in sources if s["name"].startswith("pathofexile"))


async def test_confirmation_is_cache_first_and_continues(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    scenario(fake)
    first, _ = await cluster_combos(ctx, data, request(max_pair_searches=1))
    conf = first["confirmation"]
    assert (conf["live_searches"], conf["from_cache"], conf["unconfirmed_count"]) == (1, 0, 2)
    assert conf["stopped"] == "max_pair_searches (1) live searches used"
    assert "call again" in conf["hint"]
    assert [r["pair"] for r in conf["unconfirmed"]] == [
        "Eternal Suffering + Low Tolerance",
        "Flow of Life + Wicked Pall",
    ]
    assert [r["pair"] for r in first["sellable_pairs"]] == ["Eternal Suffering + Wicked Pall"]
    assert any("confirmation stopped early" in w for w in first["warnings"])
    assert len(fake.searches) == 5  # 2 discovery, 1 pair, miss, base

    second, _ = await cluster_combos(ctx, data, request(max_pair_searches=1))
    conf = second["confirmation"]
    assert (conf["live_searches"], conf["from_cache"], conf["unconfirmed_count"]) == (1, 1, 1)
    assert len(fake.searches) == 6  # only ES+LT; discovery, ES+WP, miss and base were cached

    third, _ = await cluster_combos(ctx, data, request(max_pair_searches=1))
    conf = third["confirmation"]
    assert (conf["live_searches"], conf["from_cache"], conf["unconfirmed_count"]) == (1, 2, 0)
    assert len(third["sellable_pairs"]) == 2
    assert fake.searched(pkey(T_ES, T_WP)) == 1 and fake.searched("below") == 1


async def test_deadline_stops_confirmation(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    """Searches are paced 2 s apart: discovery at 0 s and 2 s, the first pair search would
    start at 4 s, past max_seconds = 3."""
    scenario(fake)
    payload, _ = await cluster_combos(ctx, data, request(max_seconds=3))
    conf = payload["confirmation"]
    assert conf["stopped"] == "max_seconds time budget spent"
    assert (conf["live_searches"], conf["unconfirmed_count"]) == (0, 3)
    assert "hint" in conf
    assert payload["economics"] is None and payload["sellable_pairs"] == []
    assert fake.searched("below") == 1 and fake.searched("above") == 1


async def test_rate_limit_stops_confirmation(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    scenario(fake)
    fake.fail[pkey(T_ES, T_WP)] = httpx.Response(429, headers={"Retry-After": "60"})
    payload, _ = await cluster_combos(ctx, data, request())
    conf = payload["confirmation"]
    assert conf["stopped"].startswith("trade site rate limited; retry in about")
    assert conf["unconfirmed_count"] == 3
    stopped = "confirmation stopped early: trade site rate limited"
    assert any(stopped in w for w in payload["warnings"])


async def test_failed_pair_search_is_reported_and_skipped(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    scenario(fake)
    fake.fail[pkey(T_ES, T_LT)] = httpx.Response(400, json={"error": {"message": "bad"}})
    payload, _ = await cluster_combos(ctx, data, request())
    conf = payload["confirmation"]
    assert conf["live_searches"] == 3 and conf["sellable"] == 2
    assert [e["pair"] for e in conf["errors"]] == ["Eternal Suffering + Low Tolerance"]
    assert "HTTP 400" in conf["errors"][0]["error"]


async def test_zero_sellable_shows_why(ctx: AppContext, fake: FakeTrade, data: GameData) -> None:
    scenario(fake)
    fake.responses[pkey(T_ES, T_WP)] = [(100, "chaos", (T_ES, T_WP))] * 5
    fake.responses[pkey(T_FL, T_WP)] = [(1, "divine", (T_WP, T_FL))] * 4
    payload, _ = await cluster_combos(ctx, data, request())
    assert payload["sellable_pairs"] == [] and payload["economics"] is None
    assert payload["note"] == "no pair confirmed sellable at 1 divine or more"
    assert payload["confirmation"]["not_sellable"] == 3
    (low,) = payload["top_not_sellable"]
    assert low["pair"] == "Flow of Life + Low Tolerance"
    assert (low["listings_below"], low["cheapest_below"]) == (3, 50.0)
    candidates = payload["top_candidates"]
    # Dearest confirmed value first: ES+LT 285 (only 2 listings), WP+FL 185.25, ES+WP 95.
    assert [c["pair"] for c in candidates] == [
        "Eternal Suffering + Low Tolerance",
        "Flow of Life + Wicked Pall",
        "Eternal Suffering + Wicked Pall",
    ]
    assert candidates[0]["confirmed"]["listings"] == 2
    assert candidates[1]["confirmed"]["value_chaos"] == 185.25
    assert candidates[2]["listings_at_or_above"] == 2
    assert candidates[2]["cheapest_at_or_above"] == 390.0
    assert fake.searched("miss") == 0 and fake.searched("base") == 0


async def test_truncated_discovery_warns(ctx: AppContext, fake: FakeTrade, data: GameData) -> None:
    scenario(fake)
    fake.totals["below"] = 400
    fake.totals["above"] = 250
    payload, _ = await cluster_combos(ctx, data, request())
    warnings = payload["warnings"]
    assert any("below the threshold fetched only 8 of 400" in w for w in warnings)
    assert any("most expensive pairs" in w and "4 of 250" in w for w in warnings)


async def test_missing_extras_leave_nulls(ctx: AppContext, fake: FakeTrade, data: GameData) -> None:
    scenario(fake)
    fake.fail["miss"] = httpx.Response(500)
    fake.responses["base"] = []
    payload, _ = await cluster_combos(ctx, data, request())
    econ = payload["economics"]
    assert len(payload["sellable_pairs"]) == 2 and econ["p_any_sellable"] > 0
    assert econ["miss_value"] is None and econ["expected_value"] is None
    assert econ["costs"]["base"] is None and econ["cost_per_attempt"] is None
    assert econ["profit_per_attempt"] is None and econ["profit_per_hour"] is None
    assert any(w.startswith("miss value unavailable") for w in payload["warnings"])
    assert any(w.startswith("white base price unavailable") for w in payload["warnings"])


async def test_lower_ilvl_warns_when_base_pool_differs(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    """At ilvl 50 only Eternal Suffering and Wicked Pall roll; a base up to 83 adds three."""
    scenario(fake)
    payload, _ = await cluster_combos(ctx, data, request(ilvl=50))
    assert payload["notables"]["legal_pairs"] == 1
    assert any("mod pool at 83 differs from item level 50" in w for w in payload["warnings"])
    base = next(b for b in fake.searches if fake.key(b) == "base")
    assert base["query"]["filters"]["misc_filters"]["filters"]["ilvl"] == {"min": 50, "max": 83}


async def test_small_cluster_has_no_pairs(ctx: AppContext, fake: FakeTrade, data: GameData) -> None:
    payload, sources = await cluster_combos(
        ctx, data, request(base="Small Cluster Jewel", enchant="Life", passives=2)
    )
    assert payload["legal_pairs"] == 0 and payload["economics"] is None
    assert "no pairs" in payload["note"]
    assert any("Small Cluster Jewel holds one notable" in w for w in payload["warnings"])
    assert fake.searches == [] and sources == []


async def test_no_priceable_pair(ctx: AppContext, fake: FakeTrade, data: GameData) -> None:
    bare = replace(data, trade_stats={})
    payload, _ = await cluster_combos(ctx, bare, request())
    assert payload["notables"]["priceable_pairs"] == 0
    assert "nothing to price" in payload["note"]
    assert fake.searches == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"min_value_div": 0}, "min_value_div"),
        ({"min_listings": 0}, "min_listings"),
        ({"min_listings": 101}, "min_listings must be 1-100"),
        ({"discovery_sample": 101}, "discovery_sample"),
        ({"max_pair_searches": -1}, "max_pair_searches"),
        ({"minutes_per_attempt": float("inf")}, "minutes_per_attempt"),
    ],
)
async def test_bad_arguments(
    ctx: AppContext, data: GameData, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await cluster_combos(ctx, data, request(**overrides))


# --- review fixes -----------------------------------------------------------------------------

NOT_LT_FL = {"type": "not", "filters": [{"id": T_LT}, {"id": T_FL}]}  # sorted trade ids
NOT_OTHERS = {"type": "not", "filters": [{"id": T_LT}, {"id": T_FL}, {"id": T_BR}]}
EVERY = [T_WP, T_ES, T_LT, T_FL]


def test_pair_and_miss_queries_exclude_other_notables() -> None:
    both = {"type": "and", "filters": [{"id": T_WP}, {"id": T_ES}]}
    assert pair_query(JEWEL, T_WP, T_ES, EVERY)["query"]["stats"][1:] == [both, NOT_LT_FL]
    one = {"type": "count", "value": {"min": 1, "max": 1}, "filters": [{"id": T_WP}, {"id": T_ES}]}
    assert miss_query(JEWEL, [T_ES, T_WP], EVERY)["query"]["stats"][1:] == [one, NOT_LT_FL]
    assert len(pair_query(JEWEL, T_WP, T_ES, [T_WP, T_ES])["query"]["stats"]) == 2


async def test_three_notable_jewels_never_confirm_a_pair(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    """One ES+WP listing and two ES+WP+LT ones: only one listing of the pair, so it is not
    sellable at min_listings 3."""
    scenario(fake)
    fake.responses[pkey(T_ES, T_WP)] = [(2, "divine", (T_ES, T_WP))] + [
        (5, "divine", (T_ES, T_WP, T_LT))
    ] * 2
    payload, _ = await cluster_combos(ctx, data, request())
    sent = next(b for b in fake.searches if fake.key(b) == pkey(T_ES, T_WP))
    assert sent["query"]["stats"][2] == NOT_OTHERS
    assert [r["pair"] for r in payload["sellable_pairs"]] == ["Flow of Life + Wicked Pall"]
    miss = next(b for b in fake.searches if fake.key(b) == "miss")
    # Only FL+WP sold, so FL and WP are the anchors; ES and LT are excluded.
    not_anchor = {"type": "not", "filters": [{"id": T_LT}, {"id": T_BR}, {"id": T_ES}]}
    assert miss["query"]["stats"][2] == not_anchor


async def test_unconvertible_miss_and_base_stay_unknown(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    scenario(fake)
    fake.responses["miss"] = [(20, "nonsense", (T_ES,))] * 3
    fake.responses["base"] = [(5, "nonsense", ())] * 2
    payload, _ = await cluster_combos(ctx, data, request())
    econ = payload["economics"]
    assert econ["miss_value"] is None and econ["expected_value"] is None
    assert econ["costs"]["base"] is None and econ["profit_per_attempt"] is None
    warnings = payload["warnings"]
    assert any(
        w.startswith("miss value unavailable (the search reports 3 listing(s) but none of the 3")
        and "3 nonsense" in w
        for w in warnings
    )
    base_warning = "white base price unavailable (the search reports 2 listing(s)"
    assert any(base_warning in w for w in warnings)
    assert not any("valued at 0" in w for w in warnings)


async def test_truly_empty_miss_is_worth_zero(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    scenario(fake)
    fake.responses["miss"] = []
    payload, _ = await cluster_combos(ctx, data, request())
    econ = payload["economics"]
    assert econ["miss_value"] == 0 and econ["profit_per_attempt"] is not None
    assert any("misses are valued at 0" in w for w in payload["warnings"])


async def test_pair_sample_covers_min_listings(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    """trade_sample 2 would fetch two of ES+WP's five listings; min_listings 3 needs three."""
    ctx.config = ctx.config.model_copy(update={"trade_sample": 2})
    scenario(fake)
    payload, _ = await cluster_combos(ctx, data, request())
    rows = {r["pair"]: r for r in payload["sellable_pairs"]}
    assert rows["Eternal Suffering + Wicked Pall"]["listings"] == 3
    assert request(min_listings=150).pair_sample(30) == 100
    assert request(min_listings=3).pair_sample(30) == 30


async def test_stale_fallback_never_confirms(
    ctx: AppContext, fake: FakeTrade, data: GameData, clock: FakeClock
) -> None:
    """Expired cached quote + HTTP 429: listings_for serves the stale copy, which must not
    count as a confirmation."""
    scenario(fake)
    await cluster_combos(ctx, data, request())
    clock.advance(25 * 60)
    fake.fail[pkey(T_ES, T_WP)] = httpx.Response(429, headers={"Retry-After": "60"})
    payload, _ = await cluster_combos(ctx, data, request())
    conf = payload["confirmation"]
    assert "stale cached copy" in conf["stopped"]
    assert (conf["from_cache"], conf["sellable"], conf["unconfirmed_count"]) == (0, 0, 3)
    assert "hint" in conf and payload["sellable_pairs"] == [] and payload["economics"] is None


async def test_stale_miss_and_base_are_not_used(
    ctx: AppContext, fake: FakeTrade, data: GameData, clock: FakeClock
) -> None:
    scenario(fake)
    await cluster_combos(ctx, data, request())
    clock.advance(25 * 60)
    fake.fail["miss"] = httpx.Response(500)
    fake.fail["base"] = httpx.Response(500)
    payload, _ = await cluster_combos(ctx, data, request())
    assert len(payload["sellable_pairs"]) == 2
    econ = payload["economics"]
    assert econ["miss_value"] is None and econ["costs"]["base"] is None
    assert econ["profit_per_attempt"] is None
    stale = [w for w in payload["warnings"] if "stale cached copy" in w]
    assert len(stale) == 2


def test_pool_change_counts_non_notable_mods(data: GameData) -> None:
    info = cluster_notables(data, "Medium Cluster Jewel", 84, "Chaos Damage over Time", 4)
    small = next(e for e in info.pool if not is_notable(e.mod) and e.weight > 0)
    fewer = replace(info, pool=tuple(e for e in info.pool if e is not small))
    assert pool_change(fewer, info) == f"adds: {small.mod.id}"
    assert pool_change(info, info) is None
    doubled = tuple(replace(e, multiplier=2 * e.multiplier) if e is small else e for e in info.pool)
    assert pool_change(info, replace(info, pool=doubled)) == f"reweights: {small.mod.id}"


# --- review round 2 ---------------------------------------------------------------------------

EYE = ("explicit.stat_156080652", "explicit.stat_4291066912")  # Evil Eye, shared ids


def test_tally_counts_any_other_notable_as_a_third() -> None:
    mods = {T_WP: WP, T_ES: ES}
    listings = [
        {"amount": 1, "currency": "chaos", "explicit_stats": [T_ES, T_WP]},
        {"amount": 2, "currency": "chaos", "explicit_stats": [T_ES, T_WP, T_LT]},
        {"amount": 3, "currency": "chaos", "explicit_stats": [T_WP, T_LT]},
    ]
    found = tally(listings, {"chaos": 1.0}, mods, total=3, other_ids=[T_LT, T_WP])
    assert found.pairs == {pair(ES, WP): [1.0]}
    assert (found.multi_notable_listings, found.unmatched_listings) == (1, 1)


async def test_higher_ilvl_notables_are_excluded(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    """At ilvl 50 only ES and WP roll, but sale searches take any ilvl: an ES+WP+LT jewel
    (LT unlocks at 68) must not count as an ES+WP listing."""
    scenario(fake)
    fake.responses[pkey(T_ES, T_WP)] = [(2, "divine", (T_ES, T_WP))] + [
        (5, "divine", (T_ES, T_WP, T_LT))
    ] * 2
    payload, _ = await cluster_combos(ctx, data, request(ilvl=50))
    sent = next(b for b in fake.searches if fake.key(b) == pkey(T_ES, T_WP))
    assert sent["query"]["stats"][2] == NOT_OTHERS  # LT, FL, Brush unlock at 68
    assert payload["sellable_pairs"] == [] and payload["confirmation"]["not_sellable"] == 1
    assert payload["discovery"]["below_threshold"]["multi_notable_listings"] == 1


async def test_ambiguous_ids_join_the_exclusion(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    """A notable with no single trade id (here Brush with Death given Evil Eye's two shared
    ids) cannot be priced, but every one of its ids is excluded from pair and miss searches."""
    eyed = replace(
        data,
        explicit_trade_ids={
            **data.explicit_trade_ids,
            "local_affliction_notable_brush_with_death": EYE,
        },
    )
    scenario(fake)
    payload, _ = await cluster_combos(ctx, eyed, request())
    everything_else = {"type": "not", "filters": [{"id": i} for i in (T_LT, T_FL, *EYE)]}
    sent = next(b for b in fake.searches if fake.key(b) == pkey(T_ES, T_WP))
    assert sent["query"]["stats"][2] == everything_else
    miss = next(b for b in fake.searches if fake.key(b) == "miss")
    assert miss["query"]["stats"][2] == {
        "type": "not",
        "filters": [{"id": i} for i in (T_LT, *EYE)],  # ES, FL and WP are the anchors
    }
    assert payload["notables"]["priceable"] == 4
    # Brush has explicit ids (shared ones), so the unidentifiable-notable guard stays quiet.
    assert "note" not in payload and payload["economics"] is not None
    assert not any(
        "cannot rule out" in w or "no explicit trade id" in w for w in payload["warnings"]
    )


async def test_miss_with_listings_that_vanish_is_unknown(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    """The search reports 10 listings but none can be fetched: not "none listed"."""
    scenario(fake)
    fake.responses["miss"] = []
    fake.totals["miss"] = 10
    payload, _ = await cluster_combos(ctx, data, request())
    econ = payload["economics"]
    assert econ["miss_value"] is None and econ["expected_value"] is None
    assert econ["profit_per_attempt"] is None
    assert any(
        "the search reports 10 listing(s) but none of the 0" in w for w in payload["warnings"]
    )
    assert not any("valued at 0" in w for w in payload["warnings"])


async def test_stale_ninja_rates_alone_do_not_stop_confirmation(
    ctx: AppContext, api: NinjaApi, fake: FakeTrade, data: GameData, clock: FakeClock
) -> None:
    scenario(fake)
    await cluster_combos(ctx, data, request())
    clock.advance(25 * 60)  # pair searches expire; poe.ninja is due again and fails
    api.fail("Currency", 500)
    payload, _ = await cluster_combos(ctx, data, request())
    conf = payload["confirmation"]
    assert conf["stopped"] is None and conf["live_searches"] == 3 and conf["sellable"] == 2
    assert payload["economics"]["profit_per_attempt"] is not None
    warnings = payload["warnings"]
    assert any(w.startswith("poe.ninja currency rates are an old cached copy") for w in warnings)
    assert not any("stale cached copy" in w for w in warnings)


# --- review round 3 ---------------------------------------------------------------------------


async def test_unidentifiable_notable_blocks_confirmation(
    ctx: AppContext, fake: FakeTrade, data: GameData
) -> None:
    """Brush with Death without any explicit trade id: an A+B+Brush jewel would pass every
    pair search, so nothing is confirmed or valued and no pair, miss or base search runs."""
    blind = replace(
        data,
        explicit_trade_ids={k: v for k, v in data.explicit_trade_ids.items() if k != BRUSH_STAT},
    )
    scenario(fake)
    payload, sources = await cluster_combos(ctx, blind, request())
    assert {fake.key(b) for b in fake.searches} == {"below", "above"}
    assert len(fake.searches) == 2  # discovery only: no pair, miss or base search
    assert payload["sellable_pairs"] == [] and payload["economics"] is None
    assert payload["note"].startswith("cannot rule out jewels carrying Brush with Death")
    assert "refresh the game data export" in payload["note"]
    assert payload["discovery"]["classes"]["candidate"] == 3
    assert payload["unconfirmed_count"] == 3
    assert [r["pair"] for r in payload["unconfirmed"]] == [
        "Eternal Suffering + Wicked Pall",
        "Eternal Suffering + Low Tolerance",
        "Flow of Life + Wicked Pall",
    ]
    assert "confirmation" not in payload
    assert [s["name"] for s in sources].count("pathofexile.com trade") == 2


async def test_stale_league_lookup_is_not_stale_rates(
    ctx: AppContext, api: NinjaApi, fake: FakeTrade, data: GameData, clock: FakeClock
) -> None:
    scenario(fake)
    await cluster_combos(ctx, data, request())
    clock.advance(25 * 60)  # poe.ninja is due again; only the league lookup fails
    api.fail("leagues", 500)
    payload, _ = await cluster_combos(ctx, data, request())
    warnings = payload["warnings"]
    assert any(w.startswith("the poe.ninja league lookup is an old cached copy") for w in warnings)
    assert not any("currency rates are an old cached copy" in w for w in warnings)
    assert payload["confirmation"]["stopped"] is None
