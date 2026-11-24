from __future__ import annotations

import copy
import logging
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from poe_craft_mcp.recipes.loader import (
    RECIPES_ENV_VAR,
    RecipeStore,
    parse_recipe_file,
    resolve_recipes_dir,
    schema_problems,
)
from poe_craft_mcp.recipes.models import Recipe, parse_version

RECIPE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "recipes"
REPO_RECIPES = Path(__file__).resolve().parents[1] / "recipes"
LOADER_LOGGER = "poe_craft_mcp.recipes.loader"


def fixture_data(name: str = "verified-alt-spam.yaml") -> dict[str, Any]:
    return yaml.safe_load((RECIPE_FIXTURES / name).read_text(encoding="utf-8"))


def mutated(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(fixture_data())
    data.update(changes)
    return data


def test_schema_ships_as_package_data() -> None:
    schema = resources.files("poe_craft_mcp.recipes").joinpath("recipe.schema.json")
    assert schema.is_file()


def test_example_recipes_load_and_are_unverified() -> None:
    store = RecipeStore.load(REPO_RECIPES)
    assert store.errors == []
    assert store.ids() == [
        "example-essence-spam-body-armour",
        "example-fossil-crafted-ring",
        "example-stygian-vise-base-flip",
    ]
    tags = {tag for recipe in store.recipes.values() for tag in recipe.tags}
    assert {"essence", "fossil", "base-flip"} <= tags
    for recipe in store.recipes.values():
        assert recipe.status == "unverified"
        assert recipe.verified_patch is None
        assert not recipe.is_verified
        assert any("placeholders" in w for w in recipe.warnings())
        assert any(o.use_salvage for o in recipe.outcomes)
        assert recipe.budget_div is not None


def test_fixture_dir_skips_invalid_files_and_keeps_valid_ones() -> None:
    store = RecipeStore.load(RECIPE_FIXTURES)
    assert store.ids() == [
        "deprecated-old",
        "stale-patch",
        "unverified-flip",
        "verified-alt-spam",
        "verified-short-version",
    ]
    reasons = {e.file: e.reason for e in store.errors}
    assert set(reasons) == {
        "bad-probability.yaml",
        "bad-yaml.yaml",
        "missing-minutes.yaml",
        "unquoted-patch.yaml",
        "zz-duplicate-id.yaml",
    }
    assert "sum to 1" in reasons["bad-probability.yaml"]
    assert "invalid YAML" in reasons["bad-yaml.yaml"]
    assert "minutes" in reasons["missing-minutes.yaml"]
    assert "verified_patch" in reasons["unquoted-patch.yaml"]
    assert "duplicate recipe id" in reasons["zz-duplicate-id.yaml"]


def test_skipped_files_are_logged_with_name_and_reason(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOADER_LOGGER):
        RecipeStore.load(RECIPE_FIXTURES)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 5
    assert any("bad-probability.yaml" in m and "sum to 1" in m for m in messages)
    assert any("bad-yaml.yaml" in m and "invalid YAML" in m for m in messages)
    assert any("missing-minutes.yaml" in m and "minutes" in m for m in messages)


def test_missing_directory_gives_empty_store(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOADER_LOGGER):
        store = RecipeStore.load(tmp_path / "nope")
    assert store.recipes == {} and store.errors == []
    assert "not found" in caplog.text


def test_non_mapping_and_unreadable_files(tmp_path: Path) -> None:
    (tmp_path / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
    (tmp_path / "binary.yaml").write_bytes(b"\xff\xfe\x00bad")
    store = RecipeStore.load(tmp_path)
    reasons = {e.file: e.reason for e in store.errors}
    assert "mapping" in reasons["list.yaml"]
    assert "cannot read" in reasons["binary.yaml"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3.30", (3, 30, 0)), ("3.30.0", (3, 30, 0)), ("3.29.2", (3, 29, 2)), ("3.9", (3, 9, 0))],
)
def test_parse_version(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize("text", ["3", "v3.30", "3.30.0.1", "latest", ""])
def test_parse_version_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError, match="patch version"):
        parse_version(text)


@pytest.mark.parametrize(
    ("status", "patch", "verified"),
    [
        ("verified", "3.30.0", True),
        ("verified", "3.30", True),
        ("verified", "3.31.1", True),
        ("verified", "3.29.2", False),
        ("verified", "3.9", False),
        ("unverified", None, False),
        ("unverified", "3.30.0", False),
        ("deprecated", "3.30.0", False),
    ],
)
def test_is_verified_needs_status_and_patch(status: str, patch: str | None, verified: bool) -> None:
    recipe = Recipe.model_validate(mutated(status=status, verified_patch=patch))
    assert recipe.is_verified is verified


def test_warnings_by_status() -> None:
    assert Recipe.model_validate(mutated()).warnings() == []
    stale = Recipe.model_validate(mutated(verified_patch="3.29.2"))
    assert "older than 3.30" in stale.warnings()[0]
    unverified = Recipe.model_validate(mutated(status="unverified", verified_patch=None))
    assert unverified.warnings()[0].startswith("Unverified")
    deprecated = Recipe.model_validate(mutated(status="deprecated"))
    assert deprecated.warnings()[0].startswith("Deprecated")


def test_listing_hides_unverified_and_deprecated_by_default() -> None:
    store = RecipeStore.load(RECIPE_FIXTURES)
    default = store.list()
    assert [r.id for r in default.recipes] == ["verified-alt-spam", "verified-short-version"]
    assert default.hidden_unverified == 2
    shown = store.list(include_unverified=True)
    assert [r.id for r in shown.recipes] == [
        "stale-patch",
        "unverified-flip",
        "verified-alt-spam",
        "verified-short-version",
    ]
    assert all(r.warnings() for r in shown.recipes if not r.is_verified)


def test_listing_tag_and_budget_filters() -> None:
    store = RecipeStore.load(RECIPE_FIXTURES)
    by_tag = store.list(tags=["FOSSIL", "bench"], include_unverified=True)
    assert [r.id for r in by_tag.recipes] == ["verified-alt-spam", "verified-short-version"]
    assert store.list(tags=["essence"]).recipes == []
    assert store.list(tags=["essence"]).hidden_unverified == 1
    cheap = store.list(max_budget_div=2, include_unverified=True)
    assert [r.id for r in cheap.recipes] == ["stale-patch", "unverified-flip", "verified-alt-spam"]
    assert [r.id for r in store.list(max_budget_div=1).recipes] == ["verified-alt-spam"]


def test_resolve_recipes_dir_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_recipes_dir() == REPO_RECIPES
    (tmp_path / "recipes").mkdir()
    assert resolve_recipes_dir() == tmp_path / "recipes"
    monkeypatch.setenv(RECIPES_ENV_VAR, str(tmp_path / "elsewhere"))
    assert resolve_recipes_dir() == tmp_path / "elsewhere"


def _material(**fields: Any) -> dict[str, Any]:
    return {"source": "ninja", "category": "C", "qty": 1, **fields}


REJECTED_BY_BOTH: dict[str, dict[str, Any]] = {
    "outcome with two values": {
        "outcomes": [{"name": "x", "probability": 1, "fixed_chaos": 0, "use_salvage": True}]
    },
    "outcome with no value": {"outcomes": [{"name": "x", "probability": 1}]},
    "zero probability": {"outcomes": [{"name": "x", "probability": 0, "fixed_chaos": 0}]},
    "probability above one": {"outcomes": [{"name": "x", "probability": 1.5, "fixed_chaos": 0}]},
    "zero minutes": {"minutes": 0},
    "bad patch format": {"verified_patch": "latest"},
    "verified without patch": {"verified_patch": None},
    "unknown field": {"surprise": True},
    "bad id": {"id": "Has Spaces"},
    "no steps": {"steps": []},
    "empty salvage": {"salvage": {}},
    "base with both": {
        "base": {"trade_url": "u", "lookup": {"source": "ninja", "type": "BaseType", "name": "n"}}
    },
    "base with neither": {"base": {}},
    "base lookup not BaseType": {
        "base": {"lookup": {"source": "ninja", "type": "Currency", "name": "n"}}
    },
    "material with name and id": {
        "steps": [{"action": "a", "materials": [_material(name="n", id="i")]}]
    },
    "material with neither": {"steps": [{"action": "a", "materials": [_material()]}]},
    "material qty zero": {"steps": [{"action": "a", "materials": [_material(name="n", qty=0)]}]},
    "weights without target mods": {
        "steps": [{"action": "a", "p_success": {"p_source": "weights", "method": "chaos"}}]
    },
    "guide chance above one": {
        "steps": [{"action": "a", "p_success": {"p_source": "guide", "value": 1.2}}]
    },
    "repeat without chance": {"steps": [{"action": "a", "repeat_until": "done"}]},
}


@pytest.mark.parametrize("case", REJECTED_BY_BOTH)
def test_schema_and_models_agree_on_invalid_recipes(case: str) -> None:
    data = mutated(**REJECTED_BY_BOTH[case])
    assert schema_problems(data), "schema should reject"
    with pytest.raises(ValidationError):
        Recipe.model_validate(data)


def test_probability_sum_is_checked_by_models_with_tolerance() -> None:
    outcomes = [
        {"name": "a", "probability": 0.1, "fixed_chaos": 0},
        {"name": "b", "probability": 0.2, "fixed_chaos": 0},
        {"name": "c", "probability": 0.7 + 5e-7, "fixed_chaos": 0},
    ]
    assert Recipe.model_validate(mutated(outcomes=outcomes))
    outcomes[2]["probability"] = 0.7 + 5e-6
    with pytest.raises(ValidationError, match="sum to 1"):
        Recipe.model_validate(mutated(outcomes=outcomes))


def test_parse_recipe_file_reports_schema_paths(tmp_path: Path) -> None:
    data = mutated(steps=[{"action": "a", "p_success": {"p_source": "weights", "method": "m"}}])
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match=r"schema: steps/0/p_success.*target_mods"):
        parse_recipe_file(path)
