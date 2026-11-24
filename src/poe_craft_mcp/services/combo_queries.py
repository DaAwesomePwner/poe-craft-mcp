"""Trade search bodies for cluster combos.

Every sale search shares one jewel filter: the base type, rarity, not corrupted, the enchant
option and the exact passive count (no item level filter). Prices are chaos-orb equivalents
(a `price` filter without an `option`). The trade client forces instant buyout on every search.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

ENCHANT_STAT = "enchant.stat_3948993189"
PASSIVES_STAT = "enchant.stat_3086156145"
BASE_ILVL_CAP = 83
"""A white base is bought at item level `ilvl`..83 (..100 above 83)."""

SALE_FILTER_ASSUMPTION = (
    "Sale searches match the base type, rare, not corrupted, the enchant and exactly the given "
    "passive count, with no item level filter (any ilvl that rolled the notables sells alike)"
)


@dataclass(frozen=True)
class JewelFilter:
    base: str
    """Trade `type`: the base type name, e.g. 'Medium Cluster Jewel'."""
    enchant_option: int
    passives: int


def _enchant_group(jewel: JewelFilter) -> dict[str, Any]:
    return {
        "type": "and",
        "filters": [
            {"id": ENCHANT_STAT, "value": {"option": jewel.enchant_option}},
            {"id": PASSIVES_STAT, "value": {"min": jewel.passives, "max": jewel.passives}},
        ],
    }


def _ids(trade_ids: Iterable[str]) -> list[dict[str, str]]:
    return [{"id": trade_id} for trade_id in sorted(trade_ids)]


def _body(
    jewel: JewelFilter,
    groups: list[dict[str, Any]],
    *,
    rarity: str = "rare",
    price: dict[str, float] | None = None,
    ilvl: tuple[int, int] | None = None,
) -> dict[str, Any]:
    misc: dict[str, Any] = {"corrupted": {"option": "false"}}
    if ilvl is not None:
        misc["ilvl"] = {"min": ilvl[0], "max": ilvl[1]}
    filters: dict[str, Any] = {
        "misc_filters": {"filters": misc},
        "type_filters": {"filters": {"rarity": {"option": rarity}}},
    }
    if price is not None:
        filters["trade_filters"] = {"filters": {"price": price}}
    return {
        "query": {
            "status": {"option": "securable"},
            "type": jewel.base,
            "stats": [_enchant_group(jewel), *groups],
            "filters": filters,
        },
        "sort": {"price": "asc"},
    }


def chaos_bound(value: float) -> float:
    return round(value, 2)


def discovery_query(
    jewel: JewelFilter,
    trade_ids: Iterable[str],
    *,
    below: float | None = None,
    at_least: float | None = None,
) -> dict[str, Any]:
    """Rares with two or more of `trade_ids`, priced at most `below` or at least `at_least`
    chaos (exactly one bound)."""
    if below is not None and at_least is None:
        price = {"max": chaos_bound(below)}
    elif at_least is not None and below is None:
        price = {"min": chaos_bound(at_least)}
    else:
        raise ValueError("pass exactly one of below or at_least")
    group = {"type": "count", "value": {"min": 2}, "filters": _ids(trade_ids)}
    return _body(jewel, [group], price=price)


def pair_query(jewel: JewelFilter, a: str, b: str) -> dict[str, Any]:
    """Rares holding both notables (trade ids `a` and `b`)."""
    return _body(jewel, [{"type": "and", "filters": _ids((a, b))}])


def miss_query(jewel: JewelFilter, anchor_ids: Iterable[str]) -> dict[str, Any]:
    """Rares holding exactly one of the anchor notables: what a missed attempt sells as."""
    group = {"type": "count", "value": {"min": 1, "max": 1}, "filters": _ids(anchor_ids)}
    return _body(jewel, [group])


def base_ilvl_range(ilvl: int) -> tuple[int, int]:
    return (ilvl, BASE_ILVL_CAP) if ilvl <= BASE_ILVL_CAP else (ilvl, 100)


def base_query(jewel: JewelFilter, ilvl: int) -> dict[str, Any]:
    """White (normal) jewels with the enchant and passives, item level `base_ilvl_range`."""
    return _body(jewel, [], rarity="normal", ilvl=base_ilvl_range(ilvl))
