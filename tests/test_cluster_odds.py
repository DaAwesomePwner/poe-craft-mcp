"""Cluster jewel odds and the alt_aug / regal methods, checked by hand on fixture weights.

Fixture weights (cut from the RePoE 3.30.0.4.3 export, tests/fixtures/gamedata):
Medium Cluster Jewel, enchant "Chaos Damage over Time"
(tag affliction_chaos_damage_over_time_multiplier), ilvl 84:
  prefixes WickedPall 348, EternalSuffering 348, LowTolerance 130, FlowofLife 130,
           BrushwithDeath 130, SmallPassivesHaveIncreasedEffect 500, GrantLife_ 500 = 2086
  suffixes GrantFireRes 500, GrantLifeRegen 500 = 1000          (all = 3086)
  PressurePoints, OverwhelmingMalice, GrantCriticalStrikeMultiplier, GrantAttackSpeed and
  GrantCastSpeed need other enchants (spawn weight 0 here).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from poe_craft_mcp.config import Config
from poe_craft_mcp.gamedata.loader import load_gamedata
from poe_craft_mcp.gamedata.models import GameData, GameDataError
from poe_craft_mcp.gamedata.odds import mod_odds
from poe_craft_mcp.server import build_server

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "gamedata"
MEDIUM, DOT = "Medium Cluster Jewel", "Chaos Damage over Time"
WP, ES, LT = (
    "AfflictionNotableWickedPall_",
    "AfflictionNotableEternalSuffering",
    "AfflictionNotableLowTolerance",
)
PREFIX, SUFFIX = 2086, 1000
ALL = PREFIX + SUFFIX


@pytest.fixture(scope="module")
def data() -> GameData:
    return load_gamedata(FIXTURE_DIR)


def odds(data: GameData, targets: list[str], method: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("enchant", DOT)
    return mod_odds(data, MEDIUM, 84, targets, method, **kw)


def test_alt_one_chaos_dot_notable_matches_hand_calculation(data: GameData) -> None:
    one_affix = 348 / ALL
    # Two affixes: Wicked Pall first, or a suffix first and then Wicked Pall among the prefixes.
    two_affixes = 348 / ALL + SUFFIX / ALL * 348 / PREFIX
    expected = 0.5 * one_affix + 0.5 * two_affixes
    result = odds(data, [WP], "alt", passives=4)
    assert result["chance_per_attempt"] == pytest.approx(expected, abs=1e-8)
    assert result["weight_table"]["totals"] == {
        "prefix": PREFIX,
        "suffix": SUFFIX,
        "all": ALL,
        "targets": 348,
    }
    assert result["expected_materials"] == {"alteration": pytest.approx(1 / expected, abs=1e-4)}
    assert result["enchant"] == {
        "id": "affliction_chaos_damage_over_time_multiplier",
        "name": DOT,
        "tag": "affliction_chaos_damage_over_time_multiplier",
        "text": ["12% increased Chaos Damage over Time"],
        "passives": 4,
        "passive_range": [4, 6],
    }
    assert any("cluster_jewels.json" in a for a in result["assumptions"])
    assert result["warnings"] == []


def test_enchant_excludes_notable_with_reason(data: GameData) -> None:
    result = odds(data, ["AfflictionNotablePressurePoints__"], "alt")
    assert result["chance_per_attempt"] == 0
    (warning,) = result["warnings"]
    assert "spawn weight 0 on Medium Cluster Jewel" in warning
    assert "affliction_chaos_damage_over_time_multiplier" in warning
    crit = odds(data, ["AfflictionNotablePressurePoints__"], "alt", enchant="critical chance")
    assert crit["chance_per_attempt"] > 0


def test_alt_aug_on_cluster_matches_hand_calculation(data: GameData) -> None:
    # A cycle succeeds like a 2-affix Alteration. An Augmentation is used only when the
    # Alteration gave one suffix (1000 of 3086): a prefix target can still roll. After one
    # non-target prefix the open side is suffix, which holds no target, so no Augmentation.
    p = 348 / ALL + SUFFIX / ALL * 348 / PREFIX
    augs_per_cycle = 0.5 * SUFFIX / ALL
    result = odds(data, [WP], "alt_aug")
    assert result["method"] == "alt_aug"
    assert result["chance_per_attempt"] == pytest.approx(p, abs=1e-8)
    assert result["expected_materials"] == {
        "alteration": pytest.approx(1 / p, abs=1e-4),
        "augmentation": pytest.approx(augs_per_cycle / p, abs=1e-4),
    }
    assert result["calculation"] == {"method": "exact", "resolved": True}
    assert any("Augmentation only when" in a for a in result["assumptions"])


def test_alt_aug_on_normal_item(data: GameData) -> None:
    # Two-Stone Ring ilvl 1: prefixes 1000 + 500, suffixes Strength1 1000, FireResist1 1000,
    # AllAttributes1 400 (3900 in all). Augmentation after a lone prefix (1500 of 3900).
    p = 1000 / 3900 + 1500 / 3900 * 1000 / 2400
    result = mod_odds(data, "Two-Stone Ring", 1, ["Strength1"], "alt_aug")
    assert result["chance_per_attempt"] == pytest.approx(p, abs=1e-8)
    assert result["expected_materials"]["augmentation"] == pytest.approx(
        0.5 * 1500 / 3900 / p, abs=1e-4
    )
    assert result["enchant"] is None


def test_regal_with_existing_mods_on_cluster(data: GameData) -> None:
    # Existing Wicked Pall + Fire Res block their groups; the rare jewel allows 2 + 2, so the
    # Regal draws from prefixes 2086 - 348 = 1738 and suffixes 1000 - 500 = 500.
    existing = [WP, "AfflictionJewelSmallPassivesGrantFireRes"]
    result = odds(data, [ES, LT], "regal", existing_mods=existing)
    p = (348 + 130) / (1738 + 500)
    assert result["chance_per_attempt"] == pytest.approx(p, abs=1e-8)
    assert result["affix_limits"] == {"prefix": 2, "suffix": 2}
    assert result["existing_mods"] == existing
    assert result["expected_materials"] == {"regal": pytest.approx(1 / p, abs=1e-4)}
    assert any("Rare jewels have at most 2 prefixes" in a for a in result["assumptions"])


def test_regal_on_a_full_side(data: GameData) -> None:
    # Helical Ring as rare: 1 prefix, 4 suffixes. The existing prefix fills the prefix side,
    # so the Regal adds one of the suffixes Strength1 1000, FireResist1 1000, AllAttributes1 400.
    result = mod_odds(
        data, "Helical Ring", 1, ["Strength1"], "regal", existing_mods=["IncreasedLife0"]
    )
    assert result["chance_per_attempt"] == pytest.approx(1000 / 2400, abs=1e-8)
    assert result["weight_table"]["totals"]["prefix"] == 0
    blocked = mod_odds(
        data, "Helical Ring", 1, ["AddedFireDamage1"], "regal", existing_mods=["IncreasedLife0"]
    )
    assert blocked["chance_per_attempt"] == 0
    assert any("existing mod(s) fill every prefix slot" in w for w in blocked["warnings"])
    # Two-Stone Ring ilvl 5: IncreasedLife0 and FireResist1 block IncreasedLife1 and themselves,
    # leaving AddedFireDamage1 500, Strength1 1000, AllAttributes1 400.
    ring = mod_odds(
        data,
        "Two-Stone Ring",
        5,
        ["Strength1"],
        "regal",
        existing_mods=["IncreasedLife0", "FireResist1"],
    )
    assert ring["chance_per_attempt"] == pytest.approx(1000 / 1900, abs=1e-8)


def test_small_cluster_holds_one_notable(data: GameData) -> None:
    # Flow of Life adds the tag has_affliction_notable; notables' generation_weights list it
    # (weight 0) before expansion_jewel_small, so the Regal cannot add a second notable.
    existing = ["AfflictionNotableFlowofLife_", "AfflictionJewelSmallPassivesGrantLifeRegenSmall"]
    result = mod_odds(
        data,
        "Small Cluster Jewel",
        84,
        ["AfflictionNotableBrushwithDeath"],
        "regal",
        enchant="Life",
        existing_mods=existing,
    )
    assert result["chance_per_attempt"] == 0
    assert any(
        "generation weight 0" in w and "has_affliction_notable" in w for w in result["warnings"]
    )


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"existing_mods": None}, "needs existing_mods"),
        ({"existing_mods": [ES]}, "already among existing_mods"),
        ({"existing_mods": [WP, LT]}, "at most 1 prefix"),
    ],
)
def test_regal_input_errors(data: GameData, kw: dict[str, Any], message: str) -> None:
    with pytest.raises(GameDataError, match=message):
        odds(data, [ES], "regal", **kw)


def test_existing_mods_ignored_outside_regal(data: GameData) -> None:
    result = odds(data, [WP], "alt", existing_mods=[ES])
    assert result["existing_mods"] == []
    assert "existing_mods ignored" in result["warnings"][0]


@pytest.mark.parametrize(
    ("enchant", "message"),
    [
        (None, "needs `enchant`.*Chaos Damage over Time"),
        ("Damage", "ambiguous"),
        ("Chaso dmg", "Closest: Chaos Damage over Time"),
    ],
)
def test_enchant_errors(data: GameData, enchant: str | None, message: str) -> None:
    with pytest.raises(GameDataError, match=message):
        odds(data, [WP], "alt", enchant=enchant)


def test_enchant_forms_and_other_rules(data: GameData) -> None:
    for form in (
        "chaos damage over time",
        "12% increased Chaos Damage over Time",
        "Added Small Passive Skills grant: 12% increased Chaos Damage over Time",
        "affliction_chaos_damage_over_time_multiplier",
    ):
        assert odds(data, [WP], "alt", enchant=form)["enchant"]["name"] == DOT
    with pytest.raises(GameDataError, match="adds 4-6 passive skills"):
        odds(data, [WP], "alt", passives=8)
    legacy = odds(data, [WP], "alt", enchant="Aura Effect")
    assert any("legacy tag" in w for w in legacy["warnings"])
    with pytest.raises(GameDataError, match="rare items are supported for domains: item"):
        odds(data, [WP], "chaos")
    ring = mod_odds(data, "Two-Stone Ring", 1, ["Strength1"], "alt", enchant="x", passives=3)
    assert ring["warnings"] == [
        "enchant ignored: Two-Stone Ring is not a cluster jewel",
        "passives ignored: Two-Stone Ring is not a cluster jewel",
    ]


async def test_tool_accepts_enchant_and_existing_mods(config: Config) -> None:
    shutil.copytree(FIXTURE_DIR, config.data_dir / "gamedata")
    args = {
        "base": MEDIUM,
        "ilvl": 84,
        "target_mods": [ES],
        "method": "regal",
        "enchant": DOT,
        "existing_mods": [WP],
    }
    async with Client(build_server(config)) as client:
        result = await client.call_tool("mod_odds", args)
    assert not result.is_error
    out: dict[str, Any] = result.structured_content or {}
    # Existing Wicked Pall (prefix): 1738 prefix weight left + 1000 suffix weight.
    assert out["chance_per_attempt"] == pytest.approx(348 / (1738 + 1000), abs=1e-8)
    assert out["enchant"]["tag"] == "affliction_chaos_damage_over_time_multiplier"
