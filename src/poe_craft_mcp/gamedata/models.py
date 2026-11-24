"""Parsed game data records: only the fields mod odds use."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    adds_tags: frozenset[str] = frozenset()
    """Tags the mod adds to the item once rolled (e.g. has_affliction_notable)."""
    stat_ids: tuple[str, ...] = ()
    """The mod's stat ids (mods.json stats[].id), in export order."""


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
class ClusterEnchant:
    """One 'Added Small Passive Skills grant: ...' enchant (RePoE cluster_jewels.json)."""

    id: str
    name: str
    tag: str
    """Tag the enchant gives the jewel; notables' spawn_weights reference it."""
    stat_text: tuple[str, ...]
    trade_option: int | None = None
    """Option of the trade filter enchant.stat_3948993189 for this enchant, or None (see
    records.enchant_trade_option)."""


@dataclass(frozen=True)
class ClusterJewel:
    base_id: str
    name: str
    min_skills: int
    max_skills: int
    enchants: tuple[ClusterEnchant, ...]


@dataclass(frozen=True)
class GameData:
    mods: dict[str, ModDef]
    bases: dict[str, BaseItem]
    essences: dict[str, Essence]
    fossils: dict[str, Fossil]
    game_version: str | None
    downloaded_at: datetime | None
    source_url: str
    clusters: dict[str, ClusterJewel] = field(default_factory=dict)
    """Cluster jewel bases by base id; empty when cluster_jewels.json is not in the export."""
    trade_stats: dict[str, str] = field(default_factory=dict)
    """Stat id -> explicit trade stat id (e.g. 'explicit.stat_2337273077'); empty when
    stat_translations.json is not in the export or maps nothing."""
    explicit_trade_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Stat id -> every explicit trade stat id that can show it, ambiguous ones included."""

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


def trade_stat_for(data: GameData, mod: ModDef) -> str:
    """The pathofexile.com trade stat id that searches for `mod` as an explicit affix.

    Only mods with exactly one stat whose translation has one explicit trade stat qualify."""
    if not data.trade_stats:
        raise GameDataError(
            f"no explicit trade stat ids available for {mod.id} (stat_translations.json "
            f"missing or has none for this mod); {FETCH_HINT}"
        )
    trade = data.trade_stats.get(mod.stat_ids[0]) if len(mod.stat_ids) == 1 else None
    if trade is None:
        stats = ", ".join(mod.stat_ids) or "none"
        raise GameDataError(
            f"{mod.id} has no single explicit trade stat (stats: {stats}); stat_translations "
            "maps only one-stat lines with exactly one explicit trade id"
        )
    return trade


def all_explicit_trade_ids(data: GameData, mod: ModDef) -> tuple[str, ...]:
    """Every explicit trade stat id that can show `mod`, shared or ambiguous ids included
    (for excluding it from a search); empty when none is known."""
    found: set[str] = set()
    for stat in mod.stat_ids:
        found.update(data.explicit_trade_ids.get(stat, ()))
        if stat in data.trade_stats:
            found.add(data.trade_stats[stat])
    return tuple(sorted(found))


def parse_patch(version: str | None) -> tuple[int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)", version or "")
    return (int(match[1]), int(match[2])) if match else None
