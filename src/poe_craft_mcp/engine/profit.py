"""Cost, value and profit maths for a craft. Chaos throughout; nothing is rounded."""

import math

from poe_craft_mcp.engine.types import (
    Breakdown,
    Craft,
    Material,
    MaterialLine,
    Outcome,
    OutcomeLine,
    Step,
    StepLine,
)

DEFAULT_UNDERCUT = 0.05
STALE_SECONDS = 30 * 60
PROBABILITY_TOLERANCE = 1e-6
_N90_MAX_CORRECTION = 64


def _check_p(p: float) -> None:
    if not 0 < p <= 1:
        raise ValueError(f"success probability must be in (0, 1], got {p}")


def expected_attempts(p: float) -> float:
    """Mean attempts of a geometric distribution: 1/p."""
    _check_p(p)
    return 1 / p


def n90(p: float) -> int:
    """Smallest n >= 1 with (1 - p)**n <= 0.1; 1 when p == 1.

    Works in log space with log1p(-p), which is accurate even when 1 - p would round.
    """
    _check_p(p)
    if p == 1:
        return 1
    log_q, target = math.log1p(-p), math.log(0.1)
    n = max(1, math.ceil(target / log_q))
    # The estimate is off by at most a few steps. For tiny p, n * log_q stops changing between
    # neighbouring n (a float plateau), so cap the correction instead of walking the plateau.
    for _ in range(_N90_MAX_CORRECTION):
        if n * log_q <= target:
            break
        n += 1
    for _ in range(_N90_MAX_CORRECTION):
        if n <= 1 or (n - 1) * log_q > target:
            break
        n -= 1
    return n


def to_divine(chaos: float | None, chaos_per_divine: float) -> float | None:
    """Convert chaos to divines. For display only; never feed the result back into maths."""
    if chaos_per_divine <= 0:
        raise ValueError("chaos_per_divine must be positive")
    return None if chaos is None else chaos / chaos_per_divine


def _stale(label: str, age: float | None) -> list[str]:
    if age is not None and age > STALE_SECONDS:
        return [f"quote age: {label} price is {age / 60:.0f} min old"]
    return []


def _material_line(step: str, m: Material, attempts: float) -> MaterialLine:
    expected = None if m.chaos is None else m.qty * m.chaos * attempts
    return MaterialLine(
        step, m.name, m.qty, m.qty * attempts, m.chaos, expected, m.source, m.age_seconds, m.volume
    )


def _step_lines(step: Step) -> tuple[StepLine, tuple[MaterialLine, ...]]:
    if step.p_success is None:
        attempts, bad_run = 1.0, 1
    else:
        attempts, bad_run = expected_attempts(step.p_success), n90(step.p_success)
    lines = tuple(_material_line(step.name, m, attempts) for m in step.materials)
    known = all(m.chaos is not None for m in step.materials)
    per_attempt = sum(m.qty * m.chaos for m in step.materials if m.chaos is not None)
    return (
        StepLine(
            step.name,
            step.p_success,
            attempts,
            bad_run,
            per_attempt if known else None,
            per_attempt * attempts if known else None,
            per_attempt * bad_run if known else None,
        ),
        lines,
    )


def _outcome_line(o: Outcome, salvage: Material | None, undercut: float) -> OutcomeLine:
    if o.is_failure:
        value = salvage.qty * salvage.chaos if salvage and salvage.chaos is not None else 0.0
    else:
        value = o.p25 * (1 - undercut) if o.p25 is not None else 0.0
    return OutcomeLine(
        o.name,
        o.probability,
        o.p25,
        value,
        o.probability * value,
        o.source,
        o.age_seconds,
        o.listing_count,
        o.is_failure,
    )


def _check_amount(label: str, value: float | None) -> None:
    if value is not None and not (math.isfinite(value) and value >= 0):
        raise ValueError(f"{label} must be finite and non-negative, got {value}")


def _check_material(m: Material) -> None:
    _check_amount(f"qty of '{m.name}'", m.qty)
    _check_amount(f"price of '{m.name}'", m.chaos)
    _check_amount(f"volume of '{m.name}'", m.volume)


def _validate(craft: Craft, undercut: float) -> None:
    if not 0 <= undercut < 1:
        raise ValueError(f"undercut must be in [0, 1), got {undercut}")
    if not (math.isfinite(craft.minutes) and craft.minutes > 0):
        raise ValueError("minutes must be positive and finite")
    for m in (*([craft.base] if craft.base else []), *([craft.salvage] if craft.salvage else [])):
        _check_material(m)
    for step in craft.steps:
        for m in step.materials:
            _check_material(m)
    for o in craft.outcomes:
        _check_amount(f"p25 of '{o.name}'", o.p25)
        _check_amount(f"listing_count of '{o.name}'", o.listing_count)
    if not craft.outcomes:
        raise ValueError("a craft needs at least one outcome")
    if any(not 0 <= o.probability <= 1 for o in craft.outcomes):
        raise ValueError("outcome probabilities must be within [0, 1]")
    total = sum(o.probability for o in craft.outcomes)
    if abs(total - 1) > PROBABILITY_TOLERANCE:
        raise ValueError(f"outcome probabilities must sum to 1 (failure included), got {total}")


def evaluate(craft: Craft, undercut: float = DEFAULT_UNDERCUT) -> Breakdown:
    """Evaluate a craft.

    C = C_base + sum(c_attempt * E[attempts]), C90 uses n90 instead; single steps count once.
    V = sum(p_i * P25_i * (1 - u)) + p_fail * S. Profit = V - C; Profit/hr = Profit / (minutes/60);
    ROI = Profit / C (None when C == 0). Salvage is `qty * chaos` per failure. Negative or
    non-finite prices, quantities, volumes and probabilities raise ValueError.

    An unpriced base or material makes the craft `complete=False`: c, c90, profit, profit/hr and
    ROI are None, because a guessed cost would inflate profit. An unpriced sale outcome or
    salvage is valued at 0 (conservative), with a warning.
    """
    _validate(craft, undercut)
    steps, materials = [], []
    for step in craft.steps:
        step_line, lines = _step_lines(step)
        steps.append(step_line)
        materials.extend(lines)
    base = _material_line("base", craft.base, 1.0) if craft.base else None
    cost_lines = [*([base] if base else []), *materials]

    warnings: list[str] = []
    missing = [m.name for m in cost_lines if m.unit_chaos is None]
    complete = not missing
    if missing:
        warnings.append(f"no price for input(s): {', '.join(missing)}; cost unknown")
    outcomes = tuple(_outcome_line(o, craft.salvage, undercut) for o in craft.outcomes)
    for o in outcomes:
        if o.probability == 0:
            continue
        if o.is_failure and (craft.salvage is None or craft.salvage.chaos is None):
            warnings.append(f"no salvage price; failure outcome '{o.name}' valued at 0")
        elif not o.is_failure and o.p25 is None:
            warnings.append(f"outcome '{o.name}' has no price; valued at 0")
    for line in cost_lines:
        warnings += _stale(line.name, line.age_seconds)
    for o in outcomes:
        warnings += _stale(o.name, o.age_seconds)
    if craft.salvage:
        warnings += _stale(craft.salvage.name, craft.salvage.age_seconds)

    c_base = base.expected_chaos if base else 0.0
    v = sum(o.contribution for o in outcomes)
    c = c90 = profit = per_hour = roi = None
    if complete:
        c = c_base + sum(s.expected_cost for s in steps)
        c90 = c_base + sum(s.cost_90 for s in steps)
        profit = v - c
        per_hour = profit / (craft.minutes / 60)
        roi = profit / c if c else None
    return Breakdown(
        id=craft.id,
        name=craft.name,
        minutes=craft.minutes,
        undercut=undercut,
        complete=complete,
        c_base=c_base if complete else None,
        c=c,
        c90=c90,
        v=v,
        profit=profit,
        profit_per_hour=per_hour,
        roi=roi,
        p_fail=sum(o.probability for o in outcomes if o.is_failure),
        base=base,
        materials=tuple(materials),
        steps=tuple(steps),
        outcomes=outcomes,
        salvage=craft.salvage,
        warnings=tuple(warnings),
    )
