"""Chance that sequential weighted affix draws hit at least one target mod.

Model: the item gets `n` affixes (n drawn from a mod-count distribution). Each affix is drawn
from the mods still allowed — not sharing a mod group with an affix already on the item, and
its side (prefix/suffix) not full — with probability proportional to weight. If no mod is
allowed before `n` affixes are placed, the item keeps fewer. Exact enumeration is used when the
state space is small; otherwise a seeded Monte Carlo.
"""

from __future__ import annotations

import bisect
import math
import random
from collections.abc import Sequence, Set
from dataclasses import dataclass, field

MAX_EXACT_STATES = 50_000
MC_SAMPLES = 20_000
MC_SEED = 1729
Z_95 = 1.959964
_REJECTION_TRIES = 64


@dataclass(frozen=True)
class Candidate:
    """One rollable mod. `groups` must be non-empty (use the mod id for ungrouped mods)."""

    id: str
    is_prefix: bool
    groups: frozenset[str]
    weight: float
    is_target: bool


@dataclass(frozen=True)
class CountModel:
    """Affix-count distribution and per-side caps for one item rarity on one base."""

    name: str
    counts: tuple[tuple[int, float], ...]
    max_prefix: int
    max_suffix: int


RARE = CountModel("rare", ((4, 8 / 12), (5, 3 / 12), (6, 1 / 12)), 3, 3)
MAGIC = CountModel("magic", ((1, 0.5), (2, 0.5)), 1, 1)


@dataclass(frozen=True)
class Start:
    """Item state before random draws: affixes already forced on by an essence or fossil."""

    groups: frozenset[str] = frozenset()
    prefixes: int = 0
    suffixes: int = 0

    @property
    def occupied(self) -> int:
        return self.prefixes + self.suffixes


@dataclass(frozen=True)
class Estimate:
    """`resolved` is False when sampling saw no hit: only `upper_bound_95` is then meaningful."""

    probability: float
    method: str
    samples: int | None = None
    seed: int | None = None
    std_error: float | None = None
    ci95: tuple[float, float] | None = None
    resolved: bool = True
    upper_bound_95: float | None = None
    detail: dict[str, float] = field(default_factory=dict)


def _allowed(c: Candidate, taken: Set[str], pre: int, suf: int, model: CountModel) -> bool:
    if c.groups & taken:
        return False
    return pre < model.max_prefix if c.is_prefix else suf < model.max_suffix


def _state_bound(pool: Sequence[Candidate], model: CountModel, start: Start) -> int:
    groups = {g for c in pool if not c.is_target for g in c.groups}
    depth = max(n for n, _ in model.counts) - start.occupied
    return sum(math.comb(len(groups), k) * (k + 1) for k in range(max(depth, 0)))


def exact(pool: Sequence[Candidate], model: CountModel, start: Start) -> Estimate:
    cands = [c for c in pool if c.weight > 0]
    memo: dict[tuple[frozenset[str], int, int, int], float] = {}

    def hit(taken: frozenset[str], pre: int, suf: int, left: int) -> float:
        if left <= 0:
            return 0.0
        key = (taken, pre, suf, left)
        if key not in memo:
            eligible = [c for c in cands if _allowed(c, taken, pre, suf, model)]
            total = sum(c.weight for c in eligible)
            acc = 0.0
            for c in eligible:
                if c.is_target:
                    acc += c.weight
                else:
                    pre2, suf2 = pre + c.is_prefix, suf + (not c.is_prefix)
                    acc += c.weight * hit(taken | c.groups, pre2, suf2, left - 1)
            memo[key] = acc / total if total > 0 else 0.0
        return memo[key]

    detail = {
        str(n): hit(start.groups, start.prefixes, start.suffixes, n - start.occupied)
        for n, _ in model.counts
    }
    probability = sum(share * detail[str(n)] for n, share in model.counts)
    return Estimate(probability, "exact", detail=detail)


def _wilson(hits: int, n: int) -> tuple[float, float]:
    p = hits / n
    denom = 1 + Z_95**2 / n
    centre = (p + Z_95**2 / (2 * n)) / denom
    half = Z_95 * math.sqrt(p * (1 - p) / n + Z_95**2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def monte_carlo(
    pool: Sequence[Candidate],
    model: CountModel,
    start: Start,
    samples: int = MC_SAMPLES,
    seed: int = MC_SEED,
) -> Estimate:
    cands = [c for c in pool if c.weight > 0]
    cumulative: list[float] = []
    running = 0.0
    for c in cands:
        running += c.weight
        cumulative.append(running)
    rng = random.Random(seed)
    count_values = [n for n, _ in model.counts]
    count_weights = [share for _, share in model.counts]

    def draw(taken: set[str], pre: int, suf: int) -> Candidate | None:
        for _ in range(_REJECTION_TRIES):
            idx = bisect.bisect_right(cumulative, rng.random() * running)
            c = cands[min(idx, len(cands) - 1)]
            if _allowed(c, taken, pre, suf, model):
                return c
        eligible = [c for c in cands if _allowed(c, taken, pre, suf, model)]
        if not eligible:
            return None
        return rng.choices(eligible, weights=[c.weight for c in eligible])[0]

    hits = 0
    for _ in range(samples):
        n = rng.choices(count_values, weights=count_weights)[0]
        taken, pre, suf = set(start.groups), start.prefixes, start.suffixes
        for _ in range(n - start.occupied):
            c = draw(taken, pre, suf) if cands else None
            if c is None:
                break
            if c.is_target:
                hits += 1
                break
            taken |= c.groups
            pre, suf = pre + c.is_prefix, suf + (not c.is_prefix)
    common = {"samples": samples, "seed": seed}
    if hits == 0:
        upper = 1 - 0.05 ** (1 / samples)
        return Estimate(0.0, "monte_carlo", resolved=False, upper_bound_95=upper, **common)
    p = hits / samples
    std_error = math.sqrt(p * (1 - p) / samples)
    return Estimate(p, "monte_carlo", std_error=std_error, ci95=_wilson(hits, samples), **common)


def hit_probability(
    pool: Sequence[Candidate], model: CountModel, start: Start, force_monte_carlo: bool = False
) -> Estimate:
    if not any(c.is_target and c.weight > 0 for c in pool):
        return Estimate(0.0, "exact", detail={str(n): 0.0 for n, _ in model.counts})
    if force_monte_carlo or _state_bound(pool, model, start) > MAX_EXACT_STATES:
        return monte_carlo(pool, model, start)
    return exact(pool, model, start)
