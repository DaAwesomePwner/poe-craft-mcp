"""Price snapshot bookkeeping: each (dataset body, item) pair is written exactly once."""

from __future__ import annotations

from collections.abc import Iterable

from poe_craft_mcp.clients.ninja_models import SNAPSHOT_MIN_LISTINGS, Dataset, Row
from poe_craft_mcp.http.cache import PriceSnapshot, ResponseCache


def worth_bulk_recording(ds: Dataset, row: Row) -> bool:
    """Exchange rows always; stash rows only when they have enough listings to be stable."""
    return ds.kind == "exchange" or (row["volume_or_listings"] or 0) >= SNAPSHOT_MIN_LISTINGS


class SnapshotLedger:
    """Writes `price_snapshots` rows, skipping items already recorded for the same body.

    A body is identified by `Dataset.version` (the cached response's ETag), so callers that
    share one coalesced fetch, and later lookups of the same body, never write twice.
    """

    def __init__(self, cache: ResponseCache) -> None:
        self._cache = cache
        self._recorded: dict[str, tuple[str | None, set[str]]] = {}

    async def record(self, ds: Dataset, rows: Iterable[Row]) -> int:
        """Snapshot `rows` of `ds` that are not recorded yet; returns how many were written."""
        key = ds.url or ds.type
        version, seen = self._recorded.get(key, (None, set()))
        if key not in self._recorded or version != ds.version:
            seen = set()
            self._recorded[key] = (ds.version, seen)
        fresh = [r for r in rows if r["chaos"] is not None and str(r["id"]) not in seen]
        claimed = {str(r["id"]) for r in fresh}
        seen |= claimed  # claim before awaiting so concurrent callers skip these
        if not fresh:
            return 0
        source = f"ninja:{ds.type}"
        try:
            return await self._cache.add_snapshots(
                PriceSnapshot(ds.league, source, str(r["id"]), r["chaos"], r["volume_or_listings"])
                for r in fresh
            )
        except BaseException:
            seen -= claimed
            raise
