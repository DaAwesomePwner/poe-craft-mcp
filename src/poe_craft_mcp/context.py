"""Shared state handed to every tool module's `register(server, ctx)`."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from poe_craft_mcp.config import Config
from poe_craft_mcp.http.cache import Clock, ResponseCache
from poe_craft_mcp.http.fetch import CachedHttpClient


@dataclass
class AppContext:
    """Config plus the long-lived cache and HTTP client. Add new shared services here."""

    config: Config
    cache: ResponseCache
    http: CachedHttpClient

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
        await self.http.aclose()
