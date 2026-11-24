"""The shipped recipes load offline and every real trade URL decodes locally (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from poe_craft_mcp.clients.trade_models import decode_search_id, parse_saved_url
from poe_craft_mcp.recipes.loader import RecipeStore, parse_recipe_file
from poe_craft_mcp.recipes.models import MIN_VERIFIED_PATCH, Recipe, WeightsChance, parse_version

REPO_RECIPES = Path(__file__).resolve().parents[1] / "recipes"
FILES = sorted(REPO_RECIPES.glob("*.yaml"))
REAL = [p for p in FILES if not p.name.startswith("example-")]
# The agent-drafted recipes described in docs/starter-recipes.md. They ship unverified; once a
# player checks one in game and promotes it, drop it from this tuple.
DRAFTS = (
    "convoking-wand-spell-minion-levels",
    "granite-flagellants-armadillo",
    "quicksilver-experimenters-cheetah",
    "large-cluster-spell-mage-hunter",
    "medium-cluster-chaos-dot-wicked-pall",
    "medium-cluster-crit-malice-pressure-points",
    "medium-cluster-minion-life-rebirth",
)
# Cluster drafts: id -> (base, recipe enchant, trade option of "Added Small Passive Skills
# grant", passive count). Trade options from pathofexile.com/api/trade/data/stats.
CLUSTERS = {
    "large-cluster-spell-mage-hunter": ("Large Cluster Jewel", "Spell Damage", 10, 8),
    "medium-cluster-chaos-dot-wicked-pall": (
        "Medium Cluster Jewel",
        "Chaos Damage over Time",
        19,
        5,
    ),
    "medium-cluster-crit-malice-pressure-points": (
        "Medium Cluster Jewel",
        "Critical Strike Chance",
        29,
        4,
    ),
    "medium-cluster-minion-life-rebirth": ("Medium Cluster Jewel", "Minion Life", 30, 4),
}
ENCHANT_STAT = "enchant.stat_3948993189"
PASSIVES_STAT = "enchant.stat_3086156145"


def test_every_recipe_file_loads() -> None:
    store = RecipeStore.load(REPO_RECIPES)
    assert store.errors == []
    assert store.problem is None
    assert len(store.recipes) == len(FILES)


@pytest.mark.parametrize("recipe_id", DRAFTS)
def test_drafts_ship_unverified(recipe_id: str) -> None:
    recipe = parse_recipe_file(REPO_RECIPES / f"{recipe_id}.yaml")
    assert recipe.status == "unverified" and recipe.verified_patch is None
    assert not recipe.is_verified


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_real_recipe_is_documented_and_consistent(path: Path) -> None:
    recipe = parse_recipe_file(path)
    assert recipe.id == path.stem
    if recipe.status == "verified":
        assert recipe.verified_patch is not None
        assert parse_version(recipe.verified_patch) >= MIN_VERIFIED_PATCH
        assert recipe.is_verified
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


@pytest.mark.parametrize("recipe_id", DRAFTS)
def test_draft_trade_urls_decode(recipe_id: str) -> None:
    recipe = parse_recipe_file(REPO_RECIPES / f"{recipe_id}.yaml")
    queries = _decoded(recipe)
    assert queries
    for body in queries:
        query = body["query"]
        assert query["status"] == {"option": "online"}
        assert body["sort"] == {"price": "asc"}
        assert query["type"]
        assert query["stats"]


def test_flask_bases_exclude_corrupted_items() -> None:
    for recipe_id in ("granite-flagellants-armadillo", "quicksilver-experimenters-cheetah"):
        recipe = parse_recipe_file(REPO_RECIPES / f"{recipe_id}.yaml")
        assert recipe.base.trade_url is not None
        body = decode_search_id(parse_saved_url(recipe.base.trade_url).search_id)
        assert body is not None
        misc = body["query"]["filters"]["misc_filters"]["filters"]
        assert misc["corrupted"] == {"option": "false"}
        assert all(not o.use_salvage for o in recipe.outcomes), "every result is sold"


def test_example_recipes_keep_placeholder_urls() -> None:
    for path in FILES:
        if path.name.startswith("example-"):
            recipe = parse_recipe_file(path)
            assert all("<" in url for url in recipe.trade_urls())


def _filters(body: dict) -> dict[str, dict]:
    """Every stat filter of a decoded query, by stat id."""
    return {f["id"]: f for group in body["query"]["stats"] for f in group["filters"]}


@pytest.mark.parametrize("recipe_id", sorted(CLUSTERS))
def test_cluster_recipes_model_alt_aug_then_one_regal(recipe_id: str) -> None:
    base, enchant, _, passives = CLUSTERS[recipe_id]
    recipe = parse_recipe_file(REPO_RECIPES / f"{recipe_id}.yaml")
    assert {"cluster", "alt", "regal"} <= set(recipe.tags)
    assert [s.action for s in recipe.steps] == ["transmute", "alt_aug", "regal"]
    transmute, alt_aug, regal = recipe.steps
    assert transmute.p_success is None and regal.p_success is None, "paid once"
    chance = alt_aug.p_success
    assert isinstance(chance, WeightsChance) and chance.method == "alt_aug"
    assert (chance.base, chance.enchant, chance.passives) == (base, enchant, passives)
    assert chance.ilvl is not None and 68 <= chance.ilvl <= 83, "84+ adds mods to the pool"
    assert chance.target_mods and all(m.startswith("AfflictionNotable") for m in chance.target_mods)
    names = {m.name for step in recipe.steps for m in step.materials}
    assert {"Orb of Alteration", "Orb of Augmentation", "Regal Orb"} <= names
    assert len(recipe.outcomes) == 2 and all(o.trade_url for o in recipe.outcomes)
    assert sum(o.probability for o in recipe.outcomes) == pytest.approx(1, abs=1e-9)


@pytest.mark.parametrize("recipe_id", sorted(CLUSTERS))
def test_cluster_trade_searches_match_enchant_and_passives(recipe_id: str) -> None:
    base, _, option, passives = CLUSTERS[recipe_id]
    recipe = parse_recipe_file(REPO_RECIPES / f"{recipe_id}.yaml")
    assert recipe.base.trade_url is not None
    searches = [recipe.base.trade_url, *(o.trade_url for o in recipe.outcomes)]
    for url in searches:
        body = decode_search_id(parse_saved_url(url).search_id)  # type: ignore[arg-type]
        assert body is not None
        assert body["query"]["type"] == base
        stats = _filters(body)
        assert stats[ENCHANT_STAT]["value"] == {"option": option}
        assert stats[PASSIVES_STAT]["value"] == {"min": passives, "max": passives}
        misc = body["query"]["filters"]["misc_filters"]["filters"]
        assert misc["corrupted"] == {"option": "false"}
    base_body = decode_search_id(parse_saved_url(recipe.base.trade_url).search_id)
    assert base_body is not None
    assert base_body["query"]["filters"]["type_filters"]["filters"]["rarity"] == {
        "option": "normal"
    }
    assert set(_filters(base_body)) == {ENCHANT_STAT, PASSIVES_STAT}, "the base has no notables"
    for outcome in recipe.outcomes:
        body = decode_search_id(parse_saved_url(outcome.trade_url).search_id)  # type: ignore[arg-type]
        assert body is not None
        assert len(_filters(body)) > 2, f"{outcome.name}: the sale search names its notables"
