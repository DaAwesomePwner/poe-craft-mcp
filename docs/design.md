# PoE Craft Profit MCP Server — Design Doc

Nov 23, 2026 · @dap

## Summary

Build a local MCP server, `poe-craft-mcp`, that gives Claude live Path of Exile 1 economy data and a curated list of crafts. With it, Claude can rank crafts by expected profit for the current league (3.30 Covens of Umbra) and walk through the steps. Claude does the explaining. The server does the data fetching, caching, probability math and profit math, so the numbers come from code, not from the model.

**Goals**

- Answer questions like "best craft for a 30 div budget right now" with a ranked list. Each entry shows expected cost, expected sale value, expected profit (in chaos and divines), success chance and how fast it sells.
- Use poe.ninja for prices of currency, materials and bases. Use saved trade-site searches to price the finished items.
- Keep crafting knowledge in a versioned recipe file, so Claude never makes up a craft and stale (pre-3.30) recipes are flagged.
- Run fully on the user's PC. Secrets stay local, and all API calls are rate-limited and cached.

**Non-goals (v1)**

- No automated trading, listing, whispering, or anything that touches the game client.
- No crafting simulator built from scratch. Use mod weights for simple odds, and enter guide-sourced odds for everything else.
- No hosted or multi-user service. This is a personal tool.
- No PoE2 support (keep the code generic enough to add it later).

## Architecture

Claude never calls poe.ninja or the trade site itself. It calls the server's tools, and the server answers from its cache when it can and calls the outside services only within their limits.

&#91;embedded content: server architecture · 6 parts, 2 outside services\]

The profit engine is the only part that does maths, so every figure Claude quotes comes from code with a stated source and age.

## Data sources

There are four sources. Only poe.ninja and the trade site are live. The other two are files kept in or next to the repo.

| Source | What it gives | How to call it | Refresh |
| --- | --- | --- | --- |
| [poe.ninja economy API](https://poe.ninja/docs/api) | League id, chaos:divine rate, prices of currency and materials, base type prices | `GET /poe1/api/economy/leagues`; `GET /poe1/api/economy/exchange/current/overview?league=&type=`; `GET /poe1/api/economy/stash/current/item/overview?league=&type=` | Every 15 min at most |
| Trade site (unofficial web API) | Sale price of finished items and salvage | `GET /api/trade/search/{league}/{savedId}` turns a saved trade URL into its query JSON; `POST /api/trade/search/{league}` returns result ids; `GET /api/trade/fetch/{id1,...}?query={searchId}` returns listings (10 ids per call); `POST /api/trade/exchange/{league}` for bulk items (2026-11-24: no longer used; exchange offers are whisper-only, see docs/decisions.md "Trade pricing") | On demand, cached 15 to 30 min per query |
| Game data export ([RePoE fork](https://github.com/lvlvllvlvllvlvl/RePoE)) | Mods, spawn weights, base items, essences, fossils, bench crafts, stat text | Download its JSON files (`mods.json`, `base_items.json`, `essences.json`, `fossils.json`, `crafting_bench_options.json`, `stat_translations.json`) into `data/gamedata/` | Once per patch, by hand |
| Recipe knowledge base | Which crafts exist, their steps, materials, odds and outputs | YAML files in `recipes/`, curated by the user from guides | When the user adds or edits recipes |

**poe.ninja details**

- The first entry from the leagues endpoint is the current challenge league. Use its `id` everywhere instead of hard-coding "Covens of Umbra".
- Exchange overview `type` values for PoE1: Currency, Fragment, Runegraft, AllflameEmber, Tattoo, Omen, DjinnCoin, DivinationCard, Artifact, Oil, DeliriumOrb, Scarab, Astrolabe, Fossil, Resonator, Essence. Exchange lines return `id`, `primaryValue`, `volumePrimaryValue`, `maxVolumeCurrency`, `maxVolumeRate` and `sparkline`. `core` returns `primary`, `secondary`, `rates` and `items`.
- Stash item overview `type` values include BaseType, ClusterJewel, Beast, SkillGem, ImbuedGem, the unique categories and Map. Lines return `name`, `baseType`, `detailsId`, `chaosValue`, `divineValue`, `variant`, `links`, `listingCount` and more.
- The type lists were documented before 3.30. Scarabs were removed in 3.30 and new currencies may have been added. Fetch each type and log any that return empty, rather than assuming the list is current.
- Policy: send a descriptive User-Agent with a contact, keep volume low, use ETags and conditional requests, and don't poll faster than the data refreshes. Responses are cached about 5 minutes, and PoE1 overviews refresh about every 15 minutes.

**Trade site details**

- The user builds each output search once on pathofexile.com/trade and pastes its URL into the recipe. The server turns the URL into query JSON and swaps in the current league id.
- Pricing an output = run the search (sorted by price, online sellers only, optionally "instant buyout"), fetch the cheapest 20 to 40 listings, convert each price to chaos with poe.ninja rates, drop outliers, and return p10, p25, median and the listing count. (2026-11-24: superseded; pricing now uses instant-buyout listings only, no online filter and no exchange, see docs/decisions.md "Trade pricing".)
- A `POESESSID` cookie is optional. With it, limits apply per account, not per IP. It gives full access to the account, so treat it as a password (see Caching, rate limits and secrets).

**Game data details**

- Use spawn weights from `mods.json` only for simple, single-step odds: chaos or alteration spam for one mod, essence spam for a second mod, and fossil-weighted rolls.
- Confirm the export has been regenerated for 3.30 before trusting it. 3.30 changed how items are generated, so pre-3.30 weights may be wrong.

## MCP tools

The server exposes nine tools. Each returns JSON with a `data_age_seconds` field and a `sources` list, so Claude can say how fresh a number is and where it came from. Read-only tools are marked `readOnlyHint: true`.

| Tool | Inputs | Returns |
| --- | --- | --- |
| `get_league_status` | none | League id and name, chaos per divine, age of each cached dataset, whether `POESESSID` is set, current trade rate-limit state |
| `get_prices` | `category` (a poe.ninja type), optional `names[]` | Rows of `{name, id, chaos, divine, volume_or_listings, sparkline_7d}` |
| `get_base_price` | `base`, optional `ilvl_min`, `influence` | Cheapest matching poe.ninja BaseType row, or `null` with a hint to use a trade query |
| `list_recipes` | optional `max_budget_div`, `tags[]`, `include_unverified` (default false) | Recipe summaries: id, name, tags, `verified_patch`, rough cost tier |
| `get_recipe` | `id` | The full recipe, as in the schema below |
| `price_trade_query` | `trade_url` or `query_json`, optional `sample` (default 30) | `{p10, p25, median, listing_count, cheapest_listing_age, currency_mix}`, all in chaos |
| `mod_odds` | `base`, `ilvl`, `target_mods[]`, `method` (`chaos`, `alt`, `essence:<id>`, `fossil:<ids>`) | Chance per attempt, expected attempts, and the weight table used |
| `evaluate_recipe` | `id`, optional `price_overrides{}` | Full profit breakdown (see Profit engine), with every input price and its source |
| `rank_crafts` | `budget_div`, optional `min_listings` (default 10), `max_minutes`, `risk` (`low`, `medium`, `high`), `limit` (default 5) | Recipes sorted by expected profit per hour, each with its breakdown and warnings |

**Behaviour rules**

- `rank_crafts` and `evaluate_recipe` only use recipes from the knowledge base. If none fit, they return an empty list with the reason, never a guess.
- Trade calls go through one shared queue. If a tool would go over the rate limit, it returns cached data marked `stale: true` rather than waiting longer than 20 seconds.
- Unverified recipes (no `verified_patch` of 3.30 or later) are left out of rankings unless asked for, and carry a warning when shown.
- Add a `refresh` tool only for the user to force a poe.ninja reload. Give it a 5-minute cooldown.

## Recipe knowledge base

Each craft is one YAML file in `recipes/`, checked against a JSON Schema (`schema/recipe.schema.json`) when the server starts. A recipe that fails validation is skipped and logged, and the server still starts.

**Fields**

- `id`, `name`, `tags[]` (for example `essence`, `fossil`, `harvest`, `bench`, `base-flip`, `bulk`)
- `status`: `verified`, `unverified` or `deprecated`; `verified_patch` (for example `3.30.0`); `sources[]` (guide URLs)
- `requires[]`: league or atlas content needed (for example `harvest`, `bestiary`, `gwennen`), so recipes can be filtered by what the user runs
- `base`: how to get the starting item, as a poe.ninja BaseType lookup or a trade URL
- `steps[]`: each step has an `action`, its `materials[]` (poe.ninja `id` or name, `category`, `qty`) and, for repeated steps, `repeat_until` plus `p_success` with `p_source`. `p_source` is `weights` (computed by `mod_odds`), `guide` (a number from a guide) or `user` (the user's own records)
- `outcomes[]`: each has a `name`, a `probability` and a `trade_url` that prices it. Outcome probabilities add up to 1, failure included
- `salvage`: what a failed attempt is worth, as a `trade_url`, a poe.ninja lookup or a fixed chaos value
- `minutes`: hands-on time for one full attempt
- `notes`: free text Claude can quote when explaining the steps

**Example** (illustrative structure only; the values are placeholders, not checked for 3.30)

```yaml
id: example-essence-body-armour
name: Essence spam life body armour (example)
tags: [essence]
status: unverified
verified_patch: null
sources: ["https://example.com/guide"]
requires: []
base:
  lookup: { source: ninja, type: BaseType, name: "Astral Plate", ilvl_min: 84 }
steps:
  - action: essence_spam
    materials:
      - { source: ninja, category: Essence, name: "Deafening Essence of Greed", qty: 1 }
    repeat_until: "T1 resistance suffix"
    p_success: { p_source: weights, method: "essence:greed", target_mods: ["<mod id>"] }
  - action: bench_craft
    materials:
      - { source: ninja, category: Currency, name: "Divine Orb", qty: 0.1 }
outcomes:
  - { name: "hit", probability: 1.0, trade_url: "https://www.pathofexile.com/trade/search/<league>/<id>" }
salvage: { fixed_chaos: 0 }
minutes: 6
notes: "Write the steps as a guide would, for Claude to explain."
```

**Filling it.** Start with 10 to 20 recipes the user already trusts, then add new 3.30 crafts once guides confirm them. Add an optional `scripts/import_recipe.md` prompt, so Claude can turn a guide's text into a draft recipe for the user to check. Drafts are always saved as `unverified`.

## Profit engine

Rank crafts by expected profit per hour of play, and leave out any craft whose outputs don't sell. All maths lives in `engine/`, is pure functions, and is unit-tested with fixed inputs.

**Cost.** For a repeated step with success chance p per attempt, the number of attempts follows a geometric distribution:

```latex
E[\text{attempts}] = \frac{1}{p} \qquad n_{90} = \left\lceil \frac{\ln 0.1}{\ln(1-p)} \right\rceil
```

```latex
C = C_{\text{base}} + \sum_{\text{steps}} c_{\text{attempt}} \cdot E[\text{attempts}] \qquad C_{90} = C_{\text{base}} + \sum_{\text{steps}} c_{\text{attempt}} \cdot n_{90}
```

C is the expected cost. C90 is the cost on a bad run (90th percentile), used for risk.

**Revenue.** Trade listings are asking prices, not sale prices, so value outputs below the median:

```latex
V = \sum_i p_i \cdot P_{25,i} \cdot (1 - u) + p_{\text{fail}} \cdot S
```

Here p\_i is each outcome's chance, P25,i is the 25th percentile of its listings, u is the undercut (default 5%) and S is the salvage value.

**Profit and ranking.**

```latex
\text{Profit} = V - C \qquad \text{Profit/hr} = \frac{V - C}{\text{minutes}/60}
```

- Sort by profit per hour. Show profit per craft and ROI (profit ÷ C) beside it.
- Risk filter: `low` keeps crafts where C90 is within budget, `medium` keeps those where C is within budget, and `high` keeps everything.
- Liquidity filter: drop or flag outputs with fewer than `min_listings` listings. Flag materials whose poe.ninja volume is low, since buying 200 of them may move the price.
- Show values in chaos, with divines alongside at the current rate. Divide by the rate only for display; never round in between steps.

**Cleaning listing prices.** Before taking percentiles, drop listings priced below 50% of the median of the cheapest 10. These are usually price-fixing or mistakes. Then convert each listing to chaos using poe.ninja's rate for its currency. Report how many listings were dropped.

**Every number shows its source.** Each breakdown lists every input price with its source (poe.ninja type and id, or trade query id) and its age. Claude must quote that age when it is older than 30 minutes.

## Caching, rate limits and secrets

All HTTP goes through one cache and one limiter per host. That keeps the tool polite to poe.ninja and GGG, and keeps the account safe.

**Cache.** One SQLite file (`data/cache.db`):

- `http_cache(url, etag, body, status, fetched_at)` for raw responses.
- `price_snapshots(league, source, item_id, chaos, volume, taken_at)` for history. This makes it possible to add trend checks later and to rerun an evaluation against an older snapshot.
- poe.ninja: at most one request per league and type every 15 minutes, always with `If-None-Match`.
- Trade: one entry per query hash, kept 20 minutes by default (set in config).

**Trade rate limiter.**

- Only one trade request in flight at a time, through a single async queue.
- Read `X-Rate-Limit-Rules`, then each `X-Rate-Limit-{rule}` and `X-Rate-Limit-{rule}-State` header. Each holds `hits:period:restriction` triples. Before each call, wait until every rule has room, with one hit to spare.
- On HTTP 429, read `Retry-After`, pause the whole queue for that long plus 10%, and return cached data marked stale to any tool that is waiting.
- Before the first response, run at one request every 2 seconds. Search and exchange share one limit policy; fetch has its own.
- Log every rate-limit state to `data/logs/ratelimit.log` so limits can be tuned.

**User-Agent.** Send `poe-craft-mcp/<version> (contact: <email from config>)` to both hosts. Don't copy a browser's User-Agent. GGG's [developer docs](https://www.pathofexile.com/developer/docs) ask for an identifiable User-Agent with a contact, and poe.ninja's policy asks for the same.

**Secrets.**

- `POESESSID` is optional. Store it in the OS keychain through the `keyring` library, with a `POESESSID` environment variable as a fallback. Never put it in config files or the repo.
- Never log it, never return it from a tool and never include it in error messages. A test checks that it is masked in every log line.
- Without it, the trade client still works, with IP-based limits.

**Third-party notice.** The README states that the tool isn't affiliated with or endorsed by Grinding Gear Games, as GGG's developer docs ask.

## Tech stack, layout and config

Use Python 3.12 with the official MCP Python SDK (FastMCP), over stdio. It suits the maths, the YAML files and the tests, and Claude Desktop, Claude Code and T3 Code can all launch it.

| Concern | Choice |
| --- | --- |
| MCP server | `mcp` Python SDK, FastMCP, stdio transport (streamable HTTP later, for a dashboard) |
| HTTP | `httpx` (async) |
| Models and validation | `pydantic` v2, `jsonschema` for recipe files |
| Recipes | `PyYAML` |
| Cache | `sqlite3` from the standard library, run through `asyncio.to_thread` |
| Secrets | `keyring` |
| Env and packaging | `uv`, with a `pyproject.toml` entry point `poe-craft-mcp` |
| Tests | `pytest`, `pytest-asyncio`, `respx` for mocked HTTP |

**Repo layout**

```
poe-craft-mcp/
  pyproject.toml
  README.md            # setup, third-party notice, MCP client snippets
  config.example.toml
  src/poe_craft_mcp/
    server.py          # registers the MCP tools
    config.py
    http/cache.py      # SQLite cache, ETags
    http/ratelimit.py  # GGG header parser and queue
    clients/ninja.py
    clients/trade.py   # saved URL to query, search, fetch, exchange
    gamedata/loader.py
    gamedata/odds.py   # mod_odds from spawn weights
    recipes/models.py  # pydantic models
    recipes/loader.py
    engine/pricing.py  # listing cleanup, percentiles, chaos conversion
    engine/profit.py   # cost, value, profit, C90
    engine/rank.py
  schema/recipe.schema.json
  recipes/             # one YAML per craft
  data/                # gitignored: cache.db, gamedata/, logs/
  scripts/import_recipe.md
  tests/
    fixtures/          # recorded poe.ninja and trade JSON
```

**config.toml**

```toml
league = "auto"            # first league from poe.ninja, or an explicit id
contact = "you@example.com"
ninja_min_interval_min = 15
trade_cache_min = 20
trade_sample = 30
undercut_pct = 5
min_listings = 10
include_unverified = false
```

**Hooking it up.** Claude Code: `claude mcp add poe-craft -- uv --directory <repo path> run poe-craft-mcp`. Claude Desktop: add the same command and args under `mcpServers` in `claude_desktop_config.json`. T3 Code is only needed to build the server. To use it, register it with whichever Claude client you chat in.

## Build plan for the coding agent

Build in this order. Each milestone ends with passing tests and a commit. Tests never call live APIs: they use recorded fixtures in `tests/fixtures/`. A separate `scripts/smoke_live.py` makes a few real calls, for manual checks only.

1. **Scaffold.** Set up the `uv` project, config loading, and a FastMCP server with a stub `get_league_status`.
   - Done when `claude mcp add` connects and the tool shows up in Claude.
2. **poe.ninja client and cache.** Add the leagues, exchange overview and stash item overview calls, with the SQLite cache, ETags and the 15-minute floor. Then add `get_prices`, `get_base_price` and a real `get_league_status`.
   - Done when `get_league_status` returns the live league id and divine rate, and a second call inside 15 minutes makes no HTTP request (checked with `respx`).
3. **Recipes.** Add the JSON Schema, pydantic models, loader, `list_recipes` and `get_recipe`, plus three sample recipes.
   - Done when an invalid recipe is logged and skipped without crashing, and unverified recipes are hidden by default.
4. **Trade client and limiter.** Add saved-URL resolution, search, fetch and exchange, the header-driven limiter, listing cleanup and `price_trade_query`.
   - Done when a replayed 429 with `Retry-After` pauses the queue for the right time, and a test confirms `POESESSID` never appears in logs or tool output.
5. **Profit engine.** Add cost, value, C90, profit per hour, the risk and liquidity filters, `evaluate_recipe` and `rank_crafts`.
   - Done when unit tests pass for known values (p = 0.1 gives 10 expected attempts and n90 = 22), and the ranking is stable on fixtures.
6. **Game data and odds.** Add the RePoE loader and `mod_odds` for chaos, alteration, essence and fossil methods.
   - Done when single-mod odds match a hand calculation from the fixture weights.
7. **Polish.** Add the recipe import prompt, the README (with the GGG notice and client snippets), price history snapshots and a `refresh` tool.
   - Done when a new user can follow the README from clone to first ranked answer.

## Risks and open questions

The biggest risk is stale crafting knowledge. 3.30 [changed how every item is generated, removed Scarabs and reworked Harvest, Bestiary and Delirium](https://mobalytics.gg/poe/guides/3-30-patch-notes-covens-of-umbra), so many older guides and mod weights no longer hold.

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Pre-3.30 guides and weights | Wrong odds, crafts that no longer work | `verified_patch` on every recipe; unverified recipes hidden by default; check the RePoE export was rebuilt for 3.30 |
| Asking price is not sale price | Profit overstated | Use the 25th percentile minus an undercut; later, add an optional log of real sales to tune `undercut_pct` |
| Trade site rate limits | Slow answers, temporary lockouts | One request at a time, header-driven limiter, 20-minute cache, stale data rather than waiting |
| Trade web API is unofficial | It can change or break without notice | Keep it isolated in `clients/trade.py` with recorded fixtures, so breakage shows up in one place |
| poe.ninja endpoints change | Price calls fail | Endpoints are [unversioned with no SLA](https://poe.ninja/docs/api); keep paths in config and log failures clearly |
| poe.ninja policy on desktop apps | The docs say desktop apps should go through their own backend | This is a single-user tool at a few calls per hour; if poe.ninja objects, run the fetcher on a homelab box and point the server at it |
| Crafts get crowded | Material prices rise, output prices fall | Show the 7-day sparkline trend for key materials and outputs; rerun rankings before a big batch |

**Open questions**

- [ ] Which contact email should go in the User-Agent?
- [ ] Which 10 to 20 crafts should the starter recipe set hold?
- [ ] Use `POESESSID` for account-based trade limits, or start without it?
- [ ] Is a phone dashboard wanted later? That would add a small HTTP endpoint and an artifact that reads a published JSON snapshot.
- [ ] Which new 3.30 currencies and materials does poe.ninja track? Check the type list once the league has settled.
