"""GGG rate-limit header parser and the single-file queue for trade requests.

Each response names its policy (`X-Rate-Limit-Policy`), its rules (`X-Rate-Limit-Rules`)
and, per rule, `hits:period:restriction` limits plus `hits:period:active_restriction` state.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from poe_craft_mcp.log import add_file_log

log = logging.getLogger(__name__)
state_log = logging.getLogger("poe_craft_mcp.ratelimit")

Sleep = Callable[[float], Awaitable[None]]
DEFAULT_KIND_POLICY = {"search": "search", "exchange": "search", "fetch": "fetch"}
BOOTSTRAP_INTERVAL_S = 2.0
MAX_WAIT_S = 20.0
PAUSE_FACTOR = 1.1
DEFAULT_RETRY_AFTER_S = 60.0
_SECONDS_RE = re.compile(r"\d+(\.\d+)?", re.ASCII)


class RateLimited(Exception):
    """A trade call would wait more than the limit, or the server answered 429.

    `cause` is "deadline" when the caller's deadline refused a wait before anything was sent,
    "cancelled" when it cut off a request already in flight (`retry_after` is then the wait
    for the next request, not the reason for the failure), "server" for a 429 or a queue
    pause caused by one.
    """

    def __init__(self, retry_after: float, reason: str, cause: str = "server") -> None:
        super().__init__(f"trade rate limited ({reason}); retry in {retry_after:.0f}s")
        self.retry_after = retry_after
        self.reason = reason
        self.cause = cause


def parse_triples(value: str) -> list[tuple[int, int, int]]:
    """`"5:10:60,15:60:300"` -> `[(5, 10, 60), (15, 60, 300)]`; malformed parts are skipped."""
    triples = []
    for part in value.split(","):
        pieces = part.strip().split(":")
        if len(pieces) == 3 and all(p.strip().isdigit() for p in pieces):
            a, b, c = (int(p) for p in pieces)
            triples.append((a, b, c))
    return triples


@dataclass
class Rule:
    """One `max_hits` per `period` window; `hits` holds the send times still inside it."""

    max_hits: int
    period: int
    restriction: int
    hits: deque[float] = field(default_factory=deque)

    def prune(self, now: float) -> None:
        while self.hits and self.hits[0] <= now - self.period:
            self.hits.popleft()

    def count(self, now: float) -> int:
        self.prune(now)
        return len(self.hits)

    def wait(self, now: float) -> float:
        """Seconds until a hit fits while still leaving one hit spare."""
        self.prune(now)
        allowed = max(1, self.max_hits - 1)
        excess = len(self.hits) - allowed
        return 0.0 if excess < 0 else self.hits[excess] + self.period - now


@dataclass
class Policy:
    rules: dict[str, list[Rule]] = field(default_factory=dict)
    last_sent: float | None = None
    restricted_until: float = 0.0

    def wait(self, now: float, bootstrap_s: float) -> float:
        waits = [self.restricted_until - now]
        if not self.rules and self.last_sent is not None:
            waits.append(self.last_sent + bootstrap_s - now)
        waits += [r.wait(now) for rules in self.rules.values() for r in rules]
        return max(0.0, *waits)

    def record(self, now: float) -> None:
        self.last_sent = now
        for rules in self.rules.values():
            for rule in rules:
                rule.hits.append(now)

    def update(self, headers: Mapping[str, str], now: float) -> None:
        names = [n.strip() for n in headers.get("x-rate-limit-rules", "").split(",") if n.strip()]
        for name in names:
            limits = parse_triples(headers.get(f"x-rate-limit-{name}", ""))
            old = {r.period: r for r in self.rules.get(name, [])}
            rules = [Rule(m, p, r, old[p].hits if p in old else deque()) for m, p, r in limits]
            self.rules[name] = rules
            by_period = {r.period: r for r in rules}
            for hits, period, active in parse_triples(
                headers.get(f"x-rate-limit-{name}-state", "")
            ):
                rule = by_period.get(period)
                if rule is not None:
                    rule.prune(now)
                    rule.hits.extend([now] * max(0, hits - len(rule.hits)))
                if active > 0:
                    self.restricted_until = max(self.restricted_until, now + active)

    def snapshot(self, now: float) -> dict[str, Any]:
        rules = {
            name: [
                {
                    "max_hits": r.max_hits,
                    "period": r.period,
                    "restriction": r.restriction,
                    "hits": r.count(now),
                }
                for r in rules
            ]
            for name, rules in self.rules.items()
        }
        return {"rules": rules, "restricted_until": self.restricted_until or None}


def retry_after_seconds(headers: Mapping[str, str], now: float) -> float:
    """Seconds from `Retry-After` (delta seconds or HTTP-date, never negative). Missing,
    malformed or non-finite values give `DEFAULT_RETRY_AFTER_S`."""
    raw = headers.get("retry-after", "").strip()
    seconds: float | None = None
    if _SECONDS_RE.fullmatch(raw):
        seconds = float(raw)
    elif raw:
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - now
        except (TypeError, ValueError, IndexError, OverflowError):
            seconds = None
    if seconds is None or not math.isfinite(seconds):
        log.warning("invalid or missing Retry-After %r; using %.0fs", raw, DEFAULT_RETRY_AFTER_S)
        return DEFAULT_RETRY_AFTER_S
    return max(0.0, seconds)


def setup_state_log(path: Path) -> logging.Logger:
    """Write every limiter state update to `path` at INFO whatever the main log level,
    without echoing those lines to stderr or server.log."""
    logger = add_file_log(state_log.name, path)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


class RateLimiter:
    """One trade request in flight at a time, paced by the GGG headers of earlier responses.

    `clock` and `sleep` are injectable so tests can run on fake time.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
        max_wait_s: float = MAX_WAIT_S,
        bootstrap_s: float = BOOTSTRAP_INTERVAL_S,
    ) -> None:
        self.clock = clock
        self.sleep = sleep
        self.max_wait_s = max_wait_s
        self.bootstrap_s = bootstrap_s
        self.kind_policy = dict(DEFAULT_KIND_POLICY)
        self.policies: dict[str, Policy] = {}
        self.paused_until = 0.0
        self._learned: set[str] = set()
        self._pause_epoch = 0
        self._lock = asyncio.Lock()

    def _policy(self, kind: str) -> Policy:
        name = self.kind_policy.setdefault(kind, kind)
        return self.policies.setdefault(name, Policy())

    def deadline(self) -> float:
        """An absolute deadline `max_wait_s` from now, to share across one operation's calls."""
        return self.clock() + self.max_wait_s

    def _policies_for(self, kind: str) -> list[Policy]:
        """Policies whose state gates `kind`: its own once learned; before that, its default
        bucket (bootstrap pacing) plus the learned policies of its design-default siblings."""
        own = self._policy(kind)
        if kind in self._learned:
            return [own]
        group = DEFAULT_KIND_POLICY.get(kind, kind)
        siblings = [
            self._policy(k)
            for k in sorted(self._learned)
            if k != kind and DEFAULT_KIND_POLICY.get(k, k) == group
        ]
        return [own, *(p for p in siblings if p is not own)]

    def wait_for(self, kind: str) -> float:
        now = self.clock()
        waits = [p.wait(now, self.bootstrap_s) for p in self._policies_for(kind)]
        return max(0.0, self.paused_until - now, *waits)

    async def run(
        self,
        kind: str,
        send: Callable[[], Awaitable[httpx.Response]],
        *,
        deadline: float | None = None,
    ) -> httpx.Response:
        """Wait for room under `kind`'s policy, then send.

        Raises `RateLimited` instead of waiting (for the queue, for the limits or for the
        response) past `deadline` (default `max_wait_s` from now), on 429, or when a 429 hit
        the queue while this call was waiting.
        """
        deadline = self.deadline() if deadline is None else deadline
        epoch = self._pause_epoch
        try:
            await asyncio.wait_for(self._lock.acquire(), max(0.0, deadline - self.clock()))
        except TimeoutError:
            raise RateLimited(
                self.wait_for(kind), "trade queue busy past deadline", "deadline"
            ) from None
        try:
            if self._pause_epoch != epoch:
                raise RateLimited(self.wait_for(kind), "queue paused by a 429")
            wait = self.wait_for(kind)
            if self.clock() + wait > deadline:
                raise RateLimited(wait, f"{kind} would wait {wait:.1f}s", "deadline")
            if wait > 0:
                await self.sleep(wait)
            if deadline - self.clock() <= 0:
                raise RateLimited(
                    self.wait_for(kind), f"{kind} deadline reached before sending", "deadline"
                )
            sent_at = self.clock()
            for policy in self._policies_for(kind):
                policy.record(sent_at)
            try:
                response = await asyncio.wait_for(send(), max(0.0, deadline - self.clock()))
            except TimeoutError:
                # The hit stays recorded: the request may have reached the server.
                raise RateLimited(
                    self.wait_for(kind), f"{kind} request ran past deadline", "cancelled"
                ) from None
            self._observe(kind, response)
            if response.status_code == 429:
                now = self.clock()
                pause = retry_after_seconds(response.headers, now) * PAUSE_FACTOR
                self.paused_until = max(self.paused_until, now + pause)
                self._pause_epoch += 1
                log.warning("429 on %s: pausing trade queue for %.1fs", kind, pause)
                raise RateLimited(pause, "HTTP 429")
            return response
        finally:
            self._lock.release()

    def _observe(self, kind: str, response: httpx.Response) -> None:
        headers = response.headers
        name = headers.get("x-rate-limit-policy")
        if name:
            self._learned.add(kind)
            if self.kind_policy.get(kind) != name:
                self.kind_policy[kind] = name
                self.policies.setdefault(name, Policy())
        if "x-rate-limit-rules" in headers:
            self._policy(kind).update(headers, self.clock())
        state_log.info(
            "%s %s policy=%s rules=%s %s",
            kind,
            response.status_code,
            name,
            headers.get("x-rate-limit-rules"),
            {
                k: v
                for k, v in headers.items()
                if k.startswith("x-rate-limit-") or k == "retry-after"
            },
        )

    def state(self) -> dict[str, Any]:
        """JSON-able snapshot for `get_league_status`."""
        now = self.clock()
        return {
            "paused_until": self.paused_until if self.paused_until > now else None,
            "kinds": dict(self.kind_policy),
            "policies": {name: p.snapshot(now) for name, p in self.policies.items()},
        }
