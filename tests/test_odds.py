"""Odds checked against hand calculations on the fixture weights.

Fixture weights used below (from tests/fixtures/gamedata/mods.json):
Two-Stone Ring, ilvl 1: prefixes IncreasedLife0 1000, AddedFireDamage1 500;
                        suffixes Strength1 1000, FireResist1 1000, AllAttributes1 400.
                        ilvl 5 adds IncreasedLife1 1000 (same group as IncreasedLife0).
Vaal Regalia, ilvl 5:   prefixes IncreasedLife0/1 1000 each, AttackerTakesDamage1 1000,
                        LocalIncreasedEnergyShield1 1000, LocalIncreasedEnergyShieldPercent1 1000;
                        suffixes FireResist1 1000, StunRecovery1 1000.
                        ilvl 16 adds ChaosResist1 250.
"""

from __future__ import annotations

import shutil
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from poe_craft_mcp.config import Config
from poe_craft_mcp.gamedata import odds as odds_module
from poe_craft_mcp.gamedata.draws import RARE, Candidate, CountModel, Estimate, Start, monte_carlo
from poe_craft_mcp.gamedata.loader import load_gamedata
from poe_craft_mcp.gamedata.models import GameData, GameDataError
from poe_craft_mcp.gamedata.odds import attempts_for, mod_odds
from poe_craft_mcp.server import build_server

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "gamedata"
RARE_4, RARE_5_OR_6 = 8 / 12, 4 / 12


@pytest.fixture(scope="module")
def data() -> GameData:
    return load_gamedata(FIXTURE_DIR)


def p_last(target: float, others: list[float]) -> float:
    """P(target is drawn after all `others`) in weighted draws without replacement.

    Exponential-race identity: sum over subsets S of others of (-1)^|S| * t / (t + W(S)).
    """
    return sum(
        (-1) ** k * target / (target + sum(subset))
        for k in range(len(others) + 1)
        for subset in combinations(others, k)
    )


def weights(result: dict[str, Any]) -> dict[str, float]:
    return {e["id"]: e["weight"] for e in result["weight_table"]["entries"]}


def test_alt_single_mod_matches_hand_calculation(data: GameData) -> None:
    one_affix = 1000 / 3900
    two_affixes = 1000 / 3900 + (1000 + 500) / 3900 * 1000 / (1000 + 1000 + 400)
    expected = 0.5 * one_affix + 0.5 * two_affixes
    result = mod_odds(data, "Two-Stone Ring", 1, ["Strength1"], "alt")
    assert result["chance_per_attempt"] == pytest.approx(expected, abs=1e-8)
    assert result["expected_attempts"] == pytest.approx(1 / expected, abs=0.01)
    assert result["calculation"]["method"] == "exact"
    assert result["weight_table"]["totals"] == {
        "prefix": 1500,
        "suffix": 2400,
        "all": 3900,
        "targets": 1000,
    }
    assert any("1 or 2 affixes" in a for a in result["assumptions"])


def test_chaos_single_mod_matches_hand_calculation(data: GameData) -> None:
    miss_with_4 = p_last(1000, [1000, 500, 1000, 400])
    expected = RARE_4 * (1 - miss_with_4) + RARE_5_OR_6 * 1
    result = mod_odds(data, "Two-Stone Ring", 1, ["Strength1"], "chaos")
    assert result["chance_per_attempt"] == pytest.approx(expected, abs=1e-8)
    assert result["n90"] == attempts_for(expected)
    assert any("8/12" in a for a in result["assumptions"])


def test_mod_group_blocks_second_mod_from_same_group(data: GameData) -> None:
    group_weight = 1000 + 1000
    expected = RARE_4 * (1 - p_last(group_weight, [500, 1000, 1000, 400])) + RARE_5_OR_6
    result = mod_odds(data, "Two-Stone Ring", 5, ["IncreasedLife0", "IncreasedLife1"], "chaos")
    assert result["chance_per_attempt"] == pytest.approx(expected, abs=1e-8)


def test_ilvl_gating(data: GameData) -> None:
    low = mod_odds(data, "Two-Stone Ring", 4, ["IncreasedLife1"], "alt")
    assert low["chance_per_attempt"] == 0 and low["expected_attempts"] is None
    assert low["n90"] is None
    assert any("needs item level 5" in w for w in low["warnings"])
    high = mod_odds(data, "Two-Stone Ring", 5, ["IncreasedLife1"], "alt")
    assert high["chance_per_attempt"] > 0 and "IncreasedLife1" in weights(high)


def test_tag_weight_zero_excludes_mod(data: GameData) -> None:
    result = mod_odds(data, "Vaal Regalia", 86, ["Strength1"], "chaos")
    assert result["chance_per_attempt"] == 0
    assert "Strength1" not in weights(result)
    assert any("spawn weight 0" in w for w in result["warnings"])


def test_essence_forced_mod_takes_slot_and_group(data: GameData) -> None:
    # Forced IncreasedLife1 = 1 prefix; 4 mods left: 3 prefixes, FireResist1, StunRecovery1.
    # 4 affixes -> 3 draws, miss FireResist1 only via orders P P S, P S P, S P P (prefix cap 3).
    miss_with_4 = 3 / 5 * 2 / 4 * 1 / 2 + 3 / 5 * 1 / 4 * 2 / 3 + 1 / 5 * 3 / 4 * 2 / 3
    expected = RARE_4 * (1 - miss_with_4) + RARE_5_OR_6 * 1
    method = "essence:Whispering Essence of Greed"
    result = mod_odds(data, "Vaal Regalia", 5, ["FireResist1"], method)
    assert result["chance_per_attempt"] == pytest.approx(expected, abs=1e-8)
    assert result["forced_mods"] == ["IncreasedLife1"]
    assert not {"IncreasedLife0", "IncreasedLife1"} & set(weights(result))

    forced = mod_odds(data, "Vaal Regalia", 5, ["IncreasedLife1"], method)
    assert forced["chance_per_attempt"] == 1 and forced["calculation"]["method"] == "forced"
    blocked = mod_odds(data, "Vaal Regalia", 5, ["IncreasedLife0"], method)
    assert blocked["chance_per_attempt"] == 0
    assert any("shares a mod group" in w for w in blocked["warnings"])


def test_essence_caps_item_level_of_random_mods(data: GameData) -> None:
    # Whispering Essence of Greed has item_level_restriction 35; FireResist6 needs level 60.
    high = mod_odds(data, "Vaal Regalia", 86, ["FireResist6"], "essence:greed1")
    assert high["chance_per_attempt"] == 0 and "FireResist6" not in weights(high)
    (warning,) = [w for w in high["warnings"] if "FireResist6" in w]
    assert "needs item level 60" in warning and "caps item level at 35" in warning
    fire_86 = mod_odds(data, "Vaal Regalia", 86, ["FireResist1"], "essence:greed1")
    fire_35 = mod_odds(data, "Vaal Regalia", 35, ["FireResist1"], "essence:greed1")
    assert fire_86["chance_per_attempt"] == fire_35["chance_per_attempt"]
    assert fire_86["weight_table"] == fire_35["weight_table"]
    chaos = mod_odds(data, "Vaal Regalia", 86, ["FireResist6"], "chaos")
    assert chaos["chance_per_attempt"] > 0


def test_helical_ring_affix_limits(data: GameData) -> None:
    # Helical Ring implicit: -2 prefixes, +1 suffix -> 1 prefix, 4 suffixes. At ilvl 1 the ring
    # pool is 2 prefixes + 3 suffixes, so every 4-6 affix roll ends with 1 prefix + 3 suffixes
    # and AddedFireDamage1 hits iff it is the first prefix drawn: 500 / (500 + 1000).
    result = mod_odds(data, "Helical Ring", 1, ["AddedFireDamage1"], "chaos")
    assert result["chance_per_attempt"] == pytest.approx(500 / 1500, abs=1e-8)
    assert result["affix_limits"] == {"prefix": 1, "suffix": 4}
    assert any("allows 1 prefix(es) and 4 suffix(es)" in w for w in result["warnings"])
    plain = mod_odds(data, "Two-Stone Ring", 1, ["AddedFireDamage1"], "chaos")
    unlimited = RARE_4 * (1 - p_last(500, [1000, 1000, 1000, 400])) + RARE_5_OR_6
    assert plain["chance_per_attempt"] == pytest.approx(unlimited, abs=1e-8)
    with pytest.raises(GameDataError, match="not modelled for Helical Ring"):
        mod_odds(data, "Helical Ring", 1, ["AddedFireDamage1"], "alt")


def test_fossil_with_unmodelled_effect_is_rejected(data: GameData) -> None:
    with pytest.raises(GameDataError, match=r"Sanctified Fossil is not modelled: lucky"):
        mod_odds(data, "Vaal Regalia", 86, ["FireResist1"], "fossil:pristine,sanctified")


def test_fossil_multipliers_and_forbidden_mods(data: GameData) -> None:
    # Pristine: life x10, defences x0. Scorched: fire x10, cold x0.
    # Groups at ilvl 16: IncreasedLife 2 * 10000, AttackerTakesDamage 1000, FireResist 10000,
    # StunRecovery 1000, ChaosResist 250.
    expected = RARE_4 * (1 - p_last(250, [20000, 1000, 10000, 1000])) + RARE_5_OR_6
    result = mod_odds(data, "Vaal Regalia", 16, ["ChaosResist1"], "fossil:pristine,scorched")
    assert result["chance_per_attempt"] == pytest.approx(expected, abs=1e-8)
    table = weights(result)
    assert table["IncreasedLife0"] == 10000 and table["FireResist1"] == 10000
    assert table["ChaosResist1"] == 250
    assert "LocalIncreasedEnergyShield1" not in table

    forbidden = mod_odds(
        data, "Vaal Regalia", 16, ["LocalIncreasedEnergyShield1"], "fossil:pristine"
    )
    assert forbidden["chance_per_attempt"] == 0
    assert any("Pristine Fossil" in w for w in forbidden["warnings"])


def test_monte_carlo_is_seeded_and_agrees_with_exact(data: GameData) -> None:
    args = (data, "Two-Stone Ring", 5, ["IncreasedLife0", "IncreasedLife1"], "chaos")
    exact = mod_odds(*args)["chance_per_attempt"]
    sampled = mod_odds(*args, force_monte_carlo=True)
    calc = sampled["calculation"]
    assert calc["method"] == "monte_carlo" and calc["samples"] > 0 and calc["seed"] is not None
    assert abs(sampled["chance_per_attempt"] - exact) < 4 * calc["std_error"]
    again = mod_odds(*args, force_monte_carlo=True)
    assert again["chance_per_attempt"] == sampled["chance_per_attempt"]
    low, high = calc["ci95"]
    assert calc["resolved"] is True and low < exact < high


def test_monte_carlo_without_hits_is_unresolved() -> None:
    pool = [
        Candidate("rare_target", True, frozenset({"T"}), 1e-9, True),
        *(Candidate(f"m{i}", i % 2 == 0, frozenset({f"g{i}"}), 1000, False) for i in range(12)),
    ]
    estimate = monte_carlo(pool, RARE, Start(), samples=2000)
    assert estimate.resolved is False and estimate.std_error is None
    assert estimate.upper_bound_95 == pytest.approx(1 - 0.05 ** (1 / 2000))
    assert estimate.upper_bound_95 == pytest.approx(3 / 2000, rel=0.01)


def test_unresolved_estimate_is_not_reported_as_zero(
    data: GameData, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_hits(pool: list[Candidate], model: CountModel, start: Start, force: bool) -> Estimate:
        return Estimate(
            0.0, "monte_carlo", samples=20000, seed=1729, resolved=False, upper_bound_95=1.5e-4
        )

    monkeypatch.setattr(odds_module, "hit_probability", no_hits)
    result = mod_odds(data, "Two-Stone Ring", 1, ["Strength1"], "chaos")
    assert result["chance_per_attempt"] is None
    assert result["expected_attempts"] is None and result["n90"] is None
    assert result["calculation"]["resolved"] is False
    assert result["calculation"]["upper_bound_95"] == 1.5e-4
    assert any("unresolved" in w and "1.50e-04" in w for w in result["warnings"])


def test_attempts_for() -> None:
    assert attempts_for(0) is None and attempts_for(1) == 1
    assert attempts_for(0.5) == 4


@pytest.mark.parametrize(
    ("base", "mods", "method", "message"),
    [
        ("Vaal Regalea", ["IncreasedLife0"], "chaos", "Closest: Vaal Regalia"),
        ("Vaal Regalia", ["IncreasedLife00"], "chaos", "IncreasedLife0"),
        ("Vaal Regalia", ["IncreasedLife0"], "exalt", "unknown method"),
        ("Vaal Regalia", ["IncreasedLife0"], "essence:hatred", "unknown essence"),
        ("Two-Stone Ring", ["Strength1"], "essence:greed1", "no mod for item class"),
        ("Crimson Jewel", ["Strength1"], "chaos", "not modelled"),
        ("Vaal Regalia", [], "chaos", "at least one"),
    ],
)
def test_bad_inputs_raise_with_hints(
    data: GameData, base: str, mods: list[str], method: str, message: str
) -> None:
    with pytest.raises(GameDataError, match=message):
        mod_odds(data, base, 50, mods, method)


async def test_tool_returns_envelope(config: Config) -> None:
    shutil.copytree(FIXTURE_DIR, config.data_dir / "gamedata")
    async with Client(build_server(config)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        annotations = tools["mod_odds"].annotations
        assert annotations is not None and annotations.read_only_hint is True
        args = {"base": "Two-Stone Ring", "ilvl": 1, "target_mods": ["Strength1"], "method": "alt"}
        result = await client.call_tool("mod_odds", args)
    assert not result.is_error
    out: dict[str, Any] = result.structured_content or {}
    for key in ("chance_per_attempt", "expected_attempts", "n90", "weight_table", "assumptions"):
        assert key in out
    assert out["export_version"] == "3.30.0.4.3" and out["warnings"] == []
    (src,) = out["sources"]
    assert "3.30.0.4.3" in src["name"] and src["downloaded_at"].startswith("2026-11-23")
    assert src["stale"] is False and out["data_age_seconds"] is not None


async def test_tool_missing_export_is_a_clear_error(config: Config) -> None:
    async with Client(build_server(config)) as client:
        args = {"base": "Vaal Regalia", "ilvl": 86, "target_mods": ["X"], "method": "chaos"}
        result = await client.call_tool("mod_odds", args)
    assert result.is_error
    text = " ".join(getattr(c, "text", "") for c in result.content)
    assert "fetch_gamedata.py" in text
