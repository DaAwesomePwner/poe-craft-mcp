"""Parsed game data records: only the fields mod odds use."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

MIN_TRUSTED_PATCH = (3, 30)
AFFIX_TYPES = ("prefix", "suffix")
FETCH_HINT = "run `uv run python scripts/fetch_gamedata.py` to download it"

TagWeights = tuple[tuple[str, int], ...]


class GameDataError(Exception):
    """The export is missing or unreadable, or a lookup in it failed."""


@dataclass(frozen=True)
class ModDef:
    id: str
    domain: str
    generation_type: str
    required_level: int
    groups: frozenset[str]
    tags: frozenset[str]
    spawn_weights: TagWeights
    generation_weights: TagWeights
    is_essence_only: bool
    text: str


@dataclass(frozen=True)
class BaseItem:
    id: str
    name: str
    domain: str
    item_class: str
    tags: frozenset[str]
    release_state: str
    prefix_delta: int = 0
    suffix_delta: int = 0
    """Change to the allowed prefix/suffix count from implicits (e.g. Helical Ring: -2, +1)."""


@dataclass(frozen=True)
class Essence:
    id: str
    name: str
    mods: dict[str, str]
    item_level_restriction: int | None


@dataclass(frozen=True)
class Fossil:
    id: str
    name: str
    weights: TagWeights
    added_mods: tuple[str, ...]
    forced_mods: tuple[str, ...]
    allowed_tags: frozenset[str]
    forbidden_tags: frozenset[str]
    unmodelled: tuple[str, ...]
    """Effects not expressible as weights/added/forced mods; such fossils are rejected."""


@dataclass(frozen=True)
class GameData:
    mods: dict[str, ModDef]
    bases: dict[str, BaseItem]
    essences: dict[str, Essence]
    fossils: dict[str, Fossil]
    game_version: str | None
    downloaded_at: datetime | None
    source_url: str
    load_warnings: tuple[str, ...] = ()

    @property
    def stale_warning(self) -> str | None:
        """A warning unless the export is confirmed to be built for 3.30 or later."""
        patch = parse_patch(self.game_version)
        if patch is None:
            return (
                "cannot confirm the game data export was built for 3.30+ (no game version in "
                f"manifest); 3.30 changed item generation so weights may be wrong — {FETCH_HINT}"
            )
        if patch < MIN_TRUSTED_PATCH:
            return (
                f"game data export is from {self.game_version}, before 3.30 changed item "
                f"generation; weights may be wrong — {FETCH_HINT}"
            )
        return None


def parse_patch(version: str | None) -> tuple[int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)", version or "")
    return (int(match[1]), int(match[2])) if match else None
