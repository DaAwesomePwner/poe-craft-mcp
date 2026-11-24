"""Shared response envelope: every tool result carries `data_age_seconds` and `sources`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from poe_craft_mcp.http.fetch import FetchResult

RESERVED_KEYS = ("data_age_seconds", "sources")


def source(
    name: str,
    *,
    url: str | None = None,
    age_seconds: float | None = None,
    stale: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """Describe one input, e.g. `source("poe.ninja Currency", url=..., age_seconds=42)`."""
    entry: dict[str, Any] = {"name": name, "url": url, "age_seconds": _round(age_seconds)}
    entry["stale"] = stale
    entry.update(extra)
    return entry


def source_from_fetch(name: str, result: FetchResult, **extra: Any) -> dict[str, Any]:
    """Build a source entry from a `CachedHttpClient.get_json` result."""
    return source(
        name, url=result.source_url, age_seconds=result.age_seconds, stale=result.stale, **extra
    )


def envelope(
    payload: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]] = (),
    data_age_seconds: float | None = None,
) -> dict[str, Any]:
    """Return `payload` plus `sources` and `data_age_seconds`.

    `data_age_seconds` defaults to the oldest source age (None when no source has an age).
    """
    clash = [key for key in RESERVED_KEYS if key in payload]
    if clash:
        raise ValueError(f"payload must not contain envelope keys: {clash}")
    if data_age_seconds is None:
        ages = [s["age_seconds"] for s in sources if s.get("age_seconds") is not None]
        data_age_seconds = max(ages) if ages else None
    return {
        **payload,
        "data_age_seconds": _round(data_age_seconds),
        "sources": [dict(s) for s in sources],
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
