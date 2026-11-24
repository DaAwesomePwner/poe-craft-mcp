"""Price snapshot bookkeeping: each (dataset body, item) pair is written exactly once."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from poe_craft_mcp.clients.ninja_models import SNAPSHOT_MIN_LISTINGS, Dataset, Row
from poe_craft_mcp.http.cache import CachedResponse, PriceSnapshot, ResponseCache

log = logging.getLogger(__name__)


def body_version(cached: CachedResponse) -> str:
    """Identifies one stored response body: its ETag, else the time it was fetched.

    Without an ETag the server never answers 304, so `fetched_at` only changes with the body.
    """
    return cached.etag or f"t{cached.fetched_at!r}"


def worth_bulk_recording(ds: Dataset, row: Row) -> bool:
    """Exchange rows always; stash rows only when they have enough listings to be stable."""
    return ds.kind == "exchange" or (row["volume_or_listings"] or 0) >= SNAPSHOT_MIN_LISTINGS


class SnapshotLedger:
    """Writes `price_snapshots` rows, skipping items already recorded for the same body.

    Everything runs under one lock: a batch is de-duplicated, filtered against what is
    recorded, inserted, and only then marked recorded. A failed insert is logged and leaves
    the items unmarked, so the next caller retries.
    """

    def __init__(self, cache: ResponseCache) -> None:
        self._cache = cache
        self._lock = asyncio.Lock()
        self._recorded: dict[str, tuple[str | None, set[str]]] = {}

    async def record(self, ds: Dataset, rows: Iterable[Row]) -> int:
        """Snapshot `rows` of `ds` that are not recorded yet; returns how many were written."""
        key = ds.url or ds.type
        async with self._lock:
            version, seen = self._recorded.get(key, (None, set()))
            if key not in self._recorded or version != ds.version:
                seen = set()
                self._recorded[key] = (ds.version, seen)
            batch: dict[str, Row] = {}
            for row in rows:
                item_id = str(row["id"])
                if row["chaos"] is not None and item_id not in seen:
                    batch.setdefault(item_id, row)
            if not batch:
                return 0
            source = f"ninja:{ds.type}"
            snapshots = [
                PriceSnapshot(ds.league, source, item_id, r["chaos"], r["volume_or_listings"])
                for item_id, r in batch.items()
            ]
            try:
                written = await self._cache.add_snapshots(snapshots)
            except Exception:
                log.exception("could not write %d price snapshots for %s", len(batch), source)
                return 0
            seen.update(batch)
            return written
