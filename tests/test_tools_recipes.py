from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from poe_craft_mcp.config import Config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.recipes.loader import RECIPES_ENV_VAR, RecipeStore, parse_recipe_file
from poe_craft_mcp.server import build_server
from poe_craft_mcp.tools.recipes import get_recipe_store

REPO_RECIPES = Path(__file__).resolve().parents[1] / "recipes"
RECIPE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "recipes"
SUMMARY_KEYS = {
    "id",
    "name",
    "tags",
    "status",
    "verified_patch",
    "budget_div",
    "requires",
    "warnings",
}


@pytest.fixture(autouse=True)
def _recipes_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RECIPES_ENV_VAR, str(RECIPE_FIXTURES))


async def call(config: Config, tool: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    async with Client(build_server(config)) as client:
        result = await client.call_tool(tool, args)
    if result.is_error:
        return True, {"text": result.content[0].text}  # type: ignore[union-attr]
    return False, result.structured_content or {}


def ids(data: dict[str, Any]) -> list[str]:
    return [r["id"] for r in data["recipes"]]


async def test_tools_are_registered_read_only(config: Config) -> None:
    async with Client(build_server(config)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in ("list_recipes", "get_recipe"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is True


async def test_list_recipes_hides_unverified_by_default(config: Config) -> None:
    is_error, data = await call(config, "list_recipes", {})
    assert not is_error
    assert ids(data) == ["verified-alt-spam", "verified-short-version"]
    assert data["hidden_unverified"] == 2
    assert "reason" not in data
    assert data["sources"][0]["name"] == "recipe knowledge base"
    assert data["data_age_seconds"] is None
    first = data["recipes"][0]
    assert set(first) == SUMMARY_KEYS
    assert first["warnings"] == []
    assert {e["file"] for e in data["load_errors"]} >= {"bad-yaml.yaml", "bad-probability.yaml"}


async def test_list_recipes_include_unverified_adds_warnings(config: Config) -> None:
    _, data = await call(config, "list_recipes", {"include_unverified": True})
    assert ids(data) == [
        "stale-patch",
        "unverified-flip",
        "verified-alt-spam",
        "verified-short-version",
    ]
    warned = {r["id"]: r["warnings"] for r in data["recipes"]}
    assert "older than 3.30" in warned["stale-patch"][0]
    assert warned["unverified-flip"][0].startswith("Unverified")
    assert warned["verified-alt-spam"] == []
    assert data["hidden_unverified"] == 0


async def test_config_default_and_per_call_override(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data", include_unverified=True)
    _, data = await call(config, "list_recipes", {})
    assert len(data["recipes"]) == 4
    _, data = await call(config, "list_recipes", {"include_unverified": False})
    assert len(data["recipes"]) == 2


async def test_list_recipes_filters_and_empty_reason(config: Config) -> None:
    _, data = await call(config, "list_recipes", {"tags": ["base-flip"]})
    assert data["recipes"] == []
    assert data["reason"] == (
        "1 recipe hidden as unverified; pass include_unverified=true to see them"
    )
    _, data = await call(config, "list_recipes", {"tags": ["harvest"]})
    assert data["reason"] == "no recipes match the given filters"
    _, data = await call(config, "list_recipes", {"max_budget_div": 1, "include_unverified": True})
    assert ids(data) == ["verified-alt-spam"]
    assert data["hidden_no_budget"] == 1


async def test_budget_cap_reason_mentions_recipes_without_budget(config: Config) -> None:
    _, data = await call(
        config,
        "list_recipes",
        {"tags": ["base-flip"], "max_budget_div": 0, "include_unverified": True},
    )
    assert data["recipes"] == [] and data["hidden_no_budget"] == 1
    assert "1 recipe without budget_div excluded by max_budget_div" in data["reason"]
    _, data = await call(config, "list_recipes", {"tags": ["base-flip"], "max_budget_div": 0})
    assert data["hidden_no_budget"] == 1 and data["hidden_unverified"] == 0


async def test_empty_store_explains_why(config: Config, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "broken.yaml").write_text("id: [", encoding="utf-8")
    ctx = AppContext.create(config)
    ctx.services["recipes"] = RecipeStore.load(empty)
    async with Client(build_server(ctx=ctx)) as client:
        result = await client.call_tool("list_recipes", {})
    data = result.structured_content or {}
    assert data["recipes"] == []
    assert "no recipes loaded" in data["reason"] and "1 files skipped" in data["reason"]


async def test_get_recipe_returns_full_json_and_warnings(config: Config) -> None:
    is_error, data = await call(config, "get_recipe", {"id": "unverified-flip"})
    assert not is_error
    recipe = data["recipe"]
    assert recipe["id"] == "unverified-flip" and recipe["status"] == "unverified"
    assert recipe["steps"][0]["p_success"] == {"p_source": "user", "value": 0.05}
    assert recipe["outcomes"][1]["fixed_chaos"] == 0
    assert data["warnings"][0].startswith("Unverified")
    assert data["sources"][0]["name"] == "recipe knowledge base"


async def test_get_recipe_deprecated_is_returned_with_warning(config: Config) -> None:
    _, data = await call(config, "get_recipe", {"id": "deprecated-old"})
    assert data["recipe"]["status"] == "deprecated"
    assert data["warnings"][0].startswith("Deprecated")


async def test_get_recipe_unknown_id_lists_known_ids(config: Config) -> None:
    is_error, data = await call(config, "get_recipe", {"id": "nope"})
    assert is_error
    assert "unknown recipe id 'nope'" in data["text"]
    assert "verified-alt-spam" in data["text"] and "deprecated-old" in data["text"]


def test_store_is_shared_per_context(config: Config) -> None:
    ctx = AppContext.create(config)
    assert get_recipe_store(ctx) is get_recipe_store(ctx)
    assert "deprecated-old" in get_recipe_store(ctx).recipes


async def test_get_recipe_output_round_trips_through_yaml(config: Config, tmp_path: Path) -> None:
    store = RecipeStore.load(RECIPE_FIXTURES)
    store.recipes.update(RecipeStore.load(REPO_RECIPES).recipes)
    ctx = AppContext.create(config)
    ctx.services["recipes"] = store
    async with Client(build_server(ctx=ctx)) as client:
        for recipe_id, original in store.recipes.items():
            result = await client.call_tool("get_recipe", {"id": recipe_id})
            saved = tmp_path / f"{recipe_id}.yaml"
            saved.write_text(yaml.safe_dump(result.structured_content["recipe"]), encoding="utf-8")
            assert parse_recipe_file(saved) == original


def test_registering_logs_skipped_files_at_startup(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="poe_craft_mcp.recipes.loader"):
        build_server(config)
    assert "skipping recipe bad-probability.yaml" in caplog.text
    assert "skipping recipe bad-yaml.yaml" in caplog.text


async def test_server_starts_and_explains_when_every_recipe_is_invalid(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "a.yaml").write_text("id: !!bool maybe", encoding="utf-8")
    monkeypatch.setenv(RECIPES_ENV_VAR, str(broken))
    with caplog.at_level(logging.WARNING, logger="poe_craft_mcp.recipes.loader"):
        _, data = await call(config, "list_recipes", {})
    assert "skipping recipe a.yaml" in caplog.text
    assert data["recipes"] == [] and "1 files skipped" in data["reason"]


async def test_missing_recipes_dir_reason_names_path_and_env_var(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nowhere"
    monkeypatch.setenv(RECIPES_ENV_VAR, str(missing))
    _, data = await call(config, "list_recipes", {})
    assert data["recipes"] == []
    assert str(missing) in data["reason"] and RECIPES_ENV_VAR in data["reason"]
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(RECIPES_ENV_VAR, str(empty))
    _, data = await call(config, "list_recipes", {})
    assert str(empty) in data["reason"] and RECIPES_ENV_VAR in data["reason"]
