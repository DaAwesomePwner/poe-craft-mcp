# Starter recipes

Crafts taken from public guides and written as recipe files, so `rank_crafts` has real crafts
to evaluate: three flask and wand crafts, and four cluster jewel crafts in
[Cluster jewels](#cluster-jewels). All of them are **unverified**: nobody has checked the steps in game, and
they stay hidden from rankings unless you pass `include_unverified=true`. The `notes` of each
recipe walk through the craft step by step; `get_recipe <id>` shows them.

| Recipe id | What you make | Guide (patch) |
| --- | --- | --- |
| `quicksilver-experimenters-cheetah` | Magic Quicksilver Flask, Experimenter's (36-40% duration) + of the Cheetah (12-14% movement speed) | [Exile's Foundry](https://exilesfoundry.com/blog/poe-league-start-profit-crafting/) (3.30, Nov 2026); method from the [PoE Vault flask guide](https://poe-vault.com/guides/ultimate-flask-crafting-guide) |
| `granite-flagellants-armadillo` | Magic Granite Flask, Flagellant's (3 charges when hit) + of the Armadillo (56-60% armour) | same |
| `convoking-wand-spell-minion-levels` | Rare Convoking Wand with +1 spell and +1 minion gem levels | [PoE Vault](https://poe-vault.com/guides/crafting-2-generic-minion-wand) (**pre-3.30**, July 2024) |

## Live ranking (instant buyout only)

`rank_crafts(budget_div=30, include_unverified=true, min_listings=1)`, finished 2026-11-24 04:38 UTC,
league Covens, 1 divine = 197.7 chaos, up to 30 instant-buyout listings per search (config
`trade_sample` 30), 5% undercut. Prices
come only from instant-buyout (asynchronous trade) listings; whisper listings are never used.
From a cold cache the 21 searches hit the trade site's search rate limit, so the ranking was rerun
in four rounds (waiting out the limit) until every recipe was priced; the oldest quote in the final
ranking was about 37 minutes old. Chaos values per attempt: C = expected cost, C90 = cost of a bad
(90th percentile) run, V = expected value.

| Rank | Recipe | C | C90 | V | Profit | Profit/hr | ROI | Pair / target sale (p25, listings) | Miss / off-target sale (p25, listings) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `medium-cluster-minion-life-rebirth` | 13.7 | 17.9 | 32.6 | 18.9 | 283 | 1.37 | 589.5 c (30), p 0.0183 | 24 c (30), p 0.9817 |
| 2 | `large-cluster-spell-mage-hunter` | 17.1 | 30.5 | 37.2 | 20.1 | 241 | 1.18 | 393 c (30), p 0.0481 | 21.25 c (30), p 0.9519 |
| 3 | `medium-cluster-crit-malice-pressure-points` | 20.6 | 27.2 | 31.8 | 11.2 | 168 | 0.54 | 1179 c (23), p 0.0117 | 20 c (30), p 0.9883 |
| 4 | `medium-cluster-chaos-dot-wicked-pall` | 17.0 | 29.3 | 23.7 | 6.8 | 81 | 0.40 | 196.5 c (30), p 0.0283 | 20 c (30), p 0.9717 |
| 5 | `quicksilver-experimenters-cheetah` | 30.2 | 51.0 | 12.2 | -18.0 | -180 | -0.60 | 35 c (30), p 0.0263 | 12.25 c (30), p 0.9737 |
| 6 | `granite-flagellants-armadillo` | 60.8 | 88.1 | 42.5 | -18.3 | -157 | -0.30 | 589.5 c (24), p 0.0263 | 30 c (30), p 0.9737 |
| 7 | `convoking-wand-spell-minion-levels` | 4792 | 10944 | 1680 | -3112 | -15560 | -0.65 | 1768.5 c (30), p 1.0 | - |

Read this before acting on it:

- **Much of the cluster value comes from the misses.** About 95-99% of attempts end as a
  one-notable rare (95.2% for Large Spell). Their instant-buyout *asking* prices (p25) are about
  20-24 c, modelled at about 19-22.8 c after the 5% undercut; neither is an observed sale price, and the misses make up roughly
  52-78% of each cluster craft's expected value. Profit depends on actually selling many
  one-notable rares near that price, so list them steadily and re-check the price; a few dozen
  sales can move it.
- The Quicksilver base was flagged for low volume, and both flask crafts and the wand lose money at
  these prices.
- These are snapshot numbers: rerun `rank_crafts` before a big batch. The earlier snapshots below
  were taken with online (whisper) listings and are superseded.

## How the odds were derived

Nothing is guessed: every chance comes from the RePoE export for 3.30.0.4.3 through the
project's own `mod_odds`, or from arithmetic on its weight table that the recipe's `notes`
write out.

- **Flasks.** Step 2 (Alteration spam until the suffix appears) uses `p_source: weights`
  (`mod_odds`, method `alt`): 0.01391 per Alteration for of the Cheetah on an ilvl 85
  Quicksilver, 0.01058 for of the Armadillo on an ilvl 84 Granite. When the suffix arrives,
  the prefix slot is either empty (then one Augmentation fills it) or already filled by the
  same Alteration. In both cases the prefix is drawn in proportion to prefix weight, so it is
  the wanted prefix with chance 600 / 22800 = 1/38 = 0.0263158 (prefix weight / all prefix
  weight). That is the main sale's probability; the other 0.9736842 is a flask with the right
  suffix and one other prefix. The procedure always sells it, so it is a second sale outcome
  with its own trade search (right suffix, exactly one prefix, not the target prefix, not
  corrupted), priced and liquidity-checked like the main sale; suffix-only flasks never occur
  because the procedure always augments, so the search requires a prefix. Nothing goes to
  `salvage` (kept at 0 and unused). The Augmentation quantity
  (0.4226 and 0.4215) is the share of hits where the suffix came alone. The guide's own route
  (keep alt+aug spamming until both mods are on one flask) is about 1 in 2366 Alterations for
  the Quicksilver and 1 in 3103 for the Granite; it is described in the notes but not modelled.
- **Flask bases** are white, not corrupted (a corrupted flask cannot be changed with currency).
  The Quicksilver base is item level 85+; the Granite base is item level exactly 84, because at
  85 of the Cheetah joins a Granite's suffix pool and the Armadillo odds drop (95.85 instead of
  94.48 Alterations on average).
- **Wand.** `mod_odds` with Corroded + Jagged + Shuddering + Metallic Fossils on an ilvl 84
  Convoking Wand gives P(+1 spell) = 0.1102, P(+1 minion) = 0.3211 and P(either) = 0.3497
  (Monte Carlo, 20000 samples, one seed). Both together = 0.1102 + 0.3211 - 0.3497 = 0.0816
  per resonator, recorded as `p_source: user` because `mod_odds` reports "at least one target"
  only. The guide's "around 10-12 tries" agrees (1 / 0.0816 = 12.3). A Chaotic Resonator
  rerolls a rare item, so the recipe starts with one Orb of Alchemy on a white wand. Calling and
  Convening Wands can also roll the minion prefix; the recipe, odds and searches cover Convoking
  Wand only, as the guide does.

Minutes are estimates (clicks per attempt times a few seconds, plus buying and listing); each
recipe's notes give the basis.

## Live snapshot (superseded)

**Superseded** by [Live ranking (instant buyout only)](#live-ranking-instant-buyout-only): taken with online (whisper) listings.

`evaluate_recipe` for each recipe and `rank_crafts(budget_div=30, include_unverified=true)`,
run 2026-11-24 03:54 UTC, league Covens, 1 divine = 197.7 chaos, 10 trade listings per search,
5% undercut, `min_listings` 10. The base and off-target searches were fetched live in this run;
the three main sale searches were served from the trade cache, fetched about 9 minutes earlier
(03:46 UTC). Chaos values; C = expected cost of one attempt, C90 = cost of a bad (90th
percentile) run, V = expected value.

| Recipe | C | C90 | V | Profit | Profit/hr | Rankable | Key warnings |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `quicksilver-experimenters-cheetah` | 23.1 | 43.7 | 25.8 | 2.7 | 27 | yes, but excluded by `rank_crafts` | main sale search had **1** listing (1 div): illiquid (< 10); off-target sale 22.5 c (26 listed); white uncorrupted ilvl 85+ base 7 c (17 listed) |
| `granite-flagellants-armadillo` | 26.1 | 53.2 | 46.2 | (20.2) | (173) | **no** | main sale search found **0** listings (valued at 0); off-target sale 50 c from 7 listings (5 c to 3 div), so V and profit are not meaningful |
| `convoking-wand-spell-minion-levels` | 4623 | 10558 | 0 | -4623 | -23116 | **no** | sale search found **0** listings; each attempt costs about 377 c (Prime Chaotic Resonator 296 c), ~23.4 div per finished wand on average |

`rank_crafts` returned no items: every candidate was excluded (the reasons above, plus the
three `example-*` placeholders). That is the honest answer for this market on that date: the
exact flask pairs and the +1/+1 wand were barely or not at all listed in Covens, so there is no
sale price to rank on. Rerun `evaluate_recipe` later in the league; the searches are live.

## What to verify in game before promoting a recipe

1. The mods and tiers named in the notes exist with those names and item levels in 3.30
   (hover the item; check "Item Level", not "Requires Level").
2. The step order works as described (for the wand: the four fossils can be used together in a
   Prime Chaotic Resonator in 3.30; the guide is from 2024).
3. Open each trade URL in the recipe; the search should show items you would really buy (base)
   or sell against (the sale searches). Quality and enchants are not filtered.
4. Time one attempt and replace `minutes` with your own number.
5. Then set `status: verified` and `verified_patch: "3.30.0"` (quoted), per
   [recipes.md](recipes.md#verification).

## Cluster jewels

Four cluster jewel crafts, all with the same shape: buy a white jewel with the right enchant and
passive count, make it magic, alt-aug until an anchor notable appears, then use **one** Regal
Orb. A Regal that adds a partner notable gives the pair; a miss leaves a rare jewel with the
anchor but without a target partner (the Regal's mod can be anything else, even another
notable), which is sold too. Both end states are sale outcomes with their own trade search;
`salvage` is unused. Every search (base, pair, miss) asks for **instant-buyout** listings only
(trade status `securable`), never whisper listings.

| Recipe id | What you make | Guide (patch) |
| --- | --- | --- |
| `medium-cluster-chaos-dot-wicked-pall` | Medium, 12% Chaos DoT, 5 passives: Wicked Pall + Flow of Life, Brush with Death or Student of Decay | [Exile's Foundry](https://exilesfoundry.com/blog/poe-league-start-profit-crafting/) (3.30, M5 Chaos DoT template); pairs from [poeautocraft 3.28](https://poeautocraft.com/autocraft-strategy/3534f924-2610-4dda-8a4f-b29e2001b865/) and [3.29](https://poeautocraft.com/autocraft-strategy/4000-chaos-profit-the-old-school-cluster-crafting-strategy-in-poe-329/) (**pre-3.30**) |
| `medium-cluster-crit-malice-pressure-points` | Medium, 15% Crit Chance, 4 passives: Overwhelming Malice + Pressure Points | [poeautocraft](https://poeautocraft.com/autocraft-strategy/d15fda9a-bc1b-4ead-9124-cf2f364adf69/) (**pre-3.30**, 3.28) |
| `medium-cluster-minion-life-rebirth` | Medium, Minion Life, 4 passives: Life from Death + Blessed Rebirth | [poeautocraft](https://poeautocraft.com/autocraft-strategy/542bd7d9-da31-4f35-9009-f44e43bd4111/) (**pre-3.30**, 3.28) |
| `large-cluster-spell-mage-hunter` | Large, 10% Spell Damage, 8 passives: Mage Hunter + Conjured Wall or Arcane Heroism | [poeautocraft / JJ546](https://poeautocraft.com/blog/2026-11-19-poe-330-crafting-guide-large-spell-damage-clusters/) (3.30, Nov 2026) |

The Large recipe's targets come from the guide's downloadable template: it stops a magic jewel
on Arcane Heroism, Conjured Wall, Mage Hunter or a suffix notable, and its finished rares are
Mage Hunter + (Conjured Wall or Arcane Heroism) + a suffix notable (Essence Rush, Thaumophage or
Sap Psyche). The recipe anchors on Mage Hunter (in every finished target) and stops after the
Regal, at the two-notable jewel.

### Bases and item level

The enchant decides which notables can roll (RePoE `cluster_jewels.json`); every recipe step
passes `enchant` and `passives` to `mod_odds`. Item level matters in two ways: the notables need
68 (Mage Hunter, Overwhelming Malice, Blessed Rebirth, Flow of Life, Brush with Death), and at
**84+ the pool gains extra small-passive mods** (Medium Chaos DoT prefixes 11900 → 14000,
suffixes 10600 → 14150), which makes every notable rarer. The pool is identical from 68 to 83
for the Medium jewels and from 75 to 83 for the Large one, so the base searches take item level
68-83 (Medium) or 75-83 (Large, as the guide does) and the odds use ilvl 75.
`evaluate_recipe` warns that the base's real item level is unknown; within those ranges it does
not change the odds. Bases are white, not corrupted, with the exact enchant and passive count.
Both sale searches take **rare** jewels with the same enchant, passive count and item-level range
as the base, not corrupted, so magic crafting bases (for example ilvl 84 magic jewels with the
anchor) never price the miss. The pair searches require the anchor and a partner (an `and`
group, plus a `count` group of at least one partner where there are several); the miss searches
require the anchor and exclude every target partner (a `not` group, or for Crit and Minion a
`count` group of exactly one of the two notables).

### How the odds were derived

Everything comes from the RePoE 3.30.0.4.3 export through `mod_odds`; nothing is guessed.

- **Step 1 (alt_aug)** is a `p_source: weights` step, so the tool computes it on every
  evaluation. One cycle = an Alteration, plus an Augmentation when the Alteration gave a lone
  suffix (a lone non-anchor prefix gets the next Alteration instead; a lone anchor is not
  augmented). The Augmentation quantity per cycle is `augmentation / alteration` from
  `expected_materials`.
- **Branch rule.** When step 1 stops, the anchor is either alone (the Alteration gave just the
  anchor: chance `0.5 x w_anchor / w_all` per cycle) or with one suffix, drawn in proportion to
  suffix weight. The Regal's chance differs between the two because a suffix blocks its mod
  group, so both branches are modelled: `mod_odds regal` with `existing_mods: [anchor]`, and
  with `[anchor, suffix]` for every suffix in the pool, averaged by suffix weight. With two
  anchors (Crit, Minion) each anchor is weighted by its share of step-1 hits (proportional to
  its spawn weight).
- **Step 2 (one Regal)** is paid once (no `repeat_until`); its chance becomes the pair
  outcome's probability, and 1 minus it the one-notable outcome's.

| Recipe | Step 1 chance per cycle | Alts / Augs per anchor | Anchor alone | Regal chance (alone / with suffix) | Pair outcome |
| --- | ---: | ---: | ---: | --- | ---: |
| Chaos DoT, Wicked Pall | 0.02924 | 34.2 / 8.05 | 26.4% | 0.02745 / 0.02859 | 0.0282887 (1 in 35) |
| Crit, OM or PP | 0.05356 | 18.7 / 4.43 | 26.3% | PP held (72.8%): 0.00782 / 0.00815; OM held (27.2%): 0.0206 / 0.0215 | 0.0116569 (1 in 86) |
| Minion Life, LfD or BR | 0.08278 | 12.1 / 2.85 | 26.4% | LfD held (72.7%): 0.01235 / 0.01287; BR held (27.3%): 0.0322 / 0.0335 | 0.0183218 (1 in 55) |
| Large Spell, Mage Hunter | 0.02777 | 36.0 / 9.91 | 22.5% | 0.0464 / 0.0486 | 0.0481352 (1 in 21) |

The Regal hits rarely, so **most of each recipe's value is the miss sale**; the pair is
upside. The miss searches take the cheapest comparable rare jewel (for Crit and Minion, the
cheaper of the two notables; for Large, a Mage Hunter jewel without either partner, although a
Mage Hunter + suffix notable is worth more), so the miss values are conservative.

### Live snapshot (superseded)

**Pre-instant-buyout, superseded** by [Live ranking (instant buyout only)](#live-ranking-instant-buyout-only): taken with online (whisper) listings and the earlier
magic-or-rare, any-item-level miss searches.

`evaluate_recipe` for each recipe and `rank_crafts(budget_div=30, include_unverified=true,
tags=["cluster"])`, run 2026-11-24 04:09-04:11 UTC, league Covens, 1 divine = 197.7 chaos,
10 trade listings per search, 5% undercut, `min_listings` 10. All trade searches were fetched
live in this run (21 trade requests in total); `rank_crafts` reused them from the trade cache.
The `cluster` tag kept the ranking to these four recipes, so it did not re-query the flask and
wand searches. Materials from poe.ninja Currency: Alteration 0.2216 c, Augmentation 0.249 c,
Regal 0.3155 c, Transmutation 0.0406 c. Chaos values; C = expected cost of one attempt,
C90 = cost of a bad (90th percentile) run, V = expected value.

| Recipe | Base (p25, listings) | C | C90 | V | Profit | Profit/hr | One-notable sale (p25, listings) | Pair sale | Rankable |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `medium-cluster-chaos-dot-wicked-pall` | 1 c (10) | 10.9 | 23.2 | 5.3 | -5.6 | -68 | 5.75 c (4) | **0 online listings** | **no** |
| `medium-cluster-crit-malice-pressure-points` | 1 c (10) | 6.6 | 13.1 | 5.6 | -1.0 | -14 | 6 c (9) | **0 online listings** | **no** |
| `medium-cluster-minion-life-rebirth` | 1 c (10) | 4.7 | 8.9 | 9.1 | 4.4 | 65 | 9.75 c (8) | **0 online listings** | **no** |
| `large-cluster-spell-mage-hunter` | 3 c (6) | 13.8 | 27.1 | 78.6 | 64.8 | 778 | 86.9 c (**2**) | **0 online listings** | **no** |

`rank_crafts` returned no items: each recipe was excluded because its pair search had no usable
listings (the pair is valued at 0, so V and profit above count only the one-notable sale). The
searches themselves work: the Crit pair search, run once with offline sellers included, found
30 listings, so the market was simply thin at 04:10 UTC with online-only status. Every
one-notable search also had fewer than 10 online listings (the Large one on 2), so none of the
profits above is meaningful yet. poe.ninja's ClusterJewel overview at the same time priced these
white bases higher than the cheapest trade listings (for example 15 c for 5-passive Chaos DoT and 10 c for
4-passive Crit, ilvl 75-83, and 6.6 c for 8-passive Spell Damage), so the 1-3 c trade bases may be
too good to last. Rerun `evaluate_recipe` at a busier hour; the searches are live.

### Not modelled

- **Exalted Orb finishes.** The guides use an Exalted Orb when the Regal added a suffix and a
  prefix slot is still open (Medium), and to add the third notable (Large). A step that runs
  only on one branch cannot be expressed, so it is left out (upside the numbers ignore).
- **Scour and redo.** The guides scour a missed Regal and start again; these recipes sell the
  miss instead, which the tool can price.
- **Other anchors.** The guides stop on several notables (for Chaos DoT, any of the seven pairs:
  Eldritch Inspiration + Low Tolerance, Low Tolerance + Wasting Affliction, Brush with Death +
  Flow of Life, Brewed for Potency + Low Tolerance, and the Wicked Pall pairs). One recipe has
  one sale search per outcome, so each recipe models one anchor set.

### What to verify in game

1. The notable names, that they appear on the jewel with this enchant and passive count, and the
   item levels (68 for the notables named above; the jewel's "Item Level", not "Requires Level").
2. That a Medium jewel with 4 or 5 passives and a Large with 8 can hold the pairs (the guides say
   so; the trade searches find them).
3. Open each trade URL: the base search should show white jewels with the right enchant and
   passive count; the sale searches should show jewels you would sell against.
4. Time one attempt and replace `minutes`.
5. Then set `status: verified` and `verified_patch: "3.30.0"` (quoted).

## Researched but not added

- **Righteous Fire rings and gloves** ([pohx.net rings](https://pohx.net/crafts/rings/),
  [gloves](https://pohx.net/crafts/gloves/), updated 2026-11-11). Both start from a
  *fractured* base and spam essences. `mod_odds` has no input for a fractured mod, which takes
  an affix slot and blocks its mod group, so `essence:<name>` odds would overstate the chance
  of the other mods. The guides also aim at a combination of several mods ("life, chaos/fire
  res, dexterity"), while `mod_odds` gives the chance of at least one target, and the sale value
  depends on that whole combination. Modelling them honestly needs fractured-mod support in
  `mod_odds`.
- **Cluster jewel Exalted Orb finishes and other anchors**: see [Not modelled](#not-modelled)
  under Cluster jewels.
- **Harvest reforges, beastcrafting, Rog, Gwennen**: no published per-attempt odds, and
  `mod_odds` supports only chaos, alteration (alone or with Augmentation), Regal, essence and
  fossil methods.
- **Elder Stygian Vise**: influence mods are not modelled by `mod_odds`.
