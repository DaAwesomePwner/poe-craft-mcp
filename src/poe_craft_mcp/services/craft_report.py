"""JSON views of an `Evaluation`: chaos values with divine equivalents for display only."""

from __future__ import annotations

import dataclasses
from typing import Any

from poe_craft_mcp.engine.profit import to_divine
from poe_craft_mcp.engine.types import Source
from poe_craft_mcp.envelope import source
from poe_craft_mcp.services.evaluate import Evaluation

MONEY = ("c_base", "c", "c90", "v", "profit", "profit_per_hour")
KB_SOURCE = source("recipe knowledge base")


def divines(values: dict[str, Any], chaos_per_divine: float | None) -> dict[str, Any] | None:
    if not chaos_per_divine:
        return None
    return {k: to_divine(values.get(k), chaos_per_divine) for k in MONEY if k in values}


def summary(ev: Evaluation, chaos_per_divine: float | None) -> dict[str, Any]:
    """Headline numbers (chaos), their divine equivalents and the completeness flag."""
    b = ev.breakdown
    money: dict[str, Any] = {k: getattr(b, k) if b else None for k in MONEY}
    return {
        "id": ev.recipe.id,
        "name": ev.recipe.name,
        "status": ev.recipe.status,
        "evaluable": b is not None,
        "complete": bool(b and b.complete),
        "incomplete_reasons": ev.incomplete_reasons(),
        "minutes": ev.recipe.minutes,
        **money,
        "roi": b.roi if b else None,
        "p_fail": b.p_fail if b else None,
        "divine": divines(money, chaos_per_divine),
    }


def full_report(ev: Evaluation, chaos_per_divine: float | None) -> dict[str, Any]:
    """Summary plus the engine breakdown, every priced input and the step odds."""
    b = ev.breakdown
    out = summary(ev, chaos_per_divine)
    out["chaos_per_divine"] = chaos_per_divine
    out["undercut"] = b.undercut if b else None
    out["breakdown"] = (
        {
            "base": dataclasses.asdict(b.base) if b.base else None,
            "steps": [dataclasses.asdict(s) for s in b.steps],
            "materials": [dataclasses.asdict(m) for m in b.materials],
            "outcomes": [dataclasses.asdict(o) for o in b.outcomes],
            "salvage": dataclasses.asdict(b.salvage) if b.salvage else None,
        }
        if b
        else None
    )
    out["inputs"] = [i.report() for i in ev.inputs]
    out["odds"] = [c.report() for c in ev.chances]
    out["warnings"] = ev.warnings
    return out


def collect_sources(evaluations: list[Evaluation], *extra: Source) -> list[Source]:
    """Envelope sources of every input, deduplicated by (name, url)."""
    seen: dict[tuple[str, str | None], Source] = {}
    candidates: list[Source] = [KB_SOURCE, *extra]
    for ev in evaluations:
        for i in ev.inputs:
            candidates += i.sources or ([i.source] if i.status == "ok" else [])
        candidates += [c.source for c in ev.chances if c.p is not None]
    for s in candidates:
        seen.setdefault((s["name"], s.get("url")), s)
    return list(seen.values())
