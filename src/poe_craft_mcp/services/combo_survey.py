"""Which cluster enchant of one size has the most notable pairs worth at least T.

For every enchant: the `cluster_combos` guards, then discovery only (two searches, cached for
trade_cache_min). Candidates get a PROVISIONAL value from their discovery listings at or
above T, and the exact odds engine gives the chance per attempt of landing any provisional
sellable pair. Nothing is confirmed: `cluster_combos` confirms one enchant with exact pair
searches and prices the whole craft. Enchants with fresh cached discovery go first, read
straight from the cache (no requests), then live discovery one band at a time until
max_seconds, a rate limit or a stale band; the rest stay pending.
Only instant-buyout listings are used, never whisper listings.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

from poe_craft_mcp.clients.ninja import rates_from
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.gamedata.cluster import CLUSTER_DOMAIN
from poe_craft_mcp.gamedata.combo_pairs import SMALL_TAG
from poe_craft_mcp.gamedata.lookup import resolve_base
from poe_craft_mcp.gamedata.models import FETCH_HINT, ClusterEnchant, GameData, GameDataError
from poe_craft_mcp.services.cluster_combos import _rates
from poe_craft_mcp.services.combo_discovery import Discovery
from poe_craft_mcp.services.combo_report import unique, unique_sources
from poe_craft_mcp.services.combo_request import ComboRequest
from poe_craft_mcp.services.combo_survey_discovery import Failed, Stop, survey_discovery
from poe_craft_mcp.services.combo_survey_enchant import (
    Plan,
    evaluate,
    plan_enchant,
    select_enchants,
    skip_row,
)
from poe_craft_mcp.services.combo_survey_report import (
    SURVEY_ASSUMPTIONS,
    continue_note,
    rank,
    rate_warnings,
    truncation_report,
)

__all__ = ["SurveyRequest", "cluster_combo_survey"]


@dataclass(frozen=True)
class SurveyRequest:
    base: str
    passives: int
    ilvl: int
    min_value_div: float = 1.0
    min_listings: int = 3
    discovery_sample: int = 100
    max_seconds: float = 90.0
    enchants: tuple[str, ...] | None = None
    limit: int = 10

    def check(self) -> None:
        """Raise ValueError for an argument out of range (the `cluster_combos` rules)."""
        ComboRequest(
            base=self.base,
            enchant="",
            passives=self.passives,
            ilvl=self.ilvl,
            min_value_div=self.min_value_div,
            min_listings=self.min_listings,
            discovery_sample=self.discovery_sample,
            max_seconds=self.max_seconds,
        ).check()
        if self.limit < 1:
            raise ValueError(f"limit must be >= 1 (got {self.limit})")
        if self.enchants is not None and not self.enchants:
            raise ValueError("enchants must name at least one enchant, or be omitted for all")


def _plans(
    data: GameData, req: SurveyRequest, enchants: list[ClusterEnchant]
) -> tuple[list[Plan], list[dict[str, str]], list[str]]:
    plans, skipped, warnings = [], [], []
    for enchant in enchants:
        got = plan_enchant(data, req, enchant)
        if isinstance(got, str):
            skipped.append(skip_row(enchant, got))
            continue
        plans.append(got)
        warnings += got.info.warnings
        if got.split.unpriceable_pairs:
            n = len(got.split.unpriceable_pairs)
            warnings.append(
                f"{enchant.name}: {n} legal pair(s) hold a notable without its own trade id "
                "and are left out of the survey"
            )
    return plans, skipped, warnings


async def cluster_combo_survey(
    ctx: AppContext, data: GameData, req: SurveyRequest
) -> tuple[dict[str, Any], list[Source]]:
    """(payload, envelope sources). Raises ValueError or GameDataError for bad inputs and
    PricingUnavailable when the divine rate is unavailable."""
    req.check()
    item, warnings = resolve_base(data, req.base)
    if item.domain != CLUSTER_DOMAIN:
        raise GameDataError(f"{item.name} is not a cluster jewel; combos need a cluster jewel")
    out: dict[str, Any] = {"base": item.name, "ilvl": req.ilvl, "passives": req.passives}
    assumptions = list(SURVEY_ASSUMPTIONS)
    if SMALL_TAG in item.tags:
        out.update(ranking=[], note="a Small Cluster Jewel holds one notable: no pairs to survey")
        return {**out, "assumptions": assumptions, "warnings": warnings}, []
    cluster = data.clusters.get(item.id)
    if cluster is None:
        raise GameDataError(f"cluster_jewels.json has no {item.name} — {FETCH_HINT}")
    if not cluster.min_skills <= req.passives <= cluster.max_skills:
        raise ValueError(
            f"{item.name} adds {cluster.min_skills}-{cluster.max_skills} passive skills "
            f"(got passives={req.passives})"
        )
    selected, skipped = select_enchants(cluster, req.enchants)
    plans, more_skipped, plan_warnings = await asyncio.to_thread(_plans, data, req, selected)
    skipped += more_skipped
    warnings += plan_warnings
    out["skipped"] = skipped
    if not plans:
        out.update(ranking=[], pending=[], note="no enchant has a priceable notable pair")
        return {**out, "assumptions": assumptions, "warnings": unique(warnings)}, []

    league, currency, per_divine = await _rates(ctx)
    rates = rates_from(currency.rows)
    warnings += rate_warnings(currency, league)
    threshold = req.min_value_div * per_divine
    undercut = ctx.config.undercut_pct / 100
    out.update(
        league=league,
        threshold={"divine": req.min_value_div, "chaos": round(threshold, 2)},
        chaos_per_divine=per_divine,
        undercut_pct=ctx.config.undercut_pct,
        min_listings=req.min_listings,
    )

    clock = ctx.cache.clock
    deadline = clock() + req.max_seconds
    found: list[tuple[Plan, Discovery]] = []
    live: list[Plan] = []
    for plan in plans:  # cache phase: never makes a request
        args = (ctx, league, plan, req, threshold, rates)
        got = await survey_discovery(*args, deadline=deadline, cache_only=True)
        if isinstance(got, Discovery):
            found.append((plan, got))
        else:
            live.append(plan)
    n_cached = len(found)
    stopped, retry_after, pending = None, None, []
    for i, plan in enumerate(live):
        if clock() >= deadline:
            stopped, pending = "max_seconds time budget spent", live[i:]
            break
        args = (ctx, league, plan, req, threshold, rates)
        got = await survey_discovery(*args, deadline=deadline, cache_only=False)
        if isinstance(got, Discovery):
            found.append((plan, got))
        elif isinstance(got, Failed):
            skipped.append(skip_row(plan.enchant, got.reason))
        elif isinstance(got, Stop):  # rate limit, deadline or a stale band: stays pending
            stopped, pending = got.reason, live[i:]
            retry_after = None if got.retry_after is None else math.ceil(got.retry_after)
            break

    scored, sources = [], [currency.sources()]
    for plan, disc in found:
        scored.append(await evaluate(data, req, plan, disc, threshold, undercut, per_divine))
        sources.append(disc.sources)
    ranking = rank(scored)
    out.update(
        ranking=ranking[: req.limit],
        ranked_count=len(ranking),
        pending=[{"enchant": p.enchant.name, "enchant_id": p.enchant.id} for p in pending],
        stopped=stopped,
        retry_after_seconds=retry_after,
        discovery=truncation_report(found, n_cached),
        next_step="confirm the top enchants with cluster_combos (exact pair searches, miss "
        "value, base and orb costs, profit per hour) before crafting",
    )
    if len(ranking) > req.limit:
        warnings.append(f"ranking capped at limit={req.limit} of {len(ranking)} enchants")
    if pending:
        out["note"] = continue_note(ctx.config.trade_cache_min)
        warnings.append(f"survey stopped early: {stopped}; {len(pending)} enchant(s) pending")
    payload = {**out, "assumptions": assumptions, "warnings": unique(warnings)}
    return payload, unique_sources(sources)
