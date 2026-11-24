"""Shared state handed to every tool module's `register(server, ctx)`."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

from poe_craft_mcp.config import Config
from poe_craft_mcp.http.cache import Clock, ResponseCache
from poe_craft_mcp.http.fetch import CachedHttpClient

T = TypeVar("T")


@dataclass
class AppContext:
    """Config plus the long-lived cache and HTTP client.

    Feature modules attach their own long-lived clients lazily with `service()`, so they
    never need to edit this class.
    """

    config: Config
    cache: ResponseCache
    http: CachedHttpClient
    services: dict[str, Any] = field(default_factory=dict)

    def service(self, name: str, factory: Callable[[AppContext], T]) -> T:
        """Return the shared service `name`, creating it with `factory(self)` on first use."""
        if name not in self.services:
            self.services[name] = factory(self)
        return self.services[name]

    @classmethod
    def create(
        cls,
        config: Config,
        *,
        clock: Clock = time.time,
        http_client: httpx.AsyncClient | None = None,
    ) -> AppContext:
        cache = ResponseCache(config.cache_path, clock=clock)
        http = CachedHttpClient(cache, config.user_agent, client=http_client)
        return cls(config=config, cache=cache, http=http)

    async def aclose(self) -> None:
        for svc in self.services.values():
            close = getattr(svc, "aclose", None)
            if close is not None:
                await close()
        await self.http.aclose()
