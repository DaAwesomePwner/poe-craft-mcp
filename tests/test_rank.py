import pytest

from poe_craft_mcp.engine.profit import evaluate
from poe_craft_mcp.engine.rank import RankResult, rank
from poe_craft_mcp.engine.types import Breakdown, Craft, Material, Outcome, Step

RATE = 200.0


def craft(
    id: str,
    *,
    attempt_cost: float = 10,
    p: float | None = 0.1,
    p25: float = 500,
    listings: int | None = 30,
    minutes: float = 6,
    volume: float | None = None,
) -> Breakdown:
    """C = attempt_cost / p (or attempt_cost); outcome pays p25 * 0.95 for sure."""
    step = Step("spam", (Material("Mat", 1, attempt_cost, volume=volume),), p_success=p)
    out = Outcome("hit", 1.0, p25=p25, listing_count=listings)
    return evaluate(Craft(id, id, minutes, (step,), (out,)))


def ids(result: RankResult) -> list[str]:
    return [r.breakdown.id for r in result.items]


def test_sorted_by_profit_per_hour_desc() -> None:
    cands = [craft("slow", minutes=30), craft("fast", minutes=3), craft("mid", minutes=6)]
    assert ids(rank(cands, 10, RATE, risk="high")) == ["fast", "mid", "slow"]


def test_ties_break_on_profit_then_id() -> None:
    a, b = craft("b-tie"), craft("a-tie")  # identical: profit 375 at 3750/hr
    top = craft("top", p25=1000, minutes=12)  # 850 profit, 4250/hr
    rate_tie = craft("rate-tie", p25=1000, attempt_cost=20, minutes=12)  # 750 profit, 3750/hr
    result = rank([a, b, top, rate_tie], 10, RATE, risk="high")
    assert ids(result) == ["top", "rate-tie", "a-tie", "b-tie"]
    assert ids(rank([rate_tie, top, b, a], 10, RATE, risk="high")) == ids(result)


def test_risk_levels_filter_on_c90_c_or_nothing() -> None:
    # C = 100c, C90 = 220c; budget 0.5 div = 100c at 200c/div
    cand = [craft("x")]
    assert ids(rank(cand, 0.5, RATE, risk="medium")) == ["x"]
    low = rank(cand, 0.5, RATE, risk="low")
    assert ids(low) == [] and "C90 220c exceeds budget 100c" in low.excluded[0].reason
    assert ids(rank(cand, 1.1, RATE, risk="low")) == ["x"]
    assert ids(rank(cand, 0.49, RATE, risk="high")) == ["x"]
    medium = rank(cand, 0.49, RATE, risk="medium")
    assert "C 100c exceeds budget 98c" in medium.excluded[0].reason


def test_liquidity_drops_candidates_with_thin_outcomes() -> None:
    cands = [craft("deep", listings=10), craft("thin", listings=9), craft("unknown", listings=None)]
    result = rank(cands, 10, RATE, risk="high")
    assert ids(result) == ["deep"]
    reasons = {e.id: e.reason for e in result.excluded}
    assert "9 listings (< 10)" in reasons["thin"]
    assert "unknown listing count" in reasons["unknown"]
    assert ids(rank(cands, 10, RATE, min_listings=5, risk="high"))[:2] == ["deep", "thin"]


def test_max_minutes_filters_on_minutes_per_attempt() -> None:
    result = rank([craft("a", minutes=10), craft("b", minutes=5)], 10, RATE, max_minutes=5)
    assert ids(result) == ["b"]
    assert result.excluded[0].id == "a" and "max_minutes" in result.excluded[0].reason


def test_low_volume_materials_are_flagged_not_dropped() -> None:
    result = rank([craft("v", volume=50), craft("ok", volume=500)], 10, RATE, risk="high")
    flags = {r.breakdown.id: r.flags for r in result.items}
    assert len(flags["v"]) == 1 and "Mat volume 50 < 10 x expected qty 10" in flags["v"][0]
    assert flags["ok"] == ()


def test_unpriced_inputs_are_excluded() -> None:
    step = Step("spam", (Material("Mystery", 1, None),), p_success=0.5)
    bad = evaluate(
        Craft("bad", "bad", 5, (step,), (Outcome("hit", 1.0, p25=10, listing_count=50),))
    )
    result = rank([bad], 10, RATE)
    assert result.items == () and "unpriced inputs" in result.excluded[0].reason


def test_empty_results_always_carry_reasons() -> None:
    assert rank([], 10, RATE).note == "no candidate recipes were supplied"
    result = rank([craft("a", listings=1)], 10, RATE)
    assert result.items == () and result.excluded[0].id == "a"
    assert result.note is not None and "all 1 candidates were excluded" in result.note


def test_limit_truncates_and_reports_omitted() -> None:
    cands = [craft(f"c{i}", minutes=i + 1) for i in range(4)]
    result = rank(cands, 10, RATE, risk="high", limit=2)
    assert ids(result) == ["c0", "c1"] and result.omitted_by_limit == 2 and result.note is None


def test_argument_validation() -> None:
    with pytest.raises(ValueError):
        rank([], 1, RATE, risk="reckless")
    with pytest.raises(ValueError):
        rank([], 1, RATE, limit=0)
    with pytest.raises(ValueError):
        rank([], 1, 0)


def test_divine_budget_converts_only_for_the_comparison() -> None:
    cand = craft("x")
    result = rank([cand], 0.5, RATE)
    assert result.items[0].breakdown.c == pytest.approx(100)


def test_sole_unpriced_zero_listing_outcome_is_never_ranked() -> None:
    step = Step("spam", (Material("Mat", 1, 1),), p_success=0.5)
    unsellable = evaluate(
        Craft("dud", "dud", 5, (step,), (Outcome("hit", 1.0, p25=None, listing_count=0),))
    )
    result = rank([unsellable], 10, RATE, min_listings=10, risk="high")
    assert result.items == ()
    assert result.excluded[0].id == "dud" and "no price" in result.excluded[0].reason


def test_unpriced_outcome_with_enough_listings_is_still_excluded() -> None:
    step = Step("spam", (Material("Mat", 1, 1),), p_success=0.5)
    out = (Outcome("hit", 0.5, p25=100, listing_count=50), Outcome("brick", 0.5, listing_count=50))
    result = rank([evaluate(Craft("c", "c", 5, (step,), out))], 10, RATE, risk="high")
    assert result.items == () and "'brick' has no price" in result.excluded[0].reason


def test_budget_exactly_on_the_line_survives_float_noise() -> None:
    cand = craft("exact", attempt_cost=2.9, p=0.1)  # C = 29c (within float noise), C90 = 63.8c
    assert 0.29 * 100 < 29  # the divine conversion lands just below 29c
    assert ids(rank([cand], 0.29, 100.0, risk="medium")) == ["exact"]
    assert ids(rank([cand], 0.638, 100.0, risk="low")) == ["exact"]
    assert ids(rank([cand], 0.28, 100.0, risk="medium")) == []


def test_material_demand_is_aggregated_across_steps() -> None:
    steps = (
        Step("one", (Material("Orb", 6, 1, volume=100),)),
        Step("two", (Material("Orb", 6, 1, volume=100),)),
    )
    out = (Outcome("hit", 1.0, p25=1000, listing_count=50),)
    b = evaluate(Craft("agg", "agg", 5, steps, out))
    flags = rank([b], 10, RATE, risk="high").items[0].flags
    assert flags == ("low volume: Orb volume 100 < 10 x expected qty 12",)
