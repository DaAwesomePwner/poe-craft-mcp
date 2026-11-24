"""Ranking, stop reasons and the stated assumptions of `cluster_combo_survey`."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from poe_craft_mcp.clients.ninja_models import Dataset
from poe_craft_mcp.services.combo_discovery import Discovery
from poe_craft_mcp.services.combo_queries import SALE_FILTER_ASSUMPTION

if TYPE_CHECKING:
    from poe_craft_mcp.services.combo_survey_enchant import Plan

SURVEY_ASSUMPTIONS = (
    "Only instant-buyout (asynchronous trade) listings are used; whisper listings never are",
    SALE_FILTER_ASSUMPTION,
    "Threshold T = min_value_div x poe.ninja chaos per Divine",
    "PROVISIONAL: every value here comes from the two discovery searches only (rares with two "
    "or more of the enchant's priceable notables, priced below T and at or above T); no exact "
    "pair search confirms it. Run cluster_combos on an enchant to confirm its pairs and price "
    "the craft",
    "Candidate: a pair with fewer than min_listings discovery listings below T and at least "
    "one at or above T. Provisional value: its min_listings-th cheapest listing at or above T "
    "(its cheapest one there when it has fewer), less undercut_pct; provisionally sellable "
    "when that value is at least T",
    "p_any_provisional: exact chance per attempt (Alteration/Augmentation until an anchor "
    "notable shows, then one Regal) that the rare holds a provisionally sellable pair; the "
    "hit value per attempt has no miss value and no costs",
    "Discovery listings with a third notable the base can roll at any item level are never "
    "counted as a pair listing; enchants with a notable that has no explicit trade id are "
    "skipped",
    "Enchants whose two discovery searches are cached and fresh are surveyed first with no "
    "request; a discovery result served from a stale cache (the trade site refused a new "
    "search) is never used: that enchant stays pending",
)


def rank(rows: list[dict[str, Any]], order: Sequence[str]) -> list[dict[str, Any]]:
    """p_any_provisional, then provisional sellable pairs (both descending), then the
    enchant's file order."""
    position = {enchant_id: i for i, enchant_id in enumerate(order)}
    return sorted(
        rows,
        key=lambda r: (
            -(r["p_any_provisional"] or 0.0),
            -r["provisional_sellable_pairs"],
            position[r["enchant_id"]],
        ),
    )


def stop_reason(retry_after: float, now: float, deadline: float) -> str:
    if now + retry_after > deadline:
        return (
            "max_seconds time budget spent (the next trade search would wait about "
            f"{retry_after:.0f}s)"
        )
    return f"trade site rate limited; retry in about {retry_after:.0f}s"


def continue_note(cache_minutes: int) -> str:
    return (
        "call again with the same inputs to continue with the pending enchants: discovery "
        f"searches are cached for {cache_minutes} minutes and reused without a new search"
    )


def rate_warnings(currency: Dataset, league: str) -> list[str]:
    warnings = []
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
    return warnings


def truncation_report(discs: Iterable[tuple[Plan, Discovery]], from_cache: int) -> dict[str, Any]:
    """Discovery totals over the surveyed enchants and which searches were truncated."""
    done = list(discs)
    truncated = []
    for plan, disc in done:
        for name, found in (("below T", disc.below), ("at or above T", disc.above)):
            if found.truncated:
                truncated.append(
                    f"{plan.enchant.name}: {name} fetched {found.fetched} of {found.total}"
                )
    report: dict[str, Any] = {
        "enchants_surveyed": len(done),
        "from_cache": from_cache,
        "listings_fetched": sum(d.below.fetched + d.above.fetched for _, d in done),
        "listings_total": sum(d.below.total + d.above.total for _, d in done),
        "truncated": truncated,
    }
    if truncated:
        report["truncation_effect"] = (
            "only the cheapest listings were fetched: below T a pair may look scarcer than it "
            "is; at or above T pairs listed only at higher prices are missed"
        )
    return report
