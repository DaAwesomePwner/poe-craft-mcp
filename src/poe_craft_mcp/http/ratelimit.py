"""GGG rate-limit header parser and the single-file queue for trade requests.

Each response names its policy (`X-Rate-Limit-Policy`), its rules (`X-Rate-Limit-Rules`)
and, per rule, `hits:period:restriction` limits plus `hits:period:active_restriction` state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("poe_craft_mcp.ratelimit")

Sleep = Callable[[float], Awaitable[None]]
DEFAULT_KIND_POLICY = {"search": "search", "exchange": "search", "fetch": "fetch"}
BOOTSTRAP_INTERVAL_S = 2.0
MAX_WAIT_S = 20.0
PAUSE_FACTOR = 1.1
DEFAULT_RETRY_AFTER_S = 60.0


class RateLimited(Exception):
    """A trade call would wait more than the limit, or the server answered 429."""

    def __init__(self, retry_after: float, reason: str) -> None:
        super().__init__(f"trade rate limited ({reason}); retry in {retry_after:.0f}s")
        self.retry_after = retry_after
        self.reason = reason


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


def retry_after_seconds(headers: Mapping[str, str]) -> float:
    try:
        return max(0.0, float(headers.get("retry-after", "")))
    except ValueError:
        return DEFAULT_RETRY_AFTER_S


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
        self._pause_epoch = 0
        self._lock = asyncio.Lock()

    def _policy(self, kind: str) -> Policy:
        name = self.kind_policy.setdefault(kind, kind)
        return self.policies.setdefault(name, Policy())

    def wait_for(self, kind: str) -> float:
        now = self.clock()
        return max(self.paused_until - now, self._policy(kind).wait(now, self.bootstrap_s), 0.0)

    async def run(self, kind: str, send: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        """Wait for room under `kind`'s policy, then send. Raises `RateLimited` instead of
        waiting past `max_wait_s` (queue time included), on 429, or when a 429 hit the queue
        while this call was waiting."""
        deadline = self.clock() + self.max_wait_s
        epoch = self._pause_epoch
        async with self._lock:
            if self._pause_epoch != epoch:
                raise RateLimited(self.wait_for(kind), "queue paused by a 429")
            wait = self.wait_for(kind)
            if self.clock() + wait > deadline:
                raise RateLimited(wait, f"{kind} would wait {wait:.1f}s")
            if wait > 0:
                await self.sleep(wait)
            self._policy(kind).record(self.clock())
            response = await send()
            self._observe(kind, response)
            if response.status_code == 429:
                pause = retry_after_seconds(response.headers) * PAUSE_FACTOR
                self.paused_until = max(self.paused_until, self.clock() + pause)
                self._pause_epoch += 1
                log.warning("429 on %s: pausing trade queue for %.1fs", kind, pause)
                raise RateLimited(pause, "HTTP 429")
            return response

    def _observe(self, kind: str, response: httpx.Response) -> None:
        headers = response.headers
        name = headers.get("x-rate-limit-policy")
        if name and self.kind_policy.get(kind) != name:
            self.kind_policy[kind] = name
            self.policies.setdefault(name, Policy())
        if "x-rate-limit-rules" in headers:
            self._policy(kind).update(headers, self.clock())
        log.info(
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
