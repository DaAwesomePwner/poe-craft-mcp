"""`Priced`: one recipe input's unit price in chaos, with its source, age and status."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from poe_craft_mcp.engine.types import Source


@dataclass
class Priced:
    """One priced input. `chaos` is a unit price (None when unknown); `status` is one of
    ok, fixed, override, not_found, error."""

    kind: str
    name: str
    chaos: float | None
    source: Source
    status: str = "ok"
    age_seconds: float | None = None
    volume: float | None = None
    listing_count: int | None = None
    error: str | None = None
    sources: list[Source] = field(default_factory=list)
    stale: bool = False
    ilvl: int | None = None

    @property
    def failed(self) -> bool:
        return self.status in ("not_found", "error")

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "chaos": self.chaos,
            "status": self.status,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 1),
            "source": self.source,
        }
        if self.volume is not None:
            out["volume"] = self.volume
        if self.listing_count is not None:
            out["listing_count"] = self.listing_count
        if self.stale:
            out["stale"] = True
        if self.error:
            out["error"] = self.error
        return out
