import math
import time

import pytest

from poe_craft_mcp.engine.profit import evaluate, expected_attempts, n90, to_divine
from poe_craft_mcp.engine.types import Craft, Material, Outcome, Step


@pytest.mark.parametrize(("p", "attempts", "bad_run"), [(0.1, 10, 22), (0.5, 2, 4), (1.0, 1, 1)])
def test_known_attempt_values(p: float, attempts: float, bad_run: int) -> None:
    assert expected_attempts(p) == pytest.approx(attempts)
    assert n90(p) == bad_run


@pytest.mark.parametrize("p", [0, -0.1, 1.5, float("nan")])
def test_invalid_probability_raises(p: float) -> None:
    with pytest.raises(ValueError):
        expected_attempts(p)
    with pytest.raises(ValueError):
        n90(p)


def test_n90_is_stable_at_exact_boundaries() -> None:
    assert n90(0.9) == 1
    assert n90(1e-9) > 1_000_000_000


def mat(name: str, qty: float, chaos: float | None, **kw: object) -> Material:
    return Material(name, qty, chaos, **kw)  # type: ignore[arg-type]


def worked_craft(**overrides: object) -> Craft:
    craft = Craft(
        id="worked",
        name="Worked example",
        minutes=6,
        base=mat("Astral Plate", 1, 20, source={"ninja": "base"}, age_seconds=60),
        steps=(
            Step("essence_spam", (mat("Essence", 1, 3), mat("Scour", 2, 0.5)), p_success=0.1),
            Step("bench", (mat("Divine", 0.1, 200),)),
        ),
        outcomes=(
            Outcome("hit", 0.3, p25=300, source={"trade": "q1"}, age_seconds=120, listing_count=40),
            Outcome("ok", 0.5, p25=100, listing_count=25),
            Outcome("fail", 0.2, is_failure=True),
        ),
        salvage=mat("Salvage", 1, 10, source={"fixed": 10}),
    )
    return Craft(**{**craft.__dict__, **overrides})


def test_hand_worked_example() -> None:
    b = evaluate(worked_craft())
    # C = 20 + (4 * 10) + 20; C90 = 20 + (4 * 22) + 20
    assert b.c_base == pytest.approx(20)
    assert b.c == pytest.approx(80)
    assert b.c90 == pytest.approx(128)
    # V = .3*300*.95 + .5*100*.95 + .2*10
    assert b.v == pytest.approx(135)
    assert b.profit == pytest.approx(55)
    assert b.profit_per_hour == pytest.approx(550)
    assert b.roi == pytest.approx(55 / 80)
    assert b.p_fail == pytest.approx(0.2)
    assert b.complete and b.warnings == ()


def test_single_step_counts_once_and_repeat_uses_expected_attempts() -> None:
    b = evaluate(worked_craft())
    repeat, single = b.steps
    assert (repeat.expected_attempts, repeat.n90) == (pytest.approx(10), 22)
    assert (single.expected_attempts, single.n90) == (1, 1)
    assert single.expected_cost == single.cost_90 == pytest.approx(20)
    essence = next(m for m in b.materials if m.name == "Essence")
    assert essence.expected_qty == pytest.approx(10)
    assert essence.expected_chaos == pytest.approx(30)


def test_undercut_is_a_parameter_with_five_percent_default() -> None:
    assert evaluate(worked_craft()).undercut == 0.05
    b = evaluate(worked_craft(), undercut=0.0)
    assert b.v == pytest.approx(0.3 * 300 + 0.5 * 100 + 2)
    hit = b.outcomes[0]
    assert hit.value == 300 and hit.contribution == pytest.approx(90)


def test_sources_and_ages_pass_through_untouched() -> None:
    b = evaluate(worked_craft())
    assert b.base is not None and b.base.source == {"ninja": "base"} and b.base.age_seconds == 60
    assert b.outcomes[0].source == {"trade": "q1"} and b.outcomes[0].age_seconds == 120
    assert b.salvage is not None and b.salvage.source == {"fixed": 10}


def test_outcome_probabilities_must_sum_to_one() -> None:
    bad = (Outcome("hit", 0.3, p25=1, listing_count=1), Outcome("fail", 0.5, is_failure=True))
    with pytest.raises(ValueError, match="sum to 1"):
        evaluate(worked_craft(outcomes=bad))
    near = (Outcome("hit", 0.8, p25=1), Outcome("fail", 0.2 + 5e-7, is_failure=True))
    evaluate(worked_craft(outcomes=near))


def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        evaluate(worked_craft(minutes=0))
    with pytest.raises(ValueError):
        evaluate(worked_craft(), undercut=1.0)
    with pytest.raises(ValueError):
        evaluate(worked_craft(outcomes=()))


def test_unpriced_outcome_is_valued_zero_with_warning() -> None:
    outcomes = (
        Outcome("hit", 0.5),
        Outcome("ok", 0.3, p25=100),
        Outcome("f", 0.2, is_failure=True),
    )
    b = evaluate(worked_craft(outcomes=outcomes))
    assert b.v == pytest.approx(0.3 * 100 * 0.95 + 2)
    assert any("'hit' has no price" in w for w in b.warnings)


def test_unpriced_material_makes_cost_unknown() -> None:
    steps = (Step("spam", (mat("Mystery", 1, None),), p_success=0.5),)
    b = evaluate(worked_craft(steps=steps))
    assert not b.complete
    assert (b.c, b.c90, b.profit, b.profit_per_hour, b.roi) == (None,) * 5
    assert any("Mystery" in w for w in b.warnings)


def test_missing_salvage_warns() -> None:
    b = evaluate(worked_craft(salvage=None))
    assert b.v == pytest.approx(0.3 * 300 * 0.95 + 0.5 * 100 * 0.95)
    assert any("no salvage price" in w for w in b.warnings)


def test_stale_quotes_warn_after_thirty_minutes_only() -> None:
    fresh = evaluate(worked_craft(base=mat("Base", 1, 20, age_seconds=1800)))
    assert fresh.warnings == ()
    old = evaluate(worked_craft(base=mat("Base", 1, 20, age_seconds=3600)))
    assert any(w.startswith("quote age: Base") and "60 min" in w for w in old.warnings)


def test_zero_cost_gives_no_roi() -> None:
    free = Craft("free", "Free", 1, (), (Outcome("hit", 1.0, p25=10),))
    b = evaluate(free)
    assert b.c == 0 and b.roi is None and b.profit == pytest.approx(9.5)


def test_divine_conversion_is_display_only() -> None:
    b = evaluate(worked_craft())
    assert to_divine(b.profit, 200) == pytest.approx(0.275)
    assert to_divine(None, 200) is None
    assert b.profit == pytest.approx(55)
    with pytest.raises(ValueError):
        to_divine(1, 0)


def test_n90_corrects_near_integer_boundaries() -> None:
    assert n90(0.89999999999) == 2  # (1 - p) is just above 0.1, so one attempt covers < 90%
    assert n90(0.89999999999999) == 2
    just_above = 1 - 0.1000000000000001  # 1 - p lands a few float steps above 0.1
    assert 1 - just_above > 0.1 and n90(just_above) == 2
    assert n90(0.9) == 1
    assert n90(0.1) == 22 and n90(0.5) == 4


def test_n90_at_the_supported_floor_is_the_exact_smallest_n() -> None:
    p = 1e-12
    n = n90(p)
    assert n * math.log1p(-p) <= math.log(0.1) < (n - 1) * math.log1p(-p)
    assert n == pytest.approx(math.log(10) / p, rel=1e-9)


@pytest.mark.parametrize("p", [1e-13, 2.0**-64, 1e-30, 5e-324])
def test_probability_below_supported_floor_is_rejected_promptly(p: float) -> None:
    start = time.perf_counter()
    for fn in (n90, expected_attempts):
        with pytest.raises(ValueError, match="below 1e-12"):
            fn(p)
    assert time.perf_counter() - start < 0.5


def test_salvage_applies_quantity() -> None:
    half = evaluate(worked_craft(salvage=mat("Scrap", 0.5, 10)))
    assert half.outcomes[2].value == pytest.approx(5)
    assert half.v == pytest.approx(0.3 * 300 * 0.95 + 0.5 * 100 * 0.95 + 0.2 * 5)


@pytest.mark.parametrize(
    "override",
    [
        {"base": mat("Base", 1, -10)},
        {"base": mat("Base", -1, 10)},
        {"base": mat("Base", 1, float("nan"))},
        {"base": mat("Base", 1, 10, volume=-5)},
        {"salvage": mat("Salvage", 1, -1)},
        {"steps": (Step("s", (mat("Essence", 1, -10),), p_success=0.5),)},
        {"steps": (Step("s", (mat("Essence", float("inf"), 1),)),)},
        {"outcomes": (Outcome("hit", 1.0, p25=-5, listing_count=10),)},
        {"outcomes": (Outcome("hit", float("nan"), p25=5),)},
        {"outcomes": (Outcome("hit", 1.0, p25=float("inf")),)},
        {"outcomes": (Outcome("hit", 1.0, p25=5, listing_count=-1),)},
        {"minutes": float("nan")},
    ],
)
def test_negative_or_non_finite_inputs_raise(override: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        evaluate(worked_craft(**override))


def test_failure_outcomes_can_carry_their_own_value() -> None:
    craft = worked_craft(
        outcomes=(
            Outcome("hit", 0.5, p25=100, listing_count=40),
            Outcome("bricked", 0.25, is_failure=True),
            Outcome("lost", 0.25, is_failure=True, failure_value=0.0),
        ),
        salvage=mat("Salvage", 1, 4),
    )
    b = evaluate(craft)
    assert [o.contribution for o in b.outcomes] == pytest.approx([47.5, 1.0, 0.0])
    assert b.v == pytest.approx(48.5)
    no_salvage = evaluate(worked_craft(outcomes=craft.outcomes, salvage=None))
    assert no_salvage.v == pytest.approx(47.5)
    assert no_salvage.warnings == ("no salvage price; failure outcome 'bricked' valued at 0",)
    with pytest.raises(ValueError, match="failure_value"):
        evaluate(worked_craft(outcomes=(Outcome("x", 1.0, is_failure=True, failure_value=-1),)))
