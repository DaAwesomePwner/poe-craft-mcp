"""poe.ninja types, row normalization and BaseType matching (pure functions, no I/O)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from poe_craft_mcp.envelope import source

Row = dict[str, Any]
Kind = Literal["exchange", "stash"]

EXCHANGE_TYPES: tuple[str, ...] = (
    "Currency", "Fragment", "Runegraft", "AllflameEmber", "Tattoo", "Omen", "DjinnCoin",
    "Ducat", "EnshroudingCrystal", "DivinationCard", "Artifact", "Oil", "DeliriumOrb",
    "Scarab", "Astrolabe", "Thread", "Fossil", "Resonator", "Essence",
)  # fmt: skip
STASH_TYPES: tuple[str, ...] = (
    "BaseType", "ClusterJewel", "Beast", "SkillGem", "ImbuedGem", "Flask", "Vial", "Map",
    "BlightedMap", "BlightRavagedMap", "UniqueMap", "ValdoMap", "Invitation", "Memory",
    "IncursionTemple", "ScryingOrb", "Wombgift", "Corpse", "Viscera", "Incubator",
    "UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueFlask", "UniqueJewel",
    "ForbiddenJewel", "ShrineBelt", "UniqueTincture", "UniqueRelic",
)  # fmt: skip
ALL_TYPES = EXCHANGE_TYPES + STASH_TYPES
_CANONICAL = {name.lower(): name for name in ALL_TYPES}

SNAPSHOT_MIN_LISTINGS = 20
"""Stash rows with fewer listings are too noisy (and too numerous) to keep as history."""


class NinjaError(Exception):
    """poe.ninja returned nothing usable."""


class UnknownCategory(NinjaError, ValueError):
    """The category is not a poe.ninja type this client knows."""

    def __init__(self, category: str) -> None:
        super().__init__(
            f"unknown category {category!r}; valid exchange types: {', '.join(EXCHANGE_TYPES)}; "
            f"valid stash types: {', '.join(STASH_TYPES)}"
        )


def resolve_type(category: str) -> tuple[Kind, str]:
    """Case-insensitive lookup of a poe.ninja type -> (endpoint kind, canonical type name)."""
    name = _CANONICAL.get(category.strip().lower())
    if name is None:
        raise UnknownCategory(category)
    return ("exchange" if name in EXCHANGE_TYPES else "stash"), name


@dataclass(frozen=True)
class League:
    id: str
    name: str
    age_seconds: float | None = None
    stale: bool = False
    url: str | None = None

    def source(self) -> dict[str, Any]:
        return source(
            "poe.ninja leagues", url=self.url, age_seconds=self.age_seconds, stale=self.stale
        )


@dataclass(frozen=True)
class Dataset:
    """Normalized rows of one poe.ninja type. `age_seconds=None` means nothing was fetched."""

    type: str
    kind: Kind
    league: str
    rows: list[Row]
    age_seconds: float | None = None
    stale: bool = False
    url: str | None = None
    version: str | None = None
    league_source: dict[str, Any] | None = None
    """Provenance of the league lookup that selected `league` (may itself be stale)."""

    def sources(self) -> list[dict[str, Any]]:
        """Envelope sources: the league lookup (when known) and this dataset."""
        return [*([self.league_source] if self.league_source else []), self.source()]

    def source(self) -> dict[str, Any]:
        return source(
            f"poe.ninja {self.type}",
            url=self.url,
            age_seconds=self.age_seconds,
            stale=self.stale,
            type=self.type,
            league=self.league,
        )


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def normalize_exchange(payload: Mapping[str, Any], type_name: str) -> list[Row]:
    """Rows from an exchange overview. Values are converted from `core.primary` to chaos."""
    core = payload.get("core") or {}
    primary = core.get("primary", "chaos")
    meta = {m["id"]: m for m in [*(core.get("items") or []), *(payload.get("items") or [])]}
    lines = payload.get("lines") or []
    rates = core.get("rates") or {}
    by_line = {line["id"]: line.get("primaryValue") for line in lines}

    def unit(currency: str) -> float | None:
        """Value of one `currency` in primary units: its own line, else `core.rates`."""
        if currency == primary:
            return 1.0
        if by_line.get(currency):
            return by_line[currency]
        return 1 / rates[currency] if rates.get(currency) else None

    chaos_unit, divine_unit = unit("chaos"), unit("divine")
    rows: list[Row] = []
    for line in lines:
        value, volume = line.get("primaryValue"), line.get("volumePrimaryValue")
        info = meta.get(line["id"], {})
        row: Row = {
            "name": info.get("name", line["id"]),
            "id": line["id"],
            "chaos": _round(value / chaos_unit) if value is not None and chaos_unit else None,
            "divine": _round(value / divine_unit) if value is not None and divine_unit else None,
            "volume_or_listings": _round(volume / chaos_unit) if volume and chaos_unit else None,
            "volume_kind": "chaos_volume",
            "sparkline_7d": (line.get("sparkline") or {}).get("data", []),
            "category": type_name,
        }
        if info.get("detailsId"):
            row["details_id"] = info["detailsId"]
        rows.append(row)
    return rows


def divine_chaos(rows: Sequence[Row]) -> float:
    """Chaos value of one Divine Orb from `Currency` rows."""
    for row in rows:
        if row["id"] == "divine" and row["chaos"]:
            return float(row["chaos"])
    raise NinjaError("poe.ninja Currency data has no Divine Orb price")


def normalize_stash(payload: Mapping[str, Any], type_name: str) -> list[Row]:
    """Rows from a stash item overview; `id` is the line's `detailsId` slug."""
    rows: list[Row] = []
    for line in payload.get("lines") or []:
        details_id = line.get("detailsId") or str(line.get("id"))
        row: Row = {
            "name": line.get("name"),
            "id": details_id,
            "chaos": line.get("chaosValue"),
            "divine": line.get("divineValue"),
            "volume_or_listings": line.get("listingCount"),
            "volume_kind": "listings",
            "sparkline_7d": (line.get("sparkLine") or {}).get("data", []),
            "category": type_name,
        }
        if line.get("variant"):
            row["variant"] = line["variant"]
        if line.get("links"):
            row["links"] = line["links"]
        if type_name == "BaseType":
            row["ilvl"] = line.get("levelRequired")
            row["influence"] = (line.get("variant") or "").split("/") if line.get("variant") else []
        rows.append(row)
    return rows


def _influence_set(value: str | Iterable[str]) -> set[str]:
    parts = value.replace("+", "/").split("/") if isinstance(value, str) else value
    return {p.strip().lower() for p in parts if p.strip()}


def match_base(
    rows: Sequence[Row],
    base: str,
    ilvl_min: int | None = None,
    influence: str | Iterable[str] | None = None,
) -> Row | None:
    """Cheapest row for BaseType `base` (case-insensitive) with `ilvl >= ilvl_min`.

    `influence=None` means a plain, uninfluenced base; `"any"` accepts every variant; otherwise
    the influences must match exactly, e.g. `"Shaper"` or `"Shaper/Hunter"`.
    """
    wanted = None if influence is None else _influence_set(influence)
    best: Row | None = None
    for row in rows:
        if (row.get("name") or "").lower() != base.strip().lower() or row.get("chaos") is None:
            continue
        if ilvl_min is not None and (row.get("ilvl") or 0) < ilvl_min:
            continue
        have = {i.lower() for i in row.get("influence", [])}
        if wanted is None and have:
            continue
        if wanted is not None and wanted != {"any"} and wanted != have:
            continue
        if best is None or (row["chaos"], -(row["volume_or_listings"] or 0)) < (
            best["chaos"],
            -(best["volume_or_listings"] or 0),
        ):
            best = row
    return best


def match_rows(rows: Sequence[Row], key: str) -> list[Row]:
    """Rows whose `id` equals `key` or, failing that, whose name does (case-insensitive)."""
    needle = key.strip().lower()
    by_id = [r for r in rows if str(r["id"]).lower() == needle]
    return by_id or [r for r in rows if (r.get("name") or "").lower() == needle]
