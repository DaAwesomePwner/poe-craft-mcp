from __future__ import annotations

from poe_craft_mcp.config import Config
from poe_craft_mcp.context import AppContext


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_service_is_created_once_and_closed(config: Config) -> None:
    ctx = AppContext.create(config)
    calls: list[AppContext] = []

    def factory(c: AppContext) -> _Closable:
        calls.append(c)
        return _Closable()

    first = ctx.service("thing", factory)
    second = ctx.service("thing", factory)
    plain = ctx.service("plain", lambda c: object())

    assert first is second
    assert calls == [ctx]
    assert plain is ctx.services["plain"]
    await ctx.aclose()
    assert first.closed
