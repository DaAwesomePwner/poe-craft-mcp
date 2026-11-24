"""Find, validate and index recipe files. A bad file is skipped and logged, never fatal."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from pydantic import ValidationError

from poe_craft_mcp.recipes.models import Recipe

log = logging.getLogger(__name__)

RECIPES_ENV_VAR = "POE_CRAFT_RECIPES"
SETUP_HINT = (
    f"set {RECIPES_ENV_VAR} to the folder holding your recipe *.yaml files, "
    "or launch with `uv --directory <repo> run poe-craft-mcp`"
)
MAX_REASONS = 3


@cache
def _validator() -> Draft202012Validator:
    schema = resources.files("poe_craft_mcp.recipes").joinpath("recipe.schema.json")
    return Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")))


def resolve_recipes_dir() -> Path:
    """`$POE_CRAFT_RECIPES`, else `./recipes`, else `recipes/` next to the source checkout."""
    env = os.environ.get(RECIPES_ENV_VAR)
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / "recipes"
    if local.is_dir():
        return local
    return Path(__file__).resolve().parents[3] / "recipes"


@dataclass(frozen=True)
class RecipeError:
    file: str
    reason: str


@dataclass(frozen=True)
class RecipeListing:
    recipes: list[Recipe]
    hidden_unverified: int
    hidden_no_budget: int = 0


@dataclass
class RecipeStore:
    """Valid recipes keyed by id, plus the files that were skipped."""

    directory: Path
    recipes: dict[str, Recipe] = field(default_factory=dict)
    errors: list[RecipeError] = field(default_factory=list)
    problem: str | None = None

    @classmethod
    def load(cls, directory: Path | None = None) -> RecipeStore:
        store = cls(directory or resolve_recipes_dir())
        _validator()
        try:
            files = sorted(store.directory.glob("*.yaml")) if store.directory.is_dir() else None
        except OSError as exc:
            files = None
            log.warning("cannot list recipes directory %s: %s", store.directory, exc)
        if files is None:
            store.problem = f"recipes directory not found: {store.directory}; {SETUP_HINT}"
        elif not files:
            store.problem = f"no recipe *.yaml files in {store.directory}; {SETUP_HINT}"
        if store.problem:
            log.warning("%s", store.problem)
        for path in files or []:
            store._add_file(path)
        log.info(
            "loaded %d recipes from %s (%d skipped)",
            len(store.recipes),
            store.directory,
            len(store.errors),
        )
        return store

    def _add_file(self, path: Path) -> None:
        try:
            recipe = parse_recipe_file(path)
            if recipe.id in self.recipes:
                raise ValueError(f"duplicate recipe id {recipe.id!r}")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
            log.warning("skipping recipe %s: %s", path.name, reason)
            self.errors.append(RecipeError(path.name, reason))
            return
        self.recipes[recipe.id] = recipe

    def get(self, recipe_id: str) -> Recipe | None:
        return self.recipes.get(recipe_id)

    def ids(self) -> list[str]:
        return sorted(self.recipes)

    def list(
        self,
        *,
        max_budget_div: float | None = None,
        tags: list[str] | None = None,
        include_unverified: bool = False,
    ) -> RecipeListing:
        """Recipes matching any given tag and within budget, never deprecated ones.

        With a budget cap, recipes without `budget_div` are left out (`hidden_no_budget`);
        `hidden_unverified` counts the rest that are left out because they are not verified.
        """
        wanted = {tag.lower() for tag in tags or []}
        shown: list[Recipe] = []
        hidden = no_budget = 0
        for recipe in self.recipes.values():
            if recipe.status == "deprecated":
                continue
            if wanted and not wanted & {tag.lower() for tag in recipe.tags}:
                continue
            if max_budget_div is not None:
                if recipe.budget_div is None:
                    no_budget += 1
                    continue
                if recipe.budget_div > max_budget_div:
                    continue
            if recipe.is_verified or include_unverified:
                shown.append(recipe)
            else:
                hidden += 1
        return RecipeListing(shown, hidden, no_budget)


def parse_recipe_file(path: Path) -> Recipe:
    """Parse and fully validate one file; raises ValueError with a readable reason."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read file: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {' '.join(str(exc).split())}") from exc
    if not isinstance(raw, dict):
        raise ValueError("top level must be a mapping of recipe fields")
    problems = schema_problems(raw)
    if problems:
        raise ValueError("schema: " + "; ".join(problems))
    try:
        return Recipe.model_validate(raw)
    except ValidationError as exc:
        details = [
            ": ".join(filter(None, (".".join(map(str, err["loc"])), _plain(err["msg"]))))
            for err in exc.errors(include_input=False, include_url=False, include_context=False)
        ]
        raise ValueError("; ".join(details[:MAX_REASONS])) from None


def _plain(message: str) -> str:
    return message.removeprefix("Value error, ")


def schema_problems(raw: object) -> list[str]:
    """Readable JSON Schema violations (at most a few), empty when `raw` conforms."""
    problems: list[str] = []
    for error in sorted(_validator().iter_errors(raw), key=lambda e: list(map(str, e.path))):
        leaf = best_match(error.context) if error.context else error
        where = "/".join(str(p) for p in leaf.absolute_path) or "<root>"
        text = f"{where}: {leaf.message}"
        if text not in problems:
            problems.append(text)
    return problems[:MAX_REASONS]
