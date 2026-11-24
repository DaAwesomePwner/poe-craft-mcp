"""Compact, sorted report rows for `cluster_combos`."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.gamedata.combos import ComboOdds
from poe_craft_mcp.gamedata.models import GameData
from poe_craft_mcp.services.combo_confirm import Confirmation, PairPrice
from poe_craft_mcp.services.combo_costs import chance, money
from poe_craft_mcp.services.combo_discovery import CLASSES, Discovery
from poe_craft_mcp.services.combo_notables import pair_row

Pair = frozenset[str]
ROW_LIMIT = 10
WHY_LIMIT = 5
CONTINUE_HINT = (
    "call again with the same inputs to continue: confirmed pair searches are cached for "
    "{minutes} minutes and are reused without a new search"
)


def _priced(row: dict[str, Any], price: PairPrice, per_divine: float) -> dict[str, Any]:
    row.update(
        p25=money(price.p25),
        value_chaos=money(price.value),
        value_div=None if price.value is None else round(price.value / per_divine, 3),
        listings=price.listing_count,
        from_cache=price.cached,
    )
    if price.stale:
        row["stale"] = True
    return row


def sellable_rows(
    data: GameData,
    conf: Confirmation,
    trade_ids: Mapping[str, str],
    per_divine: float,
    odds: ComboOdds | None,
) -> list[dict[str, Any]]:
    """Every sellable pair (never capped: they all feed the odds), most valuable first."""
    rows = []
    for price in sorted(conf.sellable, key=lambda p: (-(p.value or 0), sorted(p.pair))):
        row = _priced(pair_row(data, price.pair, trade_ids), price, per_divine)
        row["p_credited"] = chance(odds.per_pair.get(price.pair)) if odds else None
        rows.append(row)
    return rows


def _discovery_row(
    data: GameData, pair: Pair, disc: Discovery, trade_ids: Mapping[str, str]
) -> dict[str, Any]:
    below, above = disc.below.pairs.get(pair, []), disc.above.pairs.get(pair, [])
    row = pair_row(data, pair, trade_ids)
    row.update(
        listings_below=len(below),
        cheapest_below=money(below[0]) if below else None,
        listings_at_or_above=len(above),
        cheapest_at_or_above=money(above[0]) if above else None,
    )
    return row


def classes_report(disc: Discovery) -> dict[str, int]:
    counts = Counter(disc.classes.values())
    return {name: counts.get(name, 0) for name in CLASSES}


def why_rows(
    data: GameData,
    disc: Discovery,
    conf: Confirmation,
    trade_ids: Mapping[str, str],
    per_divine: float,
) -> dict[str, list[dict[str, Any]]]:
    """With nothing sellable: the top not_sellable pairs (dearest first) and the candidates
    (confirmed ones with their exact-search price), so the user sees why."""
    not_sellable = [p for p, c in disc.classes.items() if c == "not_sellable"]
    not_sellable.sort(key=lambda p: (-disc.below.pairs[p][0], sorted(p)))
    confirmed = {p.pair: p for p in conf.priced}
    candidates = [p for p, c in disc.classes.items() if c == "candidate"]
    candidates.sort(key=lambda p: (-(confirmed[p].value or 0) if p in confirmed else 0, sorted(p)))
    cand_rows = []
    for pair in candidates[:WHY_LIMIT]:
        row = _discovery_row(data, pair, disc, trade_ids)
        if pair in confirmed:
            row["confirmed"] = _priced({}, confirmed[pair], per_divine)
        cand_rows.append(row)
    return {
        "top_not_sellable": [
            _discovery_row(data, p, disc, trade_ids) for p in not_sellable[:WHY_LIMIT]
        ],
        "top_candidates": cand_rows,
    }


def confirmation_report(
    data: GameData,
    conf: Confirmation,
    candidates: int,
    trade_ids: Mapping[str, str],
    cache_minutes: int,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "candidates": candidates,
        "from_cache": sum(p.cached for p in conf.priced),
        "live_searches": conf.searched,
        "sellable": len(conf.sellable),
        "not_sellable": len(conf.priced) - len(conf.sellable),
        "errors": [
            {**pair_row(data, pair, trade_ids), "error": error}
            for pair, error in sorted(conf.errors.items(), key=lambda kv: sorted(kv[0]))
        ][:ROW_LIMIT],
        "unconfirmed_count": len(conf.unconfirmed),
        "unconfirmed": [pair_row(data, p, trade_ids) for p in conf.unconfirmed[:ROW_LIMIT]],
        "stopped": conf.stopped,
    }
    if conf.unconfirmed:
        report["hint"] = CONTINUE_HINT.format(minutes=cache_minutes)
    return report


def unique_sources(groups: Iterable[Iterable[Source]]) -> list[Source]:
    seen: set[tuple[Any, ...]] = set()
    out = []
    for group in groups:
        for src in group:
            key = (src.get("name"), src.get("url"), src.get("search_id"))
            if key not in seen:
                seen.add(key)
                out.append(src)
    return out


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))
