from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from poe_craft_mcp.clients import ninja as ninja_module
from poe_craft_mcp.clients.ninja import NinjaClient, get_ninja
from poe_craft_mcp.clients.ninja_models import EXCHANGE_TYPES, NinjaError, UnknownCategory
from poe_craft_mcp.config import Config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.http.fetch import FetchError

if TYPE_CHECKING:
    from conftest import FakeClock

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ninja"
BASE = "https://poe.ninja/poe1/api/economy"
EXCHANGE = f"{BASE}/exchange/current/overview"
STASH = f"{BASE}/stash/current/item/overview"
INTERVAL = 15 * 60
GARBLED = "<<not json>>"


def load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@dataclass
class Endpoint:
    """One mocked URL: a JSON body with an ETag, or a fixed error status."""

    body: Any
    etag: str
    status: int = 200
    hits: int = 0


class NinjaApi:
    """respx routes serving the recorded fixtures with ETags, 304s and request recording."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.calls: list[httpx.Request] = []
        self.endpoints: dict[str, Endpoint] = {}
        self.add("leagues", f"{BASE}/leagues", load("leagues.json"))
        self.add("Currency", EXCHANGE, load("exchange_currency.json"))
        self.add("Essence", EXCHANGE, load("exchange_essence.json"))
        self.add("Scarab", EXCHANGE, load("exchange_scarab_empty.json"))
        self.add("Ducat", EXCHANGE, None, status=404)
        self.add("BaseType", STASH, load("stash_basetype.json"))

    def add(self, name: str, url: str, body: Any, status: int = 200) -> None:
        endpoint = Endpoint(body, f'W/"{name}-1"', status)
        self.endpoints[name] = endpoint
        params = {} if name == "leagues" else {"type": name}
        self.router.get(url, params=params).mock(side_effect=self._handler(endpoint))

    def _handler(self, endpoint: Endpoint) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            endpoint.hits += 1
            if endpoint.status != 200:
                return httpx.Response(endpoint.status)
            if endpoint.body == GARBLED:
                return httpx.Response(200, content=b"<html>maintenance</html>")
            if request.headers.get("If-None-Match") == endpoint.etag:
                return httpx.Response(304)
            return httpx.Response(200, json=endpoint.body, headers={"ETag": endpoint.etag})

        return handler

    def hits(self, name: str) -> int:
        return self.endpoints[name].hits

    def change(self, name: str, body: Any) -> None:
        endpoint = self.endpoints[name]
        endpoint.body = body
        endpoint.etag = endpoint.etag + "x"

    def garble(self, name: str) -> None:
        self.endpoints[name].body = GARBLED

    def fail(self, name: str, status: int) -> None:
        self.endpoints[name].status = status

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def api() -> Iterator[NinjaApi]:
    with respx.mock(assert_all_called=False) as router:
        yield NinjaApi(router)


@pytest.fixture
async def ctx(config: Config, clock: FakeClock, api: NinjaApi) -> AsyncIterator[AppContext]:
    context = AppContext.create(config, clock=clock)
    yield context
    await context.aclose()


@pytest.fixture
def ninja(ctx: AppContext) -> NinjaClient:
    return get_ninja(ctx)


async def with_league(tmp_path: Path, clock: FakeClock, league: str) -> AppContext:
    return AppContext.create(Config(data_dir=tmp_path / "data", league=league), clock=clock)


def test_get_ninja_is_a_shared_service(ctx: AppContext) -> None:
    assert get_ninja(ctx) is get_ninja(ctx)


async def test_auto_league_is_first_entry(ninja: NinjaClient, api: NinjaApi) -> None:
    league = await ninja.current_league()
    assert (league.id, league.name) == ("Covens", "Covens")
    assert league.age_seconds == 0.0 and not league.stale
    assert api.call_count == 1


async def test_explicit_league_is_used_everywhere(
    tmp_path: Path, clock: FakeClock, api: NinjaApi
) -> None:
    ctx = await with_league(tmp_path, clock, "Hardcore Covens")
    try:
        ninja = get_ninja(ctx)
        league = await ninja.current_league()
        assert (league.id, league.name) == ("Hardcore Covens", "Hardcore Covens")
        await ninja.exchange_overview("Currency")
        assert api.calls[-1].url.params["league"] == "Hardcore Covens"
    finally:
        await ctx.aclose()


async def test_explicit_league_survives_leagues_endpoint_failure(
    tmp_path: Path, clock: FakeClock, api: NinjaApi
) -> None:
    api.fail("leagues", 503)
    ctx = await with_league(tmp_path, clock, "Standard")
    try:
        league = await get_ninja(ctx).current_league()
        assert (league.id, league.name) == ("Standard", "Standard")
    finally:
        await ctx.aclose()


async def test_auto_league_failure_propagates(ninja: NinjaClient, api: NinjaApi) -> None:
    api.fail("leagues", 503)
    with pytest.raises(FetchError):
        await ninja.current_league()


async def test_exchange_rows_are_normalized(ninja: NinjaClient) -> None:
    rows = {r["id"]: r for r in await ninja.exchange_overview("Currency")}
    exalted = rows["exalted"]
    assert exalted["name"] == "Exalted Orb"
    assert exalted["category"] == "Currency"
    assert exalted["details_id"] == "exalted-orb"
    assert exalted["chaos"] == pytest.approx(0.8757)
    assert exalted["divine"] == pytest.approx(0.8757 / 195, rel=1e-3)
    assert exalted["volume_or_listings"] == 89272 and exalted["volume_kind"] == "chaos_volume"
    assert isinstance(exalted["sparkline_7d"], list) and exalted["sparkline_7d"]
    assert rows["chaos"]["chaos"] == 1.0 and rows["divine"]["divine"] == 1.0


async def test_base_type_rows_are_normalized(ninja: NinjaClient) -> None:
    rows = {r["id"]: r for r in await ninja.stash_item_overview("BaseType")}
    shaper = rows["astral-plate-86-shaper"]
    assert (shaper["name"], shaper["ilvl"], shaper["influence"]) == ("Astral Plate", 86, ["Shaper"])
    assert shaper["variant"] == "Shaper" and shaper["category"] == "BaseType"
    assert shaper["chaos"] == 6.0 and shaper["volume_or_listings"] == 86
    assert shaper["volume_kind"] == "listings"
    plain = rows["astral-plate-84"]
    assert plain["influence"] == [] and "variant" not in plain
    dual = next(r for r in rows.values() if r["influence"] == ["Warlord", "Hunter"])
    assert dual["variant"] == "Warlord/Hunter"


async def test_chaos_per_divine_and_currency_rates(ninja: NinjaClient) -> None:
    assert await ninja.chaos_per_divine() == 195.0
    rates = await ninja.currency_rates()
    assert rates["chaos"] == 1.0 and rates["divine"] == 195.0
    assert rates["exalted"] == pytest.approx(0.8757)
    for trade_id in ("alch", "alt", "aug", "chrome", "fusing", "jewellers", "gcp", "scour", "vaal"):
        assert rates[trade_id] > 0, trade_id


async def test_chaos_per_divine_missing_raises(ninja: NinjaClient, api: NinjaApi) -> None:
    body = load("exchange_currency.json")
    body["lines"] = [line for line in body["lines"] if line["id"] != "divine"]
    api.change("Currency", body)
    with pytest.raises(NinjaError):
        await ninja.chaos_per_divine()


async def test_empty_type_returns_empty_and_warns_once(
    ninja: NinjaClient, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="poe_craft_mcp.clients.ninja")
    assert await ninja.exchange_overview("Scarab") == []
    clock.advance(INTERVAL + 1)
    assert await ninja.exchange_overview("Scarab") == []
    assert len([r for r in caplog.records if "Scarab" in r.getMessage()]) == 1


async def test_404_type_returns_empty_warns_once_and_is_not_hammered(
    ninja: NinjaClient, api: NinjaApi, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="poe_craft_mcp.clients.ninja")
    assert await ninja.exchange_overview("Ducat") == []
    assert await ninja.exchange_overview("Ducat") == []
    assert api.hits("Ducat") == 1
    clock.advance(INTERVAL + 1)
    assert await ninja.exchange_overview("Ducat") == []
    assert api.hits("Ducat") == 2
    assert len([r for r in caplog.records if "Ducat" in r.getMessage()]) == 1


async def test_server_error_without_cache_propagates(ninja: NinjaClient, api: NinjaApi) -> None:
    api.fail("Essence", 503)
    with pytest.raises(FetchError):
        await ninja.exchange_overview("Essence")


async def test_unknown_category_lists_valid_types(ninja: NinjaClient) -> None:
    with pytest.raises(UnknownCategory) as info:
        await ninja.dataset("Bananas")
    message = str(info.value)
    assert "Bananas" in message and "Currency" in message and "BaseType" in message
    assert (await ninja.dataset("currency")).type == "Currency"


async def test_overview_methods_reject_the_wrong_endpoint(ninja: NinjaClient) -> None:
    with pytest.raises(NinjaError):
        await ninja.exchange_overview("BaseType")
    with pytest.raises(NinjaError):
        await ninja.stash_item_overview("Currency")
    assert set(EXCHANGE_TYPES) >= {"Currency", "Essence", "Fossil", "Scarab"}


async def test_snapshots_only_on_fresh_fetch(
    ninja: NinjaClient, api: NinjaApi, ctx: AppContext, clock: FakeClock
) -> None:
    league = (await ninja.current_league()).id
    first_fetch = clock()
    await ninja.exchange_overview("Currency")
    snaps = await ctx.cache.get_snapshots(league, "ninja:Currency", "divine")
    assert [(s.chaos, s.volume, s.taken_at) for s in snaps] == [(195.0, 16730737, first_fetch)]

    clock.advance(60)
    await ninja.exchange_overview("Currency")  # from cache
    clock.advance(INTERVAL)
    await ninja.exchange_overview("Currency")  # 304 revalidation
    assert api.hits("Currency") == 2
    assert len(await ctx.cache.get_snapshots(league, "ninja:Currency", "divine")) == 1

    api.change("Currency", load("exchange_currency.json"))
    clock.advance(INTERVAL + 1)
    await ninja.exchange_overview("Currency")  # changed upstream: 200
    assert len(await ctx.cache.get_snapshots(league, "ninja:Currency", "divine")) == 2


async def test_stash_snapshots_skip_thin_listings(ninja: NinjaClient, ctx: AppContext) -> None:
    rows = await ninja.stash_item_overview("BaseType")
    league = (await ninja.current_league()).id
    thick = next(r for r in rows if r["volume_or_listings"] >= 20)
    thin = next(r for r in rows if r["volume_or_listings"] < 20)
    assert await ctx.cache.get_snapshots(league, "ninja:BaseType", thick["id"])
    assert not await ctx.cache.get_snapshots(league, "ninja:BaseType", thin["id"])


async def test_no_request_within_interval_then_etag_revalidation(
    ninja: NinjaClient, api: NinjaApi, clock: FakeClock
) -> None:
    await ninja.chaos_per_divine()
    assert api.call_count == 2  # leagues + Currency
    clock.advance(INTERVAL - 1)
    await ninja.chaos_per_divine()
    assert api.call_count == 2
    clock.advance(2)
    assert await ninja.chaos_per_divine() == 195.0
    assert api.call_count == 4
    assert all(call.headers.get("If-None-Match") for call in api.calls[2:])


async def test_price_by_id_and_name(ninja: NinjaClient, clock: FakeClock) -> None:
    by_id = await ninja.price("Currency", "divine")
    by_name = await ninja.price("currency", "Divine Orb")
    assert by_id is not None and by_name is not None
    assert by_id["chaos"] == by_name["chaos"] == 195.0
    assert by_id["divine"] == 1.0 and by_id["volume_or_listings"] == 16730737
    clock.advance(120)
    quote = await ninja.price("Currency", "divine")
    assert quote is not None
    assert quote["source"] == {
        "type": "Currency",
        "id": "divine",
        "age_seconds": 120.0,
        "stale": False,
        "url": f"{EXCHANGE}?league=Covens&type=Currency",
    }
    assert await ninja.price("Currency", "Nothing Orb") is None


async def test_price_prefers_the_variant_with_most_listings(ninja: NinjaClient) -> None:
    quote = await ninja.price("BaseType", "Astral Plate")
    assert quote is not None and quote["id"] == "astral-plate-85"


async def test_find_base_matching_rules(ninja: NinjaClient) -> None:
    plain = await ninja.find_base("astral plate", ilvl_min=84)
    assert plain is not None and plain["id"] == "astral-plate-84" and plain["chaos"] == 2.0
    assert plain["source"]["type"] == "BaseType" and plain["source"]["age_seconds"] == 0.0

    shaper = await ninja.find_base("Astral Plate", ilvl_min=86, influence="Shaper")
    assert shaper is not None and shaper["id"] == "astral-plate-86-shaper"

    anything = await ninja.find_base("Astral Plate", influence="any")
    assert anything is not None and anything["chaos"] == 1.0
    cheapest_plain = await ninja.find_base("Astral Plate")
    assert cheapest_plain is not None and cheapest_plain["influence"] == []

    assert await ninja.find_base("Astral Plate", ilvl_min=99) is None
    assert await ninja.find_base("Astral Plate", influence="Shaper/Hunter") is None
    assert await ninja.find_base("Nonexistent Base") is None


async def test_every_exchange_row_has_a_divine_value(ninja: NinjaClient) -> None:
    essences = {r["id"]: r for r in await ninja.exchange_overview("Essence")}
    greed = essences["deafening-essence-of-greed"]
    assert greed["chaos"] == 6.94
    assert greed["divine"] == pytest.approx(0.03559, abs=1e-5)  # core.rates.divine = 0.005128
    assert all(r["divine"] is not None for r in essences.values())
    assert all(r["divine"] is not None for r in await ninja.exchange_overview("Currency"))


@pytest.mark.parametrize("status", [403, 400])
async def test_cold_client_error_is_an_error_not_an_empty_market(
    ninja: NinjaClient, api: NinjaApi, clock: FakeClock, status: int
) -> None:
    api.fail("Essence", status)
    for _ in range(2):
        with pytest.raises(FetchError):
            await ninja.exchange_overview("Essence")
    assert api.hits("Essence") == 1  # remembered for one interval, not re-requested
    clock.advance(INTERVAL + 1)
    api.fail("Essence", 200)
    assert len(await ninja.exchange_overview("Essence")) == 6


async def test_invalid_json_cold_is_an_error_not_an_empty_market(
    ninja: NinjaClient, api: NinjaApi
) -> None:
    api.garble("Essence")
    with pytest.raises(FetchError):
        await ninja.exchange_overview("Essence")
    with pytest.raises(FetchError):
        await ninja.exchange_overview("Essence")


async def test_client_error_with_cached_copy_serves_stale_data(
    ninja: NinjaClient, api: NinjaApi, clock: FakeClock
) -> None:
    fresh = await ninja.dataset("Essence")
    assert not fresh.stale
    api.fail("Essence", 403)
    clock.advance(INTERVAL + 1)
    stale = await ninja.dataset("Essence")
    assert stale.stale and stale.rows == fresh.rows and stale.age_seconds == INTERVAL + 1
    assert stale.source()["stale"] is True


async def test_concurrent_cold_callers_share_one_fetch_and_one_snapshot(
    ninja: NinjaClient, api: NinjaApi, ctx: AppContext
) -> None:
    first, second = await asyncio.gather(
        ninja.exchange_overview("Currency"), ninja.exchange_overview("Currency")
    )
    assert first == second and api.hits("Currency") == 1
    snaps = await ctx.cache.get_snapshots("Covens", "ninja:Currency", "divine")
    assert len(snaps) == 1


async def test_quoted_rows_are_snapshotted_once_even_below_the_bulk_cutoff(
    ninja: NinjaClient, ctx: AppContext, clock: FakeClock
) -> None:
    item = "imperial-bow-86-hunter"  # 2c, 11 listings: under the bulk cutoff
    assert not await ctx.cache.get_snapshots("Covens", "ninja:BaseType", item)
    quote = await ninja.find_base("Imperial Bow", ilvl_min=86, influence="Hunter")
    assert quote is not None and quote["id"] == item
    again = await ninja.find_base("Imperial Bow", ilvl_min=86, influence="Hunter")
    assert again is not None
    snaps = await ctx.cache.get_snapshots("Covens", "ninja:BaseType", item)
    assert [s.chaos for s in snaps] == [2.0]
    assert not await ctx.cache.get_snapshots("Covens", "ninja:BaseType", "imperial-bow-83-hunter")

    other = await ninja.price("BaseType", "imperial-bow-84-hunter")
    assert other is not None
    assert len(await ctx.cache.get_snapshots("Covens", "ninja:BaseType", other["id"])) == 1

    clock.advance(INTERVAL + 1)  # 304: same body, nothing new to record
    await ninja.find_base("Imperial Bow", ilvl_min=86, influence="Hunter")
    assert len(await ctx.cache.get_snapshots("Covens", "ninja:BaseType", item)) == 1


async def test_warm_lookups_do_not_renormalize_the_body(
    ninja: NinjaClient, api: NinjaApi, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = ninja_module.normalize_stash

    def counting(payload: Any, type_name: str) -> Any:
        nonlocal calls
        calls += 1
        return original(payload, type_name)

    monkeypatch.setattr(ninja_module, "normalize_stash", counting)
    for _ in range(3):
        assert await ninja.find_base("Astral Plate", ilvl_min=84) is not None
    assert calls == 1
    clock.advance(INTERVAL + 1)  # 304: same body
    assert await ninja.find_base("Astral Plate", ilvl_min=84) is not None
    assert calls == 1
    api.change("BaseType", load("stash_basetype.json"))
    clock.advance(INTERVAL + 1)
    assert await ninja.find_base("Astral Plate", ilvl_min=84) is not None
    assert calls == 2
