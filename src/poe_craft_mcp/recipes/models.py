"""Pydantic models for recipe files (the JSON Schema in `recipe.schema.json` mirrors these)."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MIN_VERIFIED_PATCH = (3, 30, 0)
PROBABILITY_TOLERANCE = 1e-6

Version = tuple[int, int, int]
Probability = Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1)]
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)(?:\.([0-9]+))?[a-z]?")


def parse_version(text: str) -> Version:
    """Parse `3.30`, `3.30.0` or `3.30.1b` into a comparable tuple (so 3.9 < 3.30).

    One optional letter at the very end (`3.30.1b`) is accepted and ignored in comparisons.
    """
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a patch version like 3.30, 3.30.0 or 3.30.1b: {text!r}")
    return int(match[1]), int(match[2]), int(match[3] or 0)


def _exactly_one(label: str, *flags: bool) -> None:
    if sum(flags) != 1:
        raise ValueError(f"{label}: exactly one of the alternatives must be set")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NinjaMaterial(_Model):
    source: Literal["ninja"]
    category: str = Field(min_length=1)
    name: str | None = Field(None, min_length=1)
    id: str | None = Field(None, min_length=1)
    qty: Positive

    @model_validator(mode="after")
    def _name_xor_id(self) -> Self:
        _exactly_one("material needs `name` or `id`", self.name is not None, self.id is not None)
        return self


class FixedMaterial(_Model):
    source: Literal["fixed"]
    chaos: NonNegative
    label: str = Field(min_length=1)


Material = Annotated[NinjaMaterial | FixedMaterial, Field(discriminator="source")]


class WeightsChance(_Model):
    p_source: Literal["weights"]
    method: str = Field(min_length=1)
    target_mods: list[Text] = Field(min_length=1)
    base: str | None = None
    ilvl: int | None = Field(None, ge=1, le=100)


class GuideChance(_Model):
    p_source: Literal["guide"]
    value: Probability
    source_url: str | None = None


class UserChance(_Model):
    p_source: Literal["user"]
    value: Probability


Chance = Annotated[WeightsChance | GuideChance | UserChance, Field(discriminator="p_source")]


class Step(_Model):
    action: str = Field(min_length=1)
    materials: list[Material] = Field(default_factory=list)
    repeat_until: str | None = Field(None, min_length=1)
    p_success: Chance | None = None

    @model_validator(mode="after")
    def _repeat_needs_chance(self) -> Self:
        if self.repeat_until is not None and self.p_success is None:
            raise ValueError("a step with `repeat_until` needs `p_success`")
        return self


class NinjaLookup(_Model):
    source: Literal["ninja"]
    type: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ilvl_min: int | None = Field(None, ge=1, le=100)
    influence: str | None = Field(None, min_length=1)


class Base(_Model):
    """Starting item: a poe.ninja BaseType lookup or a saved trade search."""

    lookup: NinjaLookup | None = None
    trade_url: str | None = Field(None, min_length=1)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        _exactly_one("base", self.lookup is not None, self.trade_url is not None)
        if self.lookup is not None and self.lookup.type != "BaseType":
            raise ValueError("base.lookup.type must be BaseType")
        return self


class Outcome(_Model):
    """One end state of a full attempt. Failure is an ordinary outcome priced by
    `fixed_chaos` (0 for a total loss) or `use_salvage: true` (the recipe's `salvage` value)."""

    name: str = Field(min_length=1)
    probability: Probability
    trade_url: str | None = Field(None, min_length=1)
    fixed_chaos: NonNegative | None = None
    use_salvage: bool = False

    @model_validator(mode="after")
    def _one_value(self) -> Self:
        _exactly_one(
            f"outcome {self.name!r}",
            self.trade_url is not None,
            self.fixed_chaos is not None,
            self.use_salvage,
        )
        return self


class Salvage(_Model):
    """What a failed attempt is worth."""

    trade_url: str | None = Field(None, min_length=1)
    lookup: NinjaLookup | None = None
    fixed_chaos: NonNegative | None = None

    @model_validator(mode="after")
    def _one_value(self) -> Self:
        _exactly_one(
            "salvage",
            self.trade_url is not None,
            self.lookup is not None,
            self.fixed_chaos is not None,
        )
        return self


class Recipe(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    tags: list[Text] = Field(default_factory=list)
    status: Literal["verified", "unverified", "deprecated"]
    verified_patch: str | None = None
    sources: list[Text] = Field(default_factory=list)
    requires: list[Text] = Field(default_factory=list)
    budget_div: NonNegative | None = None
    base: Base
    steps: list[Step] = Field(min_length=1)
    outcomes: list[Outcome] = Field(min_length=1)
    salvage: Salvage
    minutes: Positive
    notes: str = ""

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        if self.verified_patch is not None:
            parse_version(self.verified_patch)
        elif self.status == "verified":
            raise ValueError("status `verified` needs `verified_patch`")
        total = sum(outcome.probability for outcome in self.outcomes)
        if abs(total - 1.0) > PROBABILITY_TOLERANCE:
            raise ValueError(f"outcome probabilities must sum to 1, got {total:.9g}")
        return self

    @property
    def is_verified(self) -> bool:
        """True only for status `verified` with `verified_patch` at or after 3.30."""
        return (
            self.status == "verified"
            and self.verified_patch is not None
            and parse_version(self.verified_patch) >= MIN_VERIFIED_PATCH
        )

    def to_document(self) -> dict[str, Any]:
        """JSON-ready dict with only supplied fields, so it validates against the schema
        again when saved as a recipe file."""
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=True)

    def trade_urls(self) -> list[str]:
        urls = [self.base.trade_url, self.salvage.trade_url]
        urls += [outcome.trade_url for outcome in self.outcomes]
        return [url for url in urls if url]

    def warnings(self) -> list[str]:
        """Caveats that must accompany this recipe whenever it is shown."""
        out: list[str] = []
        if self.status == "deprecated":
            out.append("Deprecated: this craft no longer works; do not recommend it.")
        elif self.status == "verified" and not self.is_verified:
            out.append(
                f"verified_patch {self.verified_patch} is older than 3.30; treat as unverified."
            )
        elif self.status == "unverified":
            out.append("Unverified: steps, odds and prices are not confirmed for patch 3.30.")
        if any("<" in url for url in self.trade_urls()):
            out.append("Trade URLs are placeholders; outcome prices cannot be looked up yet.")
        return out
