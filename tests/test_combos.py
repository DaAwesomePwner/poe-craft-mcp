"""Sellable cluster combos: trade ids, legal notable pairs and exact combo odds.

Fixture data (tests/fixtures/gamedata, cut from the RePoE 3.30.0.4.3 export) covers parsing
and the pair rules; the odds are checked on small synthetic pools whose values are derived by
hand in each test.
"""

from __future__ import annotations

import math
import shutil
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from poe_craft_mcp.gamedata.combo_pairs import NOTABLE_TAG, pair_problem
from poe_craft_mcp.gamedata.combos import best_pair, cluster_notables, combo_odds
from poe_craft_mcp.gamedata.loader import load_gamedata
from poe_craft_mcp.gamedata.method import RARE_JEWEL
from poe_craft_mcp.gamedata.models import (
    BaseItem,
    ClusterEnchant,
    ClusterJewel,
    GameData,
    GameDataError,
    ModDef,
    all_explicit_trade_ids,
    trade_stat_for,
)
from poe_craft_mcp.gamedata.records import parse_clusters

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "gamedata"
WP, ES = "AfflictionNotableWickedPall_", "AfflictionNotableEternalSuffering"
THAUM, RUSH = "AfflictionNotableThaumophage", "AfflictionNotableEssenceRush"
FLOW, BRUSH = "AfflictionNotableFlowofLife_", "AfflictionNotableBrushwithDeath"


@pytest.fixture(scope="module")
def data() -> GameData:
    return load_gamedata(FIXTURE_DIR)


def pair(*ids: str) -> frozenset[str]:
    return frozenset(ids)


# --- trade stat ids ---------------------------------------------------------------------------


def test_trade_stat_map(data: GameData) -> None:
    assert data.mods[WP].stat_ids == ("local_affliction_notable_wicked_pall",)
    assert data.trade_stats["local_affliction_notable_wicked_pall"] == "explicit.stat_1616734644"
    assert trade_stat_for(data, data.mods[THAUM]) == "explicit.stat_177215332"
    # Evil Eye has two explicit trade ids, the memory line two stat ids: both are ambiguous.
    assert "local_affliction_notable_evil_eye" not in data.trade_stats
    assert not any(k.startswith("memory_line_") for k in data.trade_stats)
    assert len(data.trade_stats) == 4
    with pytest.raises(GameDataError, match=f"{RUSH} has no single explicit trade stat"):
        trade_stat_for(data, data.mods[RUSH])
    two_stats = replace(data.mods[WP], stat_ids=("a", "b"))
    with pytest.raises(GameDataError, match="no single explicit trade stat"):
        trade_stat_for(data, two_stats)


def test_trade_stats_optional(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE_DIR, tmp_path / "gd")
    (tmp_path / "gd" / "stat_translations.json").unlink()
    data = load_gamedata(tmp_path / "gd")
    assert data.trade_stats == {}
    message = "stat_translations.json missing or has none for this mod.*fetch"
    with pytest.raises(GameDataError, match=message) as missing:
        trade_stat_for(data, data.mods[WP])
    assert "is not in the export" not in str(missing.value)


def test_empty_trade_stat_map_does_not_claim_the_file_is_missing(data: GameData) -> None:
    emptied = replace(data, trade_stats={})
    with pytest.raises(GameDataError, match="missing or has none for this mod") as raised:
        trade_stat_for(emptied, emptied.mods[WP])
    assert "is not in the export" not in str(raised.value)


def test_all_explicit_trade_ids_include_ambiguous_ones(data: GameData) -> None:
    eye = replace(data.mods[WP], stat_ids=("local_affliction_notable_evil_eye",))
    assert all_explicit_trade_ids(data, eye) == (
        "explicit.stat_156080652",
        "explicit.stat_4291066912",
    )
    assert all_explicit_trade_ids(data, data.mods[WP]) == ("explicit.stat_1616734644",)
    assert all_explicit_trade_ids(data, replace(eye, stat_ids=("nope",))) == ()
    only_single = replace(data, explicit_trade_ids={})  # e.g. GameData built by hand
    assert all_explicit_trade_ids(only_single, data.mods[WP]) == ("explicit.stat_1616734644",)


def test_trade_option_numbering(data: GameData) -> None:
    options = {c.name: [e.trade_option for e in c.enchants] for c in data.clusters.values()}
    # File order: Large 1-3, Medium 4-9, Small 10-15; Strength (14) has no trade option.
    assert options == {
        "Large Cluster Jewel": [1, 2, 3],
        "Medium Cluster Jewel": [4, 5, 6, 7, 8, 9],
        "Small Cluster Jewel": [10, 11, 12, 13, None, 15],
    }
    raw = {
        "b": {
            "name": "B",
            "min_skills": 2,
            "max_skills": 3,
            "passive_skills": [
                {"id": i, "name": i, "tag": tag}
                for i, tag in (
                    ("x", "affliction_x"),
                    ("dex", "affliction_dexterity"),
                    ("int", "affliction_intelligence"),
                    ("y", "affliction_y"),
                )
            ],
        }
    }
    assert [e.trade_option for e in parse_clusters(raw)["b"].enchants] == [1, None, None, 4]


# --- legal pairs ------------------------------------------------------------------------------


def test_medium_pairs_two_prefix_notables(data: GameData) -> None:
    info = cluster_notables(data, "Medium Cluster Jewel", 84, "Chaos Damage over Time")
    ids = [e.mod.id for e in info.notables]
    assert ids == sorted([WP, ES, "AfflictionNotableLowTolerance", FLOW, BRUSH])
    assert len(info.pairs) == 10  # every two of the five prefix notables
    assert NOTABLE_TAG in info.notables[0].mod.adds_tags
    assert "affliction_chaos_damage_over_time_multiplier" in info.item.tags


def test_large_suffix_notables_share_a_group(data: GameData) -> None:
    info = cluster_notables(data, "Large Cluster Jewel", 84, "Spell Damage")
    assert {e.mod.id for e in info.notables} == {THAUM, RUSH}
    assert info.pairs == ()
    assert any("no two notables fit" in w and "Small" not in w for w in info.warnings)


def test_small_holds_one_notable(data: GameData) -> None:
    info = cluster_notables(data, "Small Cluster Jewel", 84, "Life")
    assert {e.mod.id for e in info.notables} == {FLOW, BRUSH}
    assert info.pairs == ()
    assert any("Small Cluster Jewel holds one notable" in w for w in info.warnings)


def test_pair_problem_side_limit(data: GameData) -> None:
    info = cluster_notables(data, "Medium Cluster Jewel", 84, "Chaos Damage over Time")
    one_prefix = replace(RARE_JEWEL, max_prefix=1)
    reason = pair_problem(info.item, 84, data.mods[WP], data.mods[ES], one_prefix)
    assert reason is not None and "both prefixes" in reason


def test_not_a_cluster_jewel(data: GameData) -> None:
    with pytest.raises(GameDataError, match="not a cluster jewel"):
        cluster_notables(data, "Two-Stone Ring", 84, "x")


# --- synthetic pools for exact odds ------------------------------------------------------------

TAG = "synthetic_enchant"
NOTABLE_GEN = (
    ("expansion_jewel_large", 100),
    ("expansion_jewel_medium", 100),
    (NOTABLE_TAG, 0),
    ("expansion_jewel_small", 100),
    ("default", 0),
)


def _mod(mod_id: str, side: str, weight: int, notable: bool = False, group: str = "") -> ModDef:
    return ModDef(
        id=mod_id,
        domain="affliction_jewel",
        generation_type=side,
        required_level=1,
        groups=frozenset({group or mod_id}),
        tags=frozenset(),
        spawn_weights=((TAG, weight), ("default", 0)),
        generation_weights=NOTABLE_GEN if notable else (),
        is_essence_only=False,
        text=mod_id,
        adds_tags=frozenset({NOTABLE_TAG}) if notable else frozenset(),
    )


def synthetic(size: str, mods: list[ModDef]) -> GameData:
    base_id = f"Synthetic{size}"
    tags = frozenset({f"expansion_jewel_{size.lower()}", "jewel", "default"})
    base = BaseItem(base_id, f"{size} Test Jewel", "affliction_jewel", "Jewel", tags, "released")
    enchant = ClusterEnchant("synthetic", "Synthetic", TAG, ("x",), 1)
    cluster = ClusterJewel(base_id, base.name, 2, 12, (enchant,))
    return GameData(
        mods={m.id: m for m in mods},
        bases={base_id: base},
        essences={},
        fossils={},
        game_version="3.30.0",
        downloaded_at=None,
        source_url="test",
        clusters={base_id: cluster},
    )


# Medium: prefix notables N1 1, N2 1, N3 2; small prefix P 2; small suffix S 4 (total 10).
MEDIUM = [
    _mod("N1", "prefix", 1, True),
    _mod("N2", "prefix", 1, True),
    _mod("N3", "prefix", 2, True),
    _mod("P", "prefix", 2),
    _mod("S", "suffix", 4),
]
# Large: prefix notables N1 1, N2 1; suffix notable L1 2 (total 4).
LARGE = [
    _mod("N1", "prefix", 1, True),
    _mod("N2", "prefix", 1, True),
    _mod("L1", "suffix", 2, True, "AfflictionNotableLargeSuffix"),
]


def test_combo_odds_medium_by_hand() -> None:
    """Anchors N1, N2 (sellable pair {N1, N2} at 100c, miss 5c).

    Cycle: X1 anchor 2/10. X1 = S (4/10) then X2 from prefixes (6): anchor 2/6. X1 = P or N3:
    X2 must be the suffix S, never an anchor. p_cycle = 2/10 + 4/10 * 2/6 = 1/3.
    Raw states: {N1} = 1/10 * 1/2 = 1/20 (1-affix Alteration, no Augmentation);
    {N1, S} = 1/10 * 1/2 * 1 + 4/10 * 1/6 = 7/60; same for N2. Given success (x3):
    {N1} 3/20, {N1, S} 7/20, {N2} 3/20, {N2, S} 7/20.
    Regal from {N1}: N2 1 of (N2 1 + N3 2 + P 2 + S 4) = 1/9. From {N1, S}: 1/5 (S blocked).
    p_hit = 2 * (3/20 * 1/9 + 7/20 * 1/5) = 13/75; {N1, N3} rares are misses.
    Alterations 1 / p_cycle = 3; Augmentations: lone S (1/2 * 4/10) can be augmented to an
    anchor prefix, so 0.2 per cycle, 0.6 per attempt.
    """
    data = synthetic("Medium", MEDIUM)
    result = combo_odds(data, "Medium Test Jewel", 84, "Synthetic", 4, {pair("N1", "N2"): 100}, 5)
    assert result.anchors == ("N1", "N2")
    assert result.p_cycle_success == pytest.approx(1 / 3)
    assert result.magic_states == pytest.approx(
        {
            pair("N1"): 3 / 20,
            pair("N1", "S"): 7 / 20,
            pair("N2"): 3 / 20,
            pair("N2", "S"): 7 / 20,
        }
    )
    p_hit = float(Fraction(13, 75))
    assert result.p_hit == pytest.approx(p_hit)
    assert result.per_pair == pytest.approx({pair("N1", "N2"): p_hit})
    assert result.p_miss == pytest.approx(1 - p_hit)
    assert result.expected_alterations == pytest.approx(3)
    assert result.expected_augmentations == pytest.approx(0.6)
    assert result.regals == 1.0
    assert result.expected_hit_value == pytest.approx(100 * p_hit)
    assert result.expected_miss_value == pytest.approx(5 * (1 - p_hit))
    assert result.expected_value == pytest.approx(100 * p_hit + 5 * (1 - p_hit))
    assert any("adds_tags apply only at the Regal step" in a for a in result.assumptions)


def test_miss_value_defaults_to_zero() -> None:
    data = synthetic("Medium", MEDIUM)
    result = combo_odds(data, "Medium Test Jewel", 84, "Synthetic", 4, {pair("N1", "N2"): 100})
    assert result.miss_value is None
    assert result.expected_miss_value == 0
    assert result.expected_value == pytest.approx(100 * 13 / 75)


def test_anchor_without_second_affix_keeps_one_mod() -> None:
    """Only prefixes (N1 1, N2 1, P 2): a 2-affix Alteration has no second mod allowed, so an
    anchor X1 always leaves {X1}; X1 = P fails. p_cycle = 2/4; states {N1} 1/2, {N2} 1/2.
    Regal from {N1}: N2 1 of (N2 1 + P 2) = 1/3, so p_hit = 1/3. No Augmentation is ever
    useful (no suffix can be an anchor)."""
    data = synthetic("Medium", MEDIUM[:2] + [MEDIUM[3]])
    result = combo_odds(data, "Medium Test Jewel", 84, "Synthetic", 4, {pair("N1", "N2"): 1})
    assert result.p_cycle_success == pytest.approx(1 / 2)
    assert result.magic_states == pytest.approx({pair("N1"): 1 / 2, pair("N2"): 1 / 2})
    assert result.p_hit == pytest.approx(1 / 3)
    assert result.expected_augmentations == 0


def test_hit_credited_to_best_pair() -> None:
    """Sellable {N1,N2} 20, {N1,L1} 10, {N2,L1} 30; every notable is an anchor, p_cycle = 1.
    States: X1 = N1 (1/4): {N1} 1/8, {N1, L1} 1/8 (only L1 fits); same for N2.
    X1 = L1 (1/2): {L1} 1/4, {N1, L1} 1/8, {N2, L1} 1/8. So {N1} 1/8, {N2} 1/8, {L1} 1/4,
    {N1, L1} 1/4, {N2, L1} 1/4.
    Regal: {N1} -> N2 1/3 ({N1,N2}), L1 2/3 ({N1,L1}); {N2} -> N1 1/3, L1 2/3 ({N2,L1});
    {L1} -> N1 1/2 ({N1,L1}), N2 1/2 ({N2,L1}); {N1,L1} and {N2,L1} gain the other prefix
    notable: all three notables, credited to {N2,L1} (30), not {N1,L1} or {N1,N2}.
    {N1,N2} = 2 * 1/8 * 1/3 = 1/12; {N1,L1} = 1/8 * 2/3 + 1/4 * 1/2 = 5/24;
    {N2,L1} = 1/8 * 2/3 + 1/4 * 1/2 + 1/4 + 1/4 = 17/24. EV = 20/12 + 50/24 + 510/24 = 25."""
    data = synthetic("Large", LARGE)
    sellable = {pair("N1", "N2"): 20, pair("N1", "L1"): 10, pair("N2", "L1"): 30}
    result = combo_odds(data, "Large Test Jewel", 84, "Synthetic", 8, sellable)
    assert result.p_cycle_success == pytest.approx(1)
    assert result.per_pair == pytest.approx(
        {pair("N1", "N2"): 1 / 12, pair("N1", "L1"): 5 / 24, pair("N2", "L1"): 17 / 24}
    )
    assert result.p_hit == pytest.approx(1)
    assert result.p_miss == pytest.approx(0)
    assert result.expected_value == pytest.approx(25)
    assert result.expected_augmentations == 0


def test_best_pair() -> None:
    values = {pair("a", "b"): 5.0, pair("b", "c"): 9.0}
    assert best_pair(pair("a", "b", "c"), values) == pair("b", "c")
    assert best_pair(pair("a", "c"), values) is None
    assert best_pair(pair("a"), values) is None


def test_large_pairs_hold_one_suffix_notable() -> None:
    data = synthetic(
        "Large", [*LARGE, _mod("L2", "suffix", 2, True, "AfflictionNotableLargeSuffix")]
    )
    info = cluster_notables(data, "Large Test Jewel", 84, "Synthetic")
    assert {tuple(sorted(p)) for p in info.pairs} == {
        ("N1", "N2"),
        ("L1", "N1"),
        ("L1", "N2"),
        ("L2", "N1"),
        ("L2", "N2"),
    }


@pytest.mark.parametrize(
    ("base", "enchant", "sellable", "message"),
    [
        ("Large Cluster Jewel", "Spell Damage", {pair(THAUM, RUSH): 1}, "share mod group"),
        ("Small Cluster Jewel", "Life", {pair(FLOW, BRUSH): 1}, "generation weight 0"),
        ("Medium Cluster Jewel", "Chaos Damage over Time", {}, "at least one notable pair"),
        ("Medium Cluster Jewel", "Chaos Damage over Time", {pair(WP): 1}, "two different"),
        (
            "Medium Cluster Jewel",
            "Chaos Damage over Time",
            {pair(WP, "AfflictionNotablePressurePoints__"): 1},
            "cannot roll: .*spawn weight 0",
        ),
        (
            "Medium Cluster Jewel",
            "Chaos Damage over Time",
            {pair(WP, "AfflictionJewelSmallPassivesGrantLife_"): 1},
            "not a cluster notable",
        ),
        ("Medium Cluster Jewel", "Chaos Damage over Time", {pair(WP, "Nope"): 1}, "unknown mod"),
        ("Medium Cluster Jewel", "Chaos Damage over Time", {pair(WP, ES): "x"}, "finite chaos"),
    ],
)
def test_impossible_pairs(
    data: GameData,
    base: str,
    enchant: str,
    sellable: dict[frozenset[str], float],
    message: str,
) -> None:
    with pytest.raises(GameDataError, match=message):
        combo_odds(data, base, 84, enchant, None, sellable)


@pytest.mark.parametrize("miss", [math.inf, -math.inf, math.nan, True, "5"])
def test_miss_value_must_be_finite(miss: object) -> None:
    data = synthetic("Medium", MEDIUM)
    with pytest.raises(GameDataError, match="miss_value must be a finite chaos value"):
        combo_odds(
            data,
            "Medium Test Jewel",
            84,
            "Synthetic",
            4,
            {pair("N1", "N2"): 100},
            miss,  # type: ignore[arg-type]
        )


def test_fixture_combo_odds_sum_to_one(data: GameData) -> None:
    result = combo_odds(
        data, "Medium Cluster Jewel", 84, "Chaos Damage over Time", 4, {pair(WP, ES): 50}
    )
    assert result.p_hit + result.p_miss == pytest.approx(1)
    assert sum(result.magic_states.values()) == pytest.approx(1)
    assert 0 < result.p_hit < 1
