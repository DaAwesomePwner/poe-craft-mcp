"""Recipe `weights` chances with a cluster enchant and a Regal step: schema, model, service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from poe_craft_mcp.config import Config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.gamedata.loader import load_gamedata
from poe_craft_mcp.recipes.loader import parse_recipe_file, schema_problems
from poe_craft_mcp.recipes.models import Recipe, WeightsChance
from poe_craft_mcp.services.recipe_odds import resolve_chance

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WP, ES = "AfflictionNotableWickedPall_", "AfflictionNotableEternalSuffering"
DOT = "Chaos Damage over Time"


def cluster_recipe() -> dict[str, Any]:
    alteration = {"source": "ninja", "category": "Currency", "name": "Orb of Alteration", "qty": 1}
    regal = {"source": "ninja", "category": "Currency", "name": "Regal Orb", "qty": 1}
    return {
        "id": "medium-chaos-dot",
        "name": "Medium Chaos DoT cluster",
        "status": "unverified",
        "base": {"lookup": {"source": "ninja", "type": "BaseType", "name": "Medium Cluster Jewel"}},
        "steps": [
            {
                "action": "alt_aug",
                "materials": [alteration],
                "repeat_until": "Wicked Pall",
                "p_success": {
                    "p_source": "weights",
                    "method": "alt_aug",
                    "target_mods": [WP],
                    "ilvl": 84,
                    "enchant": DOT,
                    "passives": 4,
                },
            },
            {
                "action": "regal",
                "materials": [regal],
                "repeat_until": "Eternal Suffering",
                "p_success": {
                    "p_source": "weights",
                    "method": "regal",
                    "target_mods": [ES],
                    "ilvl": 84,
                    "enchant": DOT,
                    "existing_mods": [WP],
                },
            },
        ],
        "outcomes": [{"name": "done", "probability": 1, "fixed_chaos": 0}],
        "salvage": {"fixed_chaos": 0},
        "minutes": 5,
    }


def test_new_fields_round_trip(tmp_path: Path) -> None:
    raw = cluster_recipe()
    assert schema_problems(raw) == []
    path = tmp_path / "cluster.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    recipe = parse_recipe_file(path)
    first, second = (step.p_success for step in recipe.steps)
    assert isinstance(first, WeightsChance) and isinstance(second, WeightsChance)
    assert (first.enchant, first.passives, first.existing_mods) == (DOT, 4, None)
    assert second.existing_mods == [WP]
    assert recipe.to_document() == raw
    assert Recipe.model_validate(recipe.to_document()) == recipe


@pytest.mark.parametrize(
    "bad",
    [{"enchant": ""}, {"passives": 0}, {"passives": 13}, {"existing_mods": []}],
)
def test_schema_and_model_reject_bad_values(bad: dict[str, Any]) -> None:
    raw = cluster_recipe()
    raw["steps"][1]["p_success"].update(bad)
    assert schema_problems(raw)
    with pytest.raises(ValidationError):
        Recipe.model_validate(raw)


async def test_service_passes_enchant_and_existing_mods(tmp_path: Path) -> None:
    ctx = AppContext.create(Config(data_dir=tmp_path / "data"))
    ctx.services["gamedata"] = load_gamedata(FIXTURES / "gamedata")
    try:
        recipe = Recipe.model_validate(cluster_recipe())
        chances = [
            await resolve_chance(ctx, recipe, step.action, step.p_success)  # type: ignore[arg-type]
            for step in recipe.steps
        ]
    finally:
        await ctx.aclose()
    alt_aug, regal = chances
    assert alt_aug.error is None and regal.error is None
    # Same arithmetic as tests/test_cluster_odds.py (prefixes 2086, suffixes 1000).
    assert alt_aug.p == pytest.approx(348 / 3086 + 1000 / 3086 * 348 / 2086, abs=1e-8)
    assert regal.p == pytest.approx(348 / (1738 + 1000), abs=1e-8)
    report = alt_aug.report()
    assert report["enchant"]["name"] == DOT and report["enchant"]["passives"] == 4
    assert set(report["expected_materials"]) == {"alteration", "augmentation"}
    assert regal.report()["expected_materials"] == {"regal": pytest.approx(2738 / 348, abs=1e-4)}
