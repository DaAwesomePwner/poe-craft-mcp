"""Sellable notable pairs on one cluster jewel and the value of crafting for any of them.

1. Discovery (two searches) sorts every priceable legal pair into not_sellable, candidate,
   thin or unlisted (see `combo_discovery`).
2. Confirmation prices candidates with their exact pair search, cached ones first (see
   `combo_confirm`).
3. With at least one sellable pair: exact odds of the Alteration/Augmentation anchor cycle
   plus one Regal (`gamedata.combos.combo_odds`), miss value, white base and orb costs.
Only instant-buyout listings are used, never whisper listings.
"""

from __future__ import annotations

from typing import Any

from poe_craft_mcp.clients.ninja import get_ninja, rates_from
from poe_craft_mcp.clients.ninja_models import Dataset, NinjaError, divine_chaos
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.gamedata.combos import cluster_notables
from poe_craft_mcp.gamedata.models import GameData
from poe_craft_mcp.http.fetch import FetchError
from poe_craft_mcp.services.combo_confirm import Rules, confirm, order_candidates
from poe_craft_mcp.services.combo_discovery import discover
from poe_craft_mcp.services.combo_notables import (
    enchant_option,
    exclusion_ids,
    pair_names,
    pair_row,
    priceable,
)
from poe_craft_mcp.services.combo_queries import JewelFilter, pair_query
from poe_craft_mcp.services.combo_report import (
    ROW_LIMIT,
    classes_report,
    confirmation_report,
    sellable_rows,
    unique,
    unique_sources,
    why_rows,
)
from poe_craft_mcp.services.combo_request import PRICING_ASSUMPTIONS, ComboRequest
from poe_craft_mcp.services.combo_value import price_sellable
from poe_craft_mcp.services.trade_pricing import PricingUnavailable

__all__ = ["ComboRequest", "cluster_combos"]


async def _rates(ctx: AppContext) -> tuple[str, Dataset, float]:
    ninja = get_ninja(ctx)
    try:
        league = await ninja.current_league()
        currency = await ninja.dataset("Currency", league)
        return league.id, currency, divine_chaos(currency.rows)
    except (FetchError, NinjaError) as exc:
        raise PricingUnavailable(f"poe.ninja currency rates unavailable: {exc}") from None


async def cluster_combos(
    ctx: AppContext, data: GameData, req: ComboRequest
) -> tuple[dict[str, Any], list[Source]]:
    """(payload, envelope sources). Raises ValueError or GameDataError for bad inputs and
    PricingUnavailable when the divine rate or the discovery searches are unavailable."""
    req.check()
    info = cluster_notables(data, req.base, req.ilvl, req.enchant, req.passives)
    out: dict[str, Any] = {
        "base": info.item.name,
        "ilvl": req.ilvl,
        "enchant": info.enchant,
        "passives": req.passives,
    }
    warnings = list(info.warnings)
    assumptions = [*PRICING_ASSUMPTIONS, *info.assumptions]
    if not info.pairs:
        out.update(legal_pairs=0, sellable_pairs=[], economics=None)
        out["note"] = "no two notables fit on one rare of this base and enchant: no pairs to price"
        return {**out, "assumptions": assumptions, "warnings": warnings}, []

    option = enchant_option(data, info)
    split = priceable(data, info)
    warnings += split.warnings
    # Sale searches accept any item level: rule out every notable the base can ever roll.
    top = cluster_notables(data, req.base, 100, req.enchant, req.passives)
    excluded, unidentifiable = exclusion_ids(data, info, top)
    out["notables"] = {
        "count": len(info.notables),
        "priceable": len(split.trade_ids),
        "legal_pairs": len(info.pairs),
        "priceable_pairs": len(split.pairs),
        "unpriceable_pairs": pair_names(data, split.unpriceable_pairs, ROW_LIMIT),
    }
    if not split.pairs:
        out.update(sellable_pairs=[], economics=None)
        out["note"] = "no legal pair has two notables with their own trade ids: nothing to price"
        return {**out, "assumptions": assumptions, "warnings": warnings}, []

    league, currency, per_divine = await _rates(ctx)
    rates = rates_from(currency.rows)
    if currency.stale:
        warnings.append(
            "poe.ninja currency rates are an old cached copy (poe.ninja is failing); listing "
            "prices are converted with them"
        )
    if (currency.league_source or {}).get("stale"):
        warnings.append(
            f"the poe.ninja league lookup is an old cached copy; league {league!r} may be out "
            "of date"
        )
    threshold = req.min_value_div * per_divine
    undercut = ctx.config.undercut_pct / 100
    out.update(
        league=league,
        threshold={"divine": req.min_value_div, "chaos": round(threshold, 2)},
        chaos_per_divine=per_divine,
        undercut_pct=ctx.config.undercut_pct,
        min_listings=req.min_listings,
    )
    jewel = JewelFilter(info.item.name, option, req.passives)
    deadline = ctx.cache.clock() + req.max_seconds
    disc = await discover(
        ctx,
        league,
        jewel,
        split.mod_by_trade,
        excluded,
        split.pairs,
        threshold,
        sample=req.discovery_sample,
        min_listings=req.min_listings,
        rates=rates,
        deadline=deadline,
    )
    warnings += disc.warnings
    if any(src.get("stale") for src in disc.sources):
        warnings.append(
            "a discovery search was served from a stale cache (the trade site refused a new "
            "search); its classes may be out of date"
        )
    out["discovery"] = {
        "below_threshold": disc.below.report(),
        "at_or_above_threshold": disc.above.report(),
        "classes": classes_report(disc),
    }

    weights = {e.mod.id: e.weight for e in info.notables}
    ordered = order_candidates([p for p, c in disc.classes.items() if c == "candidate"], weights)
    ids = split.trade_ids
    if unidentifiable:  # a jewel carrying one of them would pass any pair or miss search
        names = ", ".join(unidentifiable)
        out.update(
            sellable_pairs=[],
            economics=None,
            unconfirmed_count=len(ordered),
            unconfirmed=[pair_row(data, p, ids) for p in ordered[:ROW_LIMIT]],
            note=f"cannot rule out jewels carrying {names} (no explicit trade id in the game "
            "data), so no pair is confirmed or valued; refresh the game data export",
        )
        payload = {**out, "assumptions": assumptions, "warnings": unique(warnings)}
        return payload, unique_sources([currency.sources(), disc.sources])
    sample = req.pair_sample(ctx.config.trade_sample)
    rules = Rules(threshold, req.min_listings, undercut, req.max_pair_searches, deadline, sample)
    conf = await confirm(
        ctx,
        league,
        ordered,
        lambda p: pair_query(jewel, *(ids[m] for m in sorted(p)), excluded),
        rules,
    )
    cache_min = ctx.config.trade_cache_min
    out["confirmation"] = confirmation_report(data, conf, len(ordered), ids, cache_min)
    warnings += [f"{pair_row(data, p, ids)['pair']}: {e}" for p, e in conf.errors.items()]
    if conf.stopped:
        warnings.append(f"confirmation stopped early: {conf.stopped}")
    sources = [currency.sources(), disc.sources, conf.sources]

    if not conf.sellable:
        out.update(sellable_pairs=[], economics=None, **why_rows(data, disc, conf, ids, per_divine))
        out["note"] = f"no pair confirmed sellable at {req.min_value_div:g} divine or more"
        all_sources = unique_sources(sources)
        return {**out, "assumptions": assumptions, "warnings": unique(warnings)}, all_sources

    valued = await price_sellable(
        ctx, data, req, info, conf, ids, excluded, jewel, league, rates, per_divine
    )
    out["sellable_pairs"] = sellable_rows(data, conf, ids, per_divine, valued.odds)
    out["economics"] = valued.economics
    out["miss_listings"], out["base_listings"] = valued.miss_listings, valued.base_listings
    warnings += valued.warnings
    assumptions = [*PRICING_ASSUMPTIONS, *valued.odds.assumptions]
    sources.append(valued.sources)
    payload = {**out, "assumptions": unique(assumptions), "warnings": unique(warnings)}
    return payload, unique_sources(sources)
