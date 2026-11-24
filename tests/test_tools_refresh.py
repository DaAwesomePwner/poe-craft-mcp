# ruff: noqa: F811, F401  (fixtures are re-used from test_ninja)
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from test_ninja import INTERVAL, NinjaApi, api, ctx
from test_tools_prices import call, session

from poe_craft_mcp.context import AppContext

if TYPE_CHECKING:
    from conftest import FakeClock

COOLDOWN = 300


async def test_refresh_annotations(ctx: AppContext) -> None:
    async with session(ctx) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    annotations = tools["refresh"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is False


async def test_refresh_revalidates_cached_datasets_with_cooldown(
    ctx: AppContext, api: NinjaApi, clock: FakeClock
) -> None:
    async with session(ctx) as client:
        await call(client, "get_prices", category="Essence")
        await call(client, "get_base_price", base="Astral Plate")
        clock.advance(60)  # well inside the 15-minute floor
        before = api.call_count

        first = await call(client, "refresh")
        assert first["refreshed"] is True and first["league"] == "Covens"
        assert set(first["datasets"]) == {"Currency", "Essence", "BaseType"}
        assert first["errors"] == {}
        forced = api.calls[before:]
        assert len(forced) == 4  # leagues + Currency + Essence + BaseType
        revalidated = [r for r in forced if "Currency" not in str(r.url)]
        assert all(r.headers.get("If-None-Match") for r in revalidated)
        assert all(ds["age_seconds"] == 0.0 for ds in first["datasets"].values())
        assert {s["name"] for s in first["sources"]} >= {"poe.ninja leagues", "poe.ninja Essence"}

        clock.advance(COOLDOWN - 1)
        calls = len(api.router.calls)
        second = await call(client, "refresh")
        assert len(api.router.calls) == calls
        assert second["refreshed"] is False and second["cooldown_remaining_seconds"] == 1.0
        assert "cooldown" in second["message"]

        clock.advance(1)
        third = await call(client, "refresh")
        assert third["refreshed"] is True
        assert len(api.router.calls) == calls + 4
        assert all(r.headers.get("If-None-Match") for r in api.calls[-4:])
    assert "trade" not in ctx.services


async def test_refreshed_data_resets_the_regular_interval(
    ctx: AppContext, api: NinjaApi, clock: FakeClock
) -> None:
    async with session(ctx) as client:
        await call(client, "get_prices", category="Essence")
        clock.advance(INTERVAL - 10)
        await call(client, "refresh")
        hits = api.hits("Essence")
        clock.advance(20)  # past the original fetch's interval, not the refresh's
        await call(client, "get_prices", category="Essence")
        assert api.hits("Essence") == hits


async def test_refresh_reports_per_dataset_errors(ctx: AppContext, api: NinjaApi) -> None:
    api.fail("Currency", 503)
    async with session(ctx) as client:
        data = await call(client, "refresh")
    assert data["refreshed"] is True and "Currency" not in data["datasets"]
    assert "HTTP 503" in data["errors"]["Currency"]


def essence(data: dict[str, Any]) -> dict[str, Any]:
    return next(src for src in data["sources"] if src["name"] == "poe.ninja Essence")


async def test_forced_404_hides_the_removed_type_from_ordinary_reads(
    ctx: AppContext, api: NinjaApi, clock: FakeClock
) -> None:
    async with session(ctx) as client:
        await call(client, "get_prices", category="Essence")
        clock.advance(60)
        api.fail("Essence", 404)
        refreshed = await call(client, "refresh")
        assert refreshed["datasets"]["Essence"]["age_seconds"] is None
        hits = api.hits("Essence")
        data = await call(client, "get_prices", category="Essence")
    assert data["rows"] == [] and "no rows" in data["hint"]
    assert api.hits("Essence") == hits


async def test_forced_503_keeps_the_cached_copy_flagged_stale(
    ctx: AppContext, api: NinjaApi, clock: FakeClock
) -> None:
    async with session(ctx) as client:
        await call(client, "get_prices", category="Essence")
        clock.advance(60)
        api.fail("Essence", 503)
        refreshed = await call(client, "refresh")
        assert refreshed["datasets"]["Essence"]["stale"] is True
        hits = api.hits("Essence")
        data = await call(client, "get_prices", category="Essence")
    assert data["total_rows"] == 6 and essence(data)["stale"] is True
    assert api.hits("Essence") == hits


async def test_forced_failure_backs_off_for_the_full_interval(
    ctx: AppContext, api: NinjaApi, clock: FakeClock
) -> None:
    async with session(ctx) as client:
        await call(client, "get_prices", category="Essence")
        clock.advance(INTERVAL + 10)  # expired, not yet re-read
        api.fail("Essence", 503)
        await call(client, "refresh")
        hits = api.hits("Essence")
        clock.advance(61)  # past CachedHttpClient's 60 s minimum backoff
        early = await call(client, "get_prices", category="Essence")
        assert api.hits("Essence") == hits and essence(early)["stale"] is True
        clock.advance(INTERVAL)
        await call(client, "get_prices", category="Essence")
        assert api.hits("Essence") == hits + 1
