# poe-craft-mcp

A local [MCP](https://modelcontextprotocol.io) server that gives Claude live Path of Exile 1
economy data and a list of crafts you trust, so it can rank crafts by expected profit per hour
for the current league. Claude does the explaining; the server fetches prices (poe.ninja and the
trade site), caches them politely, and does the probability and profit maths in code, so the
numbers are not made up by the model.

**What it is not.** It does not trade, list, whisper or buy anything, and it never touches the
game client. It is a single-user tool that runs on your own PC. It does not ship a verified
craft list: you add the crafts you trust (see [Writing recipes](#writing-recipes)).

## Third-party notice

This product isn't affiliated with or endorsed by Grinding Gear Games in any way.

| Data | Source |
| --- | --- |
| Currency, essence, fossil, base and other prices | [poe.ninja](https://poe.ninja) |
| Prices of finished items | the Path of Exile trade site (pathofexile.com) |
| Mod spawn weights (for `mod_odds`) | the [RePoE fork](https://repoe-fork.github.io) game data export |

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12+ for you)
- Git
- Claude Code, Claude Desktop or another MCP client
- Windows, macOS or Linux

## Quick start

1. **Clone and install.**

   ```sh
   git clone https://github.com/DaAwesomePwner/poe-craft-mcp.git
   cd poe-craft-mcp
   uv sync
   ```

2. **Create your config.** Copy the example and set `contact`: poe.ninja and GGG both ask for an
   identifiable contact in the User-Agent. Without it the repo URL is sent.

   ```sh
   cp config.example.toml config.toml     # Windows PowerShell: Copy-Item config.example.toml config.toml
   ```

   ```toml
   contact = "you@example.com"
   ```

   `config.toml` is gitignored. Everything else has a sensible default (see
   [Configuration](#configuration)).

3. **Optional: store your POESESSID.** Without it the trade site still works, with IP-based
   limits. With it, you may get your account's limits. It goes in the OS keychain, never in a
   file:

   ```sh
   uv run python -m poe_craft_mcp.secrets set      # prompts, input hidden
   uv run python -m poe_craft_mcp.secrets status   # "POESESSID: set (keyring)", "set (env)" or "not set"
   uv run python -m poe_craft_mcp.secrets clear
   ```

   No keychain? Set the `POESESSID` environment variable instead. The value is never logged,
   never returned by a tool and never accepted in `config.toml`.

4. **Download the game data** (needed only for `mod_odds` and recipes that use `p_source: weights`):

   ```sh
   uv run python scripts/fetch_gamedata.py
   ```

   This writes `data/gamedata/` from the RePoE fork export. Rerun it after a patch. The
   previous export is kept as `data/gamedata.previous`.

5. **Register the server with Claude.**

   **Claude Code**

   Run this with the absolute path of your clone, in quotes (it may contain spaces):

   ```sh
   # Windows
   claude mcp add poe-craft -- uv --directory "C:\path\to\poe-craft-mcp" run poe-craft-mcp
   # macOS / Linux
   claude mcp add poe-craft -- uv --directory "/path/to/poe-craft-mcp" run poe-craft-mcp
   ```

   **Claude Desktop**: add this under `mcpServers` in `claude_desktop_config.json` (Windows:
   `%APPDATA%\Claude\claude_desktop_config.json`; macOS:
   `~/Library/Application Support/Claude/claude_desktop_config.json`), then restart Claude
   Desktop. Use your own clone path; on Windows write backslashes doubled.

   ```json
   {
     "mcpServers": {
       "poe-craft": {
         "command": "uv",
         "args": ["--directory", "C:\\Users\\you\\Documents\\GitHub\\poe-craft-mcp", "run", "poe-craft-mcp"]
       }
     }
   }
   ```

   **T3 Code** is only needed to build the server. To use it, register it with whichever Claude
   client you chat in, as above.

   `uv --directory <repo>` makes the server run from the repo, so it finds `config.toml`,
   `recipes/` and `data/` there. If you launch it some other way, set `POE_CRAFT_CONFIG` to the
   config file and `POE_CRAFT_RECIPES` to the recipe folder.

6. **Ask Claude**, then follow [Your first ranked answer](#your-first-ranked-answer).

## First questions

- "What's the best craft for a 30 div budget right now?"
- "What league is it and what's a divine worth?"
- "List my recipes, including unverified ones."
- "Evaluate the `<recipe id>` craft. What does it cost and what does it sell for?"
- "What's the price of a 2-link Astral Plate base, ilvl 84+?"
- "Price this trade search: `<trade URL>`"
- "What's my chance per attempt of rolling `<mod id>` on a Vaal Regalia at ilvl 84 with chaos orbs?"
- "Which Medium Cluster Jewel enchant with 4 passives has the most notable pairs worth 1 divine?"

**What to expect on a fresh clone.** `recipes/` holds two kinds of files, all `unverified`, so
rankings and `list_recipes` hide them by default:

- three `example-*` structural templates, not working crafts, with placeholders
  (`<league>`/`<...>` trade URLs, a `<mod id>`, example guide URLs). **They will not rank until
  you replace those placeholders**, even with `include_unverified` on;
- three starter crafts drafted from public guides with real trade searches and odds from the
  game data (two utility flasks and a fossil-crafted minion wand). They evaluate live, but
  nobody has checked them in game; see [docs/starter-recipes.md](docs/starter-recipes.md) for
  what they are, how their odds were derived and what to verify.

A first question like "best craft for 30 div" may still answer that nothing is rankable (only
unverified recipes, or a sale with too few listings). That is expected.

## Your first ranked answer

1. **Pick or write a recipe.** Copy an example in `recipes/` (or write one, see
   [Writing recipes](#writing-recipes), or have Claude draft one from a guide with
   [`scripts/import_recipe.md`](scripts/import_recipe.md)) and give it a new `id`.
2. **Make a trade search for each outcome.** On pathofexile.com/trade, build the search for each
   finished item the craft can produce, copy the URL and paste it unchanged into the recipe's
   `trade_url` fields. Replace every other placeholder (`<mod id>`, guide URLs, `TODO`). Make
   sure the failure outcome and `salvage` have real values.
3. **Restart the server** (Claude Code: start a new session or `/mcp` reconnect; Claude
   Desktop: restart the app). Recipes are loaded at startup.
4. **Check that everything resolves.** Ask Claude to `get_recipe` and `evaluate_recipe` your
   recipe. The breakdown must show a price for the base, every material and every outcome. Any
   missing price (unknown item name, unresolved trade URL, bad `<mod id>`) is reported there;
   fix it and restart again. `list_recipes` shows `load_errors` if the file failed validation.
5. **Rank.** Ask "What's the best craft for a 30 div budget right now? Include unverified
   recipes." (Claude passes `include_unverified=true`; or set `include_unverified = true` in
   `config.toml`.) The recipe appears with an "unverified" warning that Claude should relay.
6. **Optionally mark it verified.** Once you have checked it in game against patch 3.30 or later,
   set `status: verified` and `verified_patch: "3.30.0"` (quoted). It then ranks by default.

Every number comes with its source and age (`data_age_seconds`). Rankings sort by expected
profit per hour: the price of each outcome is the 25th percentile of its instant-buyout trade
listings minus a 5% undercut, and risk `low` keeps crafts whose bad-luck cost (C90, the 90th-percentile number
of attempts) still fits your budget.

## Tools

| Tool | What it does | Network |
| --- | --- | --- |
| `get_league_status` | Current league, chaos per divine, age of each cached dataset, whether POESESSID is set (never its value), trade rate-limit state | poe.ninja (cached) |
| `get_prices(category, names?)` | poe.ninja prices for one type (Currency, Essence, Fossil, Oil, BaseType, SkillGem, ...) | poe.ninja (cached) |
| `get_base_price(base, ilvl_min?, influence?)` | Cheapest poe.ninja BaseType row for a base | poe.ninja (cached) |
| `list_recipes(max_budget_div?, tags?, include_unverified?)` | Browse recipes; unverified are hidden by default | none |
| `get_recipe(id)` | One full recipe with its warnings | none |
| `price_trade_query(trade_url? \| query_json?, sample?)` | p10 / p25 / median in chaos of the cheapest instant-buyout listings of a trade search | trade site (cached 20 min, rate limited) |
| `mod_odds(base, ilvl, target_mods, method)` | Chance per attempt for chaos, alt, essence or fossil crafting, from RePoE spawn weights, plus attempts for a 90% chance | none (needs game data) |
| `cluster_combo_survey(base, passives, ilvl, min_value_div=1, ...)` | Ranks the current, searchable enchants of a Medium or Large Cluster Jewel by the chance per attempt of landing a notable pair that looks worth at least `min_value_div` divines, then by how many such pairs there are. Discovery searches only, so values are provisional; see [Finding sellable cluster combos](#finding-sellable-cluster-combos) | poe.ninja and trade, 2 searches per enchant (cached 20 min; call again to continue) |
| `cluster_combos(base, enchant, passives, ilvl, min_value_div=1, ...)` | For one enchant: confirms candidate notable pairs worth at least `min_value_div` divines with exact instant-buyout searches (up to `max_pair_searches` live searches per call; call again for the rest), then, for the pairs confirmed so far, the chance, expected value, cost and profit per attempt and per hour of Alteration/Augmentation until a notable of a sellable pair shows, then Regal | poe.ninja and trade (needs game data) |
| `evaluate_recipe(id, price_overrides?)` | Full cost / value / profit breakdown of one recipe, with every input's source and age | poe.ninja and trade |
| `rank_crafts(budget_div, min_listings?, max_minutes?, risk="medium", limit=5, include_unverified?, tags?)` | Ranks recipes by profit per hour within a budget | poe.ninja and trade |
| `refresh` | Only when you ask: reload poe.ninja data now instead of waiting out the 15-minute floor. Once per 5 minutes; never touches the trade site | poe.ninja |

## Finding sellable cluster combos

A rare Medium or Large Cluster Jewel with the right two notables can sell for divines, but most
pairs sell for little. Two tools find the pairs worth crafting for:

1. **Survey the enchants.** Ask for `cluster_combo_survey` with the base (Medium or Large), your
   passive count and item level, e.g. "Which Medium Cluster Jewel enchant with 4 passives has
   the most notable pairs worth 1 divine?" It ranks the base's current enchants that the trade
   site can search for by the chance per attempt of landing a provisionally sellable pair, then
   by how many such pairs there are, and lists the skipped enchants with reasons.
2. **Confirm the top few.** Run `cluster_combos` on the best enchants. It prices candidate pairs
   with their exact trade searches, most likely first: cached searches are free, then up to
   `max_pair_searches` (12) live searches per call; the rest are listed as unconfirmed and
   calling again continues. It adds the miss value, white base, orb costs and profit per hour
   for the pairs confirmed so far.
3. **Craft** the enchant with the best confirmed profit per hour.

- **Sellable** means the 25th percentile of a pair's instant-buyout listings from its exact
  search, less the 5% undercut, is at least `min_value_div` divines, with at least
  `min_listings` listings. In discovery, a pair with `min_listings` or more listings priced
  strictly below that threshold is not sellable.
- **The craft priced** is the anchor strategy: Transmutation, then Alteration (and Augmentation)
  until the magic jewel shows an anchor (a notable of any sellable pair), then one Regal Orb.
  The rare is a hit when it holds a sellable pair. More sellable pairs do not always mean
  better odds: a pair between notables you already chase adds no anchor, only more rares that
  count as hits, and a new anchor that rarely completes a pair can lower the chance per
  attempt. Compare the chance and the profit, not the pair count.
- **Provisional vs confirmed.** The survey only runs two broad discovery searches per enchant
  and values each candidate pair from one of those listings (the `min_listings`-th cheapest at
  or above the threshold, or the cheapest when fewer are listed, less the undercut). It is cheap and good for ranking, but it
  confirms nothing. `cluster_combos` confirms pairs with exact searches that exclude every
  other notable, and only its numbers should decide a craft.
- **Rate limits.** The trade site allows roughly 30 searches per 5 minutes, so a cold survey of
  every Medium enchant (2 searches each) takes several minutes. The survey uses cached results
  first, read straight from the cache, then searches live until `max_seconds` (90 by default),
  a rate limit or a stale reply, and lists the rest as `pending` with the retry hint. Call it again with the same inputs to continue: results are cached for 20
  minutes (`trade_cache_min`).

The model, its odds derivation and its known approximations are in
[docs/cluster-combos.md](docs/cluster-combos.md).

## Writing recipes

One YAML file per craft in `recipes/`. The server checks every file against
[`recipe.schema.json`](src/poe_craft_mcp/recipes/recipe.schema.json) when it starts. A bad
file is skipped and logged, and `list_recipes` reports it under `load_errors`; the server still
starts. Restart the server to pick up new or changed recipes. Set `POE_CRAFT_RECIPES` to use a
folder outside the repo.

Minimal example:

```yaml
id: alt-spam-life-ring
name: Alteration spam life ring
tags: [alt, bulk]
status: unverified
verified_patch: null
sources: ["https://example.com/guide"]
base:
  lookup: { source: ninja, type: BaseType, name: "Vermillion Ring", ilvl_min: 84 }
steps:
  - action: alt_spam
    materials:
      - { source: ninja, category: Currency, name: "Orb of Alteration", qty: 1 }
    repeat_until: "T1 life prefix"
    p_success: { p_source: guide, value: 0.05, source_url: "https://example.com/guide" }
outcomes:
  - { name: "hit", probability: 0.4, trade_url: "https://www.pathofexile.com/trade/search/Standard/abc123" }  # example id: paste your own saved search
  - { name: "miss", probability: 0.6, use_salvage: true }
salvage: { fixed_chaos: 1 }
minutes: 10
notes: "Alt until a T1 life prefix, then sell."
```

Fields (all are checked; unknown keys are rejected): `id`, `name`, `status`, `base`, `steps`,
`outcomes`, `salvage` and `minutes` are required; `tags`, `sources`, `requires`, `budget_div`,
`verified_patch` and `notes` are optional. The full reference with every field, chance source
and outcome type is in [docs/recipes.md](docs/recipes.md).

**Verification rules**

- A recipe counts as verified only with `status: verified` **and** `verified_patch` at or
  after `3.30`. Anything else (`unverified`, an older patch, `deprecated`) is hidden from
  `rank_crafts` and `list_recipes` unless you ask for unverified ones. `deprecated` is never
  listed.
- **Always quote `verified_patch`**: `verified_patch: "3.30.0"`. An unquoted `3.30` is read by
  YAML as the number 3.3, which is a different (older) patch.
- Verify a recipe only after you have checked every number against the current patch, and
  replaced every placeholder.

**Failure is an outcome.** Outcome probabilities must sum to 1 (a failed attempt is just
another outcome). Price a failure with `fixed_chaos: 0` for a total loss, `fixed_chaos: <n>`
or `use_salvage: true` (uses the recipe's `salvage`). Those two are for failures only: a sale
must be priced by a `trade_url`, or the recipe is evaluated but never ranked.

**Making a trade URL.** On pathofexile.com/trade, build the search for the finished item
(the same filters you would use to sell it), run it, and copy the address-bar URL:
`https://www.pathofexile.com/trade/search/<league>/<id>`. Paste it into the recipe
**unchanged**, league included: the server needs it to look up a saved search, and then swaps
in the current league when it runs the search. If the URL contains the whole query, no extra
request is needed to resolve it; otherwise the server fetches it once and caches it.

**Instant buyout only.** Every price and listing count comes from instant-buyout
(asynchronous trade) listings: the server sends every search with status `securable`,
whatever online/any status the URL was saved with, and drops any fetched listing that is not
instant buyout. Whisper listings are never used, since anyone can list at any price and ignore
whispers. Bulk exchange searches (`/trade/exchange/...`) only list whisper offers, so they are
refused: use an item search instead.

**Odds.** `p_success` can be a number from a guide (`p_source: guide`), your own record
(`p_source: user`) or computed from mod weights (`p_source: weights`, same arguments as
`mod_odds`; needs the game data). Probabilities below 1e-12 are treated as unsupported.

**Importing from a guide.** Paste [`scripts/import_recipe.md`](scripts/import_recipe.md) and
the guide's text into Claude. It returns a draft recipe, always `unverified`, with
placeholders marked and a checklist for you to work through.

## Configuration

`config.toml` is read from `$POE_CRAFT_CONFIG`, else `./config.toml` in the folder the server
runs from, else built-in defaults. [`config.example.toml`](config.example.toml) documents every
key. The ones you are most likely to change:

| Key | Default | Meaning |
| --- | --- | --- |
| `contact` | repo URL | Contact in the User-Agent sent to poe.ninja and pathofexile.com |
| `league` | `"auto"` | League id, or the current challenge league from poe.ninja |
| `include_unverified` | `false` | Show and rank unverified recipes |
| `undercut_pct` | `5` | Undercut applied to the 25th-percentile listing price |
| `min_listings` | `10` | Outputs with fewer instant-buyout trade listings are dropped or flagged (liquidity) |
| `trade_cache_min` | `20` | Minutes a trade search result stays cached |
| `ninja_min_interval_min` | `15` | Minimum minutes between refreshes of a poe.ninja dataset (15 or more) |
| `data_dir` | `data/` | Where `cache.db`, `gamedata/` and `logs/` live |

## Data, caching and rate limits

- **Everything is local.** `data/cache.db` (SQLite) holds cached responses and
  `data/gamedata/` the game data export; both are gitignored. Logs are in `data/logs/`
  (`server.log`, `ratelimit.log`).
- **poe.ninja**: at most one request per league and dataset every 15 minutes, with
  `If-None-Match`. Answers show their age; Claude is told to quote it.
- **Trade site**: one request at a time through a header-driven limiter, results cached for
  20 minutes. On HTTP 429 the whole queue pauses and old cached listings are returned marked
  `stale: true` rather than waiting.
- **User-Agent**: `poe-craft-mcp/<version> (+<repo>; contact: <your contact>)`. Set `contact`.
- **Secrets**: only POESESSID, only in the OS keychain or the environment. The server's
  logs mask it.
- The trade web API is unofficial and can change without notice. Ranking prices every outcome
  through the rate limiter, so the first answer after a cold start can be slow; repeats within
  the cache window make no trade requests.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "no verified recipes" or an empty ranking | The shipped recipes are unverified: the examples are templates with placeholders, the starter crafts need checking in game (see [docs/starter-recipes.md](docs/starter-recipes.md)). Follow [Your first ranked answer](#your-first-ranked-answer): replace the placeholders, then rank with `include_unverified` or verify the recipe. `list_recipes` shows how many are hidden |
| `list_recipes` shows `load_errors` | A recipe file failed validation and was skipped; the `reason` names the problem. Common: unquoted `verified_patch`, probabilities not summing to 1, an unknown key |
| "no recipes loaded" | The server cannot find `recipes/`. Launch with `uv --directory <repo> run poe-craft-mcp`, or set `POE_CRAFT_RECIPES` |
| Game data missing, or `mod_odds` fails | Run `uv run python scripts/fetch_gamedata.py` |
| "a game data fetch is in progress or was interrupted" | A fetch was killed and left `data/gamedata.fetch.lock`. If no fetch is running, delete that file by hand and fetch again |
| Prices are old or `stale: true` | poe.ninja or the trade site failed or is rate limiting (see `data/logs/ratelimit.log`), so cached data was served. Wait and retry; `refresh` reloads poe.ninja data (not the trade site) |
| "Price unavailable ... Try again in about Ns" | The trade site rate-limited you with nothing cached. Wait that long |
| The server does not appear in Claude | Run `uv --directory <repo> run poe-craft-mcp` in a terminal and read the error (a bad `config.toml` prints a message and exits). Restart Claude Desktop after editing its JSON |
| League or prices wrong after a new league starts | Set `league = "<id>"` explicitly, or use `refresh` |

To check your setup against the live sites without Claude, run a manual smoke test (a handful
of real requests; never run by the tests or CI):

```sh
uv run python scripts/smoke_live.py --live
uv run python scripts/smoke_live.py --live --trade-url "https://www.pathofexile.com/trade/search/<league>/<id>"
```

## Development

```sh
uv sync --locked        # create .venv with runtime and dev dependencies
uv run pytest -q        # tests: recorded fixtures, never live APIs
uv run ruff check .     # lint
uv run ruff format .    # format (CI runs `ruff format --check .`)
```

CI runs the same checks on Ubuntu and Windows with Python 3.12 and 3.13. The design and its
rationale are in [docs/design.md](docs/design.md).

## License

[MIT](LICENSE)
