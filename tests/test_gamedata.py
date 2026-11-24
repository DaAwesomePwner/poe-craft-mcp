from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import httpx
import pytest

from poe_craft_mcp.config import Config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.gamedata.fetch import FetchError, fetch_export
from poe_craft_mcp.gamedata.loader import EXPORT_FILES, get_gamedata, load_gamedata
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


def test_affix_limit_implicit_and_unmodelled_fossil_parsed(data: GameData) -> None:
    helical, _ = resolve_base(data, "Helical Ring")
    assert (helical.prefix_delta, helical.suffix_delta) == (-2, 1)
    two_stone, _ = resolve_base(data, "Two-Stone Ring")
    assert (two_stone.prefix_delta, two_stone.suffix_delta) == (0, 0)
    (sanctified,) = resolve_fossils(data, ["sanctified"])
    assert sanctified.unmodelled and "lucky" in sanctified.unmodelled[0]
    (pristine,) = resolve_fossils(data, ["pristine"])
    assert pristine.unmodelled == ()


def _export_transport(fail: str | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.lstrip("/")
        if name == "version.txt":
            return httpx.Response(200, text="3.30.0.4.3")
        stem = name.removesuffix(".min.json")
        if stem == fail:
            return httpx.Response(500)
        path = FIXTURE_DIR / f"{stem}.json"
        return httpx.Response(200, content=path.read_bytes() if path.is_file() else b"{}")

    return httpx.MockTransport(handler)


def test_fetch_publishes_complete_verified_export(tmp_path: Path) -> None:
    target = tmp_path / "gamedata"
    with httpx.Client(transport=_export_transport()) as client:
        manifest = fetch_export(target, "test-agent", client=client)
    assert manifest["game_version"] == "3.30.0.4.3"
    assert set(manifest["files"]) == set(EXPORT_FILES)
    assert not (tmp_path / "gamedata.staging").exists()
    loaded = load_gamedata(target)
    assert loaded.game_version == "3.30.0.4.3" and "Strength1" in loaded.mods


def test_failed_fetch_keeps_previous_export(tmp_path: Path) -> None:
    target = _copy_export(tmp_path / "gamedata", {"game_version": "3.30.0", "marker": "old"})
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    with (
        httpx.Client(transport=_export_transport(fail="base_items")) as client,
        pytest.raises(FetchError, match="500"),
    ):
        fetch_export(target, "test-agent", client=client)
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before
    assert not (tmp_path / "gamedata.staging").exists()


def test_oversized_download_aborts_and_keeps_previous_export(tmp_path: Path) -> None:
    target = _copy_export(tmp_path / "gamedata", {"game_version": "3.30.0"})
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    with (
        httpx.Client(transport=_export_transport()) as client,
        pytest.raises(FetchError, match="larger than 1000 bytes"),
    ):
        fetch_export(target, "test-agent", client=client, max_bytes=1000)
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


def test_loader_rejects_files_not_matching_manifest(tmp_path: Path) -> None:
    files = {
        name: {"sha256": hashlib.sha256((FIXTURE_DIR / f"{name}.json").read_bytes()).hexdigest()}
        for name in ("mods", "base_items", "essences", "fossils")
    }
    export = _copy_export(tmp_path / "gd", {"game_version": "3.30.0", "files": files})
    assert load_gamedata(export).game_version == "3.30.0"
    (export / "base_items.json").write_text("{}", encoding="utf-8")
    with pytest.raises(GameDataError, match="does not match manifest"):
        load_gamedata(export)
