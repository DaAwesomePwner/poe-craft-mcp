from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from poe_craft_mcp.config import Config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.gamedata.loader import get_gamedata, load_gamedata
from poe_craft_mcp.gamedata.lookup import resolve_base, resolve_essence, resolve_fossils
from poe_craft_mcp.gamedata.models import GameData, GameDataError, parse_patch
from poe_craft_mcp.gamedata.pool import PoolEntry, entry_for, first_match

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "gamedata"


@pytest.fixture(scope="module")
def data() -> GameData:
    return load_gamedata(FIXTURE_DIR)


def _copy_export(dest: Path, manifest: dict[str, object] | None) -> Path:
    shutil.copytree(FIXTURE_DIR, dest)
    if manifest is None:
        (dest / "manifest.json").unlink()
    else:
        (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return dest


def test_loads_fixture_export(data: GameData) -> None:
    assert data.game_version == "3.30.0.4.3"
    assert data.stale_warning is None
    assert data.downloaded_at is not None and data.downloaded_at.year == 2026
    life = data.mods["IncreasedLife0"]
    assert life.generation_type == "prefix" and life.groups == {"IncreasedLife"}
    assert life.spawn_weights[0] == ("fishing_rod", 0)
    assert {"life", "resource"} <= life.tags
    regalia, _ = resolve_base(data, "vaal regalia")
    assert regalia.item_class == "Body Armour" and "int_armour" in regalia.tags
    assert resolve_essence(data, "Whispering Essence of Greed").mods == {
        "Body Armour": "IncreasedLife1"
    }
    pristine, scorched = resolve_fossils(data, ["pristine", "Scorched Fossil"])
    assert ("life", 1000) in pristine.weights and ("cold", 0) in scorched.weights


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (None, "cannot confirm"),
        ({"game_version": "3.25.3.1"}, "before 3.30"),
        ({"game_version": "3.30.0"}, None),
    ],
)
def test_stale_warning_unless_built_for_330(
    tmp_path: Path, manifest: dict[str, object] | None, expected: str | None
) -> None:
    loaded = load_gamedata(_copy_export(tmp_path / "gd", manifest))
    if expected is None:
        assert loaded.stale_warning is None
    else:
        assert loaded.stale_warning is not None and expected in loaded.stale_warning
    assert parse_patch("3.30.0.4.3") == (3, 30) and parse_patch("garbage") is None


def test_missing_export_explains_how_to_fetch(tmp_path: Path) -> None:
    with pytest.raises(GameDataError, match=r"fetch_gamedata\.py"):
        load_gamedata(tmp_path / "nothing-here")


def test_malformed_export_is_a_gamedata_error(tmp_path: Path) -> None:
    export = _copy_export(tmp_path / "gd", {"game_version": "3.30.0"})
    (export / "mods.json").write_text('{"X": {"domain": "item"}}', encoding="utf-8")
    with pytest.raises(GameDataError, match="unexpected format"):
        load_gamedata(export)
    (export / "mods.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(GameDataError, match="cannot read"):
        load_gamedata(export)


def test_get_gamedata_is_lazy_and_retries_after_fetch(config: Config) -> None:
    ctx = AppContext.create(config)
    assert "gamedata" not in ctx.services
    with pytest.raises(GameDataError, match="fetch_gamedata"):
        get_gamedata(ctx)
    assert "gamedata" not in ctx.services
    shutil.copytree(FIXTURE_DIR, config.data_dir / "gamedata")
    first = get_gamedata(ctx)
    assert get_gamedata(ctx) is first
    assert first.game_version == "3.30.0.4.3"


def test_lookup_errors_give_hints(data: GameData) -> None:
    with pytest.raises(GameDataError, match="Closest: Vaal Regalia"):
        resolve_base(data, "Vaal Regalea")
    with pytest.raises(GameDataError, match="Pristine Fossil"):
        resolve_fossils(data, ["Pristne Fossil"])
    with pytest.raises(GameDataError, match="1 to 4 fossils"):
        resolve_fossils(data, [])
    with pytest.raises(GameDataError, match="only once"):
        resolve_fossils(data, ["pristine", "pristine"])


def test_first_matching_tag_wins_in_list_order(data: GameData) -> None:
    life = data.mods["IncreasedLife0"].spawn_weights
    assert first_match(life, {"default", "weapon"}) == 0
    assert first_match(life, {"default", "ring"}) == 1000
    strength = data.mods["Strength1"].spawn_weights
    assert first_match(strength, {"ring", "default"}) == 1000
    assert first_match(strength, {"int_armour", "armour", "default"}) == 0
    assert first_match(strength, {"no_such_tag"}) is None
    chaos_res = data.mods["ChaosResist1"].spawn_weights
    assert first_match(chaos_res, {"body_armour", "armour", "default"}) == 250


def test_entry_for_exclusions(data: GameData) -> None:
    regalia, _ = resolve_base(data, "Vaal Regalia")
    ring, _ = resolve_base(data, "Two-Stone Ring")
    jewel, _ = resolve_base(data, "Crimson Jewel")
    strength = data.mods["Strength1"]
    assert "spawn weight 0" in str(entry_for(strength, regalia, 86))
    on_ring = entry_for(strength, ring, 86)
    assert isinstance(on_ring, PoolEntry) and on_ring.weight == 1000
    assert "item level 16" in str(entry_for(data.mods["ChaosResist1"], regalia, 15))
    assert isinstance(entry_for(data.mods["ChaosResist1"], regalia, 16), PoolEntry)
    assert "essence-only" in str(entry_for(data.mods["StrengthEssence7_"], ring, 86))
    assert "domain" in str(entry_for(strength, jewel, 86))
