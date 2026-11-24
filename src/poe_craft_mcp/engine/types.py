"""Plain dataclasses shared by the pricing, profit and ranking modules.

All prices are in chaos. `source` dicts are opaque to the engine and are passed through untouched.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

Source = dict[str, Any]


@dataclass(frozen=True)
class Listing:
    """One trade listing in its own currency."""

    amount: float
    currency: str
    indexed_at: datetime | None = None


@dataclass(frozen=True)
class PriceSummary:
    """Cleaned listing prices in chaos; prices are None when nothing usable remains."""

    p10: float | None
    p25: float | None
    median: float | None
    listing_count: int
    raw_count: int
    dropped_outliers: int
    dropped_unknown_currency: dict[str, int]
    currency_mix: dict[str, int]
    cheapest_listing_age_seconds: float | None


@dataclass(frozen=True)
class Material:
    """A priced input. `chaos` is the unit price; None means no price is known."""

    name: str
    qty: float
    chaos: float | None
    source: Source = field(default_factory=dict)
    age_seconds: float | None = None
    volume: float | None = None


@dataclass(frozen=True)
class Step:
    """A craft step. `p_success` None means a single step; otherwise it repeats until success."""

    name: str
    materials: tuple[Material, ...] = ()
    p_success: float | None = None


@dataclass(frozen=True)
class Outcome:
    """A result with its probability. Failure outcomes are valued at the craft's salvage."""

    name: str
    probability: float
    p25: float | None = None
    source: Source = field(default_factory=dict)
    age_seconds: float | None = None
    listing_count: int | None = None
    is_failure: bool = False


@dataclass(frozen=True)
class Craft:
    """Everything the engine needs to evaluate one recipe."""

    id: str
    name: str
    minutes: float
    steps: tuple[Step, ...]
    outcomes: tuple[Outcome, ...]
    base: Material | None = None
    salvage: Material | None = None


@dataclass(frozen=True)
class MaterialLine:
    """A material as costed: per-attempt qty, expected qty over all attempts, expected cost."""

    step: str
    name: str
    qty: float
    expected_qty: float
    unit_chaos: float | None
    expected_chaos: float | None
    source: Source
    age_seconds: float | None
    volume: float | None


@dataclass(frozen=True)
class StepLine:
    """A step's attempt statistics and costs."""

    name: str
    p_success: float | None
    expected_attempts: float
    n90: int
    cost_per_attempt: float | None
    expected_cost: float | None
    cost_90: float | None


@dataclass(frozen=True)
class OutcomeLine:
    """An outcome's value per occurrence (after undercut) and its probability-weighted share."""

    name: str
    probability: float
    p25: float | None
    value: float
    contribution: float
    source: Source
    age_seconds: float | None
    listing_count: int | None
    is_failure: bool


@dataclass(frozen=True)
class Breakdown:
    """Full result of evaluating a craft. Money fields are None when `complete` is False."""

    id: str
    name: str
    minutes: float
    undercut: float
    complete: bool
    c_base: float | None
    c: float | None
    c90: float | None
    v: float
    profit: float | None
    profit_per_hour: float | None
    roi: float | None
    p_fail: float
    base: MaterialLine | None
    materials: tuple[MaterialLine, ...]
    steps: tuple[StepLine, ...]
    outcomes: tuple[OutcomeLine, ...]
    salvage: Material | None
    warnings: tuple[str, ...]
