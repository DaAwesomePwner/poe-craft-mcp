"""Manual live smoke test: a few real requests to poe.ninja and (optionally) the trade site.

Never run by CI or the test suite. It needs the explicit `--live` flag.

Usage:
    uv run python scripts/smoke_live.py --live
    uv run python scripts/smoke_live.py --live --trade-url "https://www.pathofexile.com/trade/search/<league>/<id>"
    uv run python scripts/smoke_live.py --live --config path/to/config.toml

Makes 2 to 3 poe.ninja requests (league list, Currency) and, with --trade-url, one trade search
plus one listing fetch (resolving a saved URL costs one more). Results land in the normal
cache (<data_dir>/cache.db), so a repeat run inside the cache window makes no requests.
Prints a compact summary to stdout and exits 1 on any failure. Prints whether POESESSID is set,
never its value.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from poe_craft_mcp import secrets
from poe_craft_mcp.clients.ninja import get_ninja
from poe_craft_mcp.clients.ninja_models import NinjaError, divine_chaos
from poe_craft_mcp.config import Config, ConfigError, load_config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.http.fetch import FetchError
from poe_craft_mcp.services.trade_pricing import PricingUnavailable, price_outcome


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.1f}"


async def check_ninja(ctx: AppContext) -> bool:
    ninja = get_ninja(ctx)
    try:
        league = await ninja.current_league()
        currency = await ninja.dataset("Currency", league)
        per_divine = divine_chaos(currency.rows)
    except (NinjaError, FetchError) as exc:
        print(f"FAIL poe.ninja: {exc}")
        return False
    stale = " (STALE)" if currency.stale else ""
    age = "just fetched" if currency.age_seconds is None else f"{currency.age_seconds:.0f}s old"
    print(f"ok   league: {league.id} ({league.name})")
    print(f"ok   Currency: {len(currency.rows)} rows, {age}{stale}")
    print(f"ok   1 divine = {fmt(per_divine)} chaos")
    return True


async def check_trade(ctx: AppContext, url: str) -> bool:
    try:
        price = await price_outcome(ctx, trade_url=url)
    except ValueError as exc:
        print(f"FAIL trade URL: {exc}")
        return False
    except PricingUnavailable as exc:
        wait = f" (retry in about {exc.retry_after:.0f}s)" if exc.retry_after else ""
        print(f"FAIL trade: {exc.reason}{wait}")
        return False
    stale = " (STALE: served from cache)" if price.stale else ""
    print(f"ok   trade search {price.search_id} in {price.league}: {price.total} total{stale}")
    print(
        f"ok   chaos p10/p25/median: {fmt(price.p10)} / {fmt(price.p25)} / {fmt(price.median)} "
        f"from {price.listing_count} listings (raw {price.raw_count})"
    )
    return price.listing_count > 0


async def run(config: Config, trade_url: str | None) -> bool:
    print(f"User-Agent: {config.user_agent}")
    print(f"data_dir:   {config.data_dir}")
    print(f"POESESSID:  {'set' if secrets.poesessid_set() else 'not set'}")
    ctx = AppContext.create(config)
    try:
        ok = await check_ninja(ctx)
        if ok and trade_url:
            ok = await check_trade(ctx, trade_url)
        elif trade_url:
            print("skip trade check: poe.ninja failed, so there is no league or rate to use")
        return ok
    finally:
        await ctx.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="required: confirm real requests")
    parser.add_argument("--trade-url", help="a saved pathofexile.com/trade search URL to price")
    parser.add_argument("--config", type=Path, default=None, help="config.toml to read")
    args = parser.parse_args()
    if not args.live:
        print(
            "smoke_live.py makes real requests to poe.ninja and pathofexile.com.\n"
            "Re-run with --live to confirm. It is for manual checks only, never CI.",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"smoke_live: {exc}", file=sys.stderr)
        return 1
    ok = asyncio.run(run(config, args.trade_url))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
