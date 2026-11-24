"""The shipped recipes load offline and every real trade URL decodes locally (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from poe_craft_mcp.clients.trade_models import decode_search_id, parse_saved_url
from poe_craft_mcp.recipes.loader import RecipeStore, parse_recipe_file
from poe_craft_mcp.recipes.models import Recipe

REPO_RECIPES = Path(__file__).resolve().parents[1] / "recipes"
FILES = sorted(REPO_RECIPES.glob("*.yaml"))
STARTERS = [p for p in FILES if not p.name.startswith("example-")]


def test_every_recipe_file_loads() -> None:
    store = RecipeStore.load(REPO_RECIPES)
    assert store.errors == []
    assert store.problem is None
    assert len(store.recipes) == len(FILES)
    assert len(STARTERS) >= 3


@pytest.mark.parametrize("path", STARTERS, ids=lambda p: p.name)
def test_starter_recipe_is_unverified_and_documented(path: Path) -> None:
    recipe = parse_recipe_file(path)
    assert recipe.id == path.stem
    assert recipe.status == "unverified" and recipe.verified_patch is None
    assert not recipe.is_verified
    assert recipe.sources and all(s.startswith("https://") for s in recipe.sources)
    assert recipe.budget_div is not None and recipe.notes
    assert any(o.trade_url for o in recipe.outcomes), "a sale must be priced by trade_url"
    for step in recipe.steps:
        assert (step.repeat_until is None) == (step.p_success is None)


def _decoded(recipe: Recipe) -> list[dict]:
    queries = []
    for url in recipe.trade_urls():
        saved = parse_saved_url(url)
        assert saved.kind == "search"
        body = decode_search_id(saved.search_id)
        assert body is not None, f"{recipe.id}: {url} is not a self-contained (H4sI) search id"
        queries.append(body)
    return queries


@pytest.mark.parametrize("path", STARTERS, ids=lambda p: p.name)
def test_starter_trade_urls_decode(path: Path) -> None:
    recipe = parse_recipe_file(path)
    queries = _decoded(recipe)
    assert queries
    for body in queries:
        query = body["query"]
        assert query["status"] == {"option": "online"}
        assert body["sort"] == {"price": "asc"}
        assert query["type"]
        assert query["stats"]


def test_example_recipes_keep_placeholder_urls() -> None:
    for path in FILES:
        if path.name.startswith("example-"):
            recipe = parse_recipe_file(path)
            assert all("<" in url for url in recipe.trade_urls())
