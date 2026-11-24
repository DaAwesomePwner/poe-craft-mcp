"""The docs match the code: tools named in the README exist, snippets validate."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from mcp import Client

from poe_craft_mcp.config import Config
from poe_craft_mcp.recipes.loader import parse_recipe_file
from poe_craft_mcp.recipes.models import Base, Outcome, Salvage, Step
from poe_craft_mcp.server import build_server

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
# Built in a parallel task; documented from their agreed signatures until they merge.
PENDING_TOOLS = {"evaluate_recipe", "rank_crafts"}


def _tools_in_readme() -> set[str]:
    section = README.split("## Tools", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `(\w+)", section, flags=re.MULTILINE))


async def test_readme_tools_exist(config: Config) -> None:
    async with Client(build_server(config)) as client:
        registered = {tool.name for tool in (await client.list_tools()).tools}
    documented = _tools_in_readme()
    assert documented >= {"get_league_status", "price_trade_query", "refresh", "mod_odds"}
    assert documented - PENDING_TOOLS <= registered
    assert registered - PENDING_TOOLS <= documented, "tool missing from the README table"


def test_readme_minimal_recipe_validates(tmp_path: Path) -> None:
    block = README.split("## Writing recipes", 1)[1].split("```yaml", 1)[1].split("```", 1)[0]
    path = tmp_path / "minimal.yaml"
    path.write_text(block, encoding="utf-8")
    recipe = parse_recipe_file(path)
    assert recipe.status == "unverified"
    assert not recipe.is_verified


def test_readme_config_keys_exist() -> None:
    section = README.split("## Configuration", 1)[1].split("\n## ", 1)[0]
    keys = set(re.findall(r"^\| `(\w+)`", section, flags=re.MULTILINE))
    assert {"contact", "league", "include_unverified"} <= keys
    assert keys <= set(Config.model_fields)


@pytest.mark.parametrize("path", sorted((ROOT / "recipes").glob("*.yaml")), ids=lambda p: p.name)
def test_shipped_recipes_validate(path: Path) -> None:
    assert parse_recipe_file(path).status == "unverified"


def test_import_prompt_requires_unverified_drafts() -> None:
    prompt = (ROOT / "scripts" / "import_recipe.md").read_text(encoding="utf-8")
    assert "status: unverified" in prompt and "verified_patch: null" in prompt
    assert "recipe.schema.json" in prompt


def test_recipe_guide_snippets_validate() -> None:
    """Every YAML fragment in docs/recipes.md parses into the recipe models."""
    text = (ROOT / "docs" / "recipes.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, flags=re.DOTALL)
    assert len(blocks) >= 3
    seen: set[str] = set()
    for block in blocks:
        # A block may show alternatives separated by a "# or" line.
        for part in block.split("# or\n"):
            doc = yaml.safe_load(part)
            if isinstance(doc, list):
                steps = [Step.model_validate(item) for item in doc]
                assert steps
                seen.add("steps")
            elif "base" in doc:
                Base.model_validate(doc["base"])
                seen.add("base")
            elif "outcomes" in doc:
                outcomes = [Outcome.model_validate(item) for item in doc["outcomes"]]
                assert sum(o.probability for o in outcomes) == pytest.approx(1.0)
                Salvage.model_validate(doc["salvage"])
                seen.add("outcomes")
            else:
                pytest.fail(f"unrecognised snippet: {part!r}")
    assert seen == {"steps", "base", "outcomes"}
