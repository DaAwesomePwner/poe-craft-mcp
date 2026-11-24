"""SQLite store for raw HTTP responses (with ETags) and price history snapshots.

All public methods are async; the blocking sqlite3 work runs in `asyncio.to_thread`,
one short-lived connection per call (never shared between threads). Schema setup runs
once, under a lock, and every connection waits up to `BUSY_TIMEOUT_S` for a locked DB.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Clock = Callable[[], float]
"""Returns the current time as epoch seconds; injectable for tests."""

BUSY_TIMEOUT_S = 10.0
_SQL_BATCH = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    etag TEXT,
    body BLOB NOT NULL,
    status INTEGER NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS price_snapshots (
    league TEXT NOT NULL,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    chaos REAL NOT NULL,
    volume REAL,
    taken_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_price_snapshots_lookup
    ON price_snapshots (league, source, item_id, taken_at);
"""


@dataclass(frozen=True)
class CachedResponse:
    """A stored response. `age_seconds` is measured when it was read from the cache."""

    url: str
    body: bytes
    status: int
    etag: str | None
    fetched_at: float
    age_seconds: float

    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass(frozen=True)
class PriceSnapshot:
    """One price observation. `taken_at=None` means "now" when written."""

    league: str
    source: str
    item_id: str
    chaos: float
    volume: float | None = None
    taken_at: float | None = None


class ResponseCache:
    """Async facade over `<data_dir>/cache.db`."""

    def __init__(self, path: Path, clock: Clock = time.time) -> None:
        self.path = path
        self.clock = clock
        self._ready = False
        self._init_lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        # Each connection is created, used and closed in a single worker thread.
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        return conn

    def _initialize(self) -> None:
        """Create the folder, enable WAL and create tables exactly once per instance."""
        with self._init_lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._open()) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.commit()
            self._ready = True

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self._initialize()
        return self._open()

    def _run(self, sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        with closing(self._connect()) as conn, conn:
            return conn.execute(sql, tuple(params)).fetchall()

    async def get(self, url: str) -> CachedResponse | None:
        rows = await asyncio.to_thread(
            self._run, "SELECT etag, body, status, fetched_at FROM http_cache WHERE url = ?", (url,)
        )
        if not rows:
            return None
        etag, body, status, fetched_at = rows[0]
        age = max(0.0, self.clock() - fetched_at)
        return CachedResponse(url, bytes(body), status, etag, fetched_at, age)

    async def fetched_at(self, urls: Iterable[str]) -> dict[str, float]:
        """`fetched_at` of each cached URL among `urls`, without loading any body."""
        wanted = list(dict.fromkeys(urls))
        found: dict[str, float] = {}
        for start in range(0, len(wanted), _SQL_BATCH):
            chunk = wanted[start : start + _SQL_BATCH]
            marks = ",".join("?" * len(chunk))
            rows = await asyncio.to_thread(
                self._run, f"SELECT url, fetched_at FROM http_cache WHERE url IN ({marks})", chunk
            )
            found.update((url, float(at)) for url, at in rows)
        return found

    async def put(
        self, url: str, body: bytes | str, status: int = 200, etag: str | None = None
    ) -> None:
        """Store (or replace) the response for `url`, stamped with the current time."""
        data = body.encode("utf-8") if isinstance(body, str) else body
        await asyncio.to_thread(
            self._run,
            "INSERT OR REPLACE INTO http_cache (url, etag, body, status, fetched_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (url, etag, data, status, self.clock()),
        )

    async def touch(self, url: str) -> bool:
        """Mark a cached response as fresh again (after a 304). Returns False if absent."""

        def _touch() -> bool:
            with closing(self._connect()) as conn, conn:
                cur = conn.execute(
                    "UPDATE http_cache SET fetched_at = ? WHERE url = ?", (self.clock(), url)
                )
                return cur.rowcount > 0

        return await asyncio.to_thread(_touch)

    async def add_snapshots(self, rows: Iterable[PriceSnapshot]) -> int:
        """Append price observations; returns how many were written."""
        now = self.clock()
        values = [
            (
                r.league,
                r.source,
                r.item_id,
                r.chaos,
                r.volume,
                now if r.taken_at is None else r.taken_at,
            )
            for r in rows
        ]

        def _insert() -> int:
            with closing(self._connect()) as conn, conn:
                conn.executemany("INSERT INTO price_snapshots VALUES (?, ?, ?, ?, ?, ?)", values)
            return len(values)

        return await asyncio.to_thread(_insert)

    async def get_snapshots(
        self, league: str, source: str, item_id: str, since: float | None = None
    ) -> list[PriceSnapshot]:
        """Observations for one item, oldest first, optionally only those at or after `since`."""
        rows = await asyncio.to_thread(
            self._run,
            "SELECT league, source, item_id, chaos, volume, taken_at FROM price_snapshots"
            " WHERE league = ? AND source = ? AND item_id = ? AND taken_at >= ?"
            " ORDER BY taken_at",
            (league, source, item_id, float("-inf") if since is None else since),
        )
        return [PriceSnapshot(*row) for row in rows]
