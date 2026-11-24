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


class _Failing:
    async def aclose(self) -> None:
        raise RuntimeError("boom")


async def test_aclose_closes_everything_even_if_one_fails(config: Config) -> None:
    ctx = AppContext.create(config)
    ctx.service("bad", lambda c: _Failing())
    good = ctx.service("good", lambda c: _Closable())
    http_closed: list[bool] = []
    original = ctx.http.aclose

    async def track() -> None:
        http_closed.append(True)
        await original()

    ctx.http.aclose = track  # type: ignore[method-assign]
    try:
        await ctx.aclose()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected RuntimeError")
    assert good.closed
    assert http_closed == [True]
