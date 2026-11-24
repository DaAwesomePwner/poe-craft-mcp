# Cluster combos: the model

How `cluster_combo_survey` and `cluster_combos` decide which notable pairs on a Medium or Large
Cluster Jewel are worth crafting for, and how they price the craft. The workflow is in the
README under [Finding sellable cluster combos](../README.md#finding-sellable-cluster-combos).
Every listing used is an instant-buyout (asynchronous trade) listing; whisper listings and the
bulk exchange are never used.

## The craft: anchor cycle, then Regal

The craft is Transmutation, then Alteration (and Augmentation) until the magic jewel shows an
**anchor**, then one Regal Orb. An anchor is any notable that appears in at least one sellable
pair. The odds are exact: every draw is enumerated from the RePoE spawn weights of the
enchanted base (the enchant's tag joins the jewel's tags and picks the notable pool), nothing
is sampled.

One cycle, in words:

- The Alteration's first affix X1 is drawn from the whole pool in proportion to weight. An
  Alteration gives one or two affixes with equal chance (assumed).
- If X1 is an anchor: with a one-affix Alteration the cycle stops at {X1} and no Augmentation is
  used; with a two-affix Alteration the second affix X2 is drawn from the mods still allowed
  (no shared mod group, at most one prefix and one suffix) and the cycle stops at {X1, X2}.
- If X1 is not an anchor: the cycle succeeds only when X2 (the Alteration's second affix, or
  the Augmentation after a one-affix Alteration) is an anchor, stopping at {X1, X2}. An
  Augmentation is used only when the open side can still roll an anchor; otherwise the next
  Alteration is used instead (same success odds, fewer orbs).
- The chance one cycle succeeds is the sum over those paths. Expected Alterations per attempt
  are 1 / p_cycle and expected Augmentations are the Augmentations per cycle / p_cycle.
- Given that the cycle stopped, each magic jewel it can stop at has a known probability. The
  Regal then adds one affix drawn from the mods still allowed on a rare jewel (at most 2
  prefixes and 2 suffixes, no shared mod group, with the tags the existing mods add applied).
- The rare's notables are the magic jewel's notables plus the Regal's affix if it is one. The
  attempt is a **hit** when they include a sellable pair, credited to the most valuable pair
  held. Anything else is a **miss**, including a rare whose two notables are not a sellable
  pair.

`p_any` (`p_any_sellable` in `cluster_combos`, `p_any_provisional` in the survey) is the chance
per attempt of a hit. Another sellable pair does not always raise it. A pair that brings a new
notable adds an anchor, so the cycle stops sooner and costs fewer Alterations, but if that
anchor rarely completes a sellable pair, stopping on it can lower the chance per attempt. A
pair between two notables that are already anchors (A+C next to A+B and B+C) adds no anchor;
it only turns more of the rares you already make into hits. The survey therefore ranks by
`p_any_provisional` itself, and uses the pair count only to break ties.

## Discovery and confirmation

**Threshold.** T = `min_value_div` x poe.ninja chaos per Divine. Every sale search matches the
base type, rare, not corrupted, the enchant and exactly the given passive count, with no item
level filter.

**Discovery** is two broad searches per enchant over the notables that have their own trade
id: rares with two or more of them priced at most T (search A) and at least T (search B),
`discovery_sample` cheapest listings each (at most 100). Both trade bounds include T, so each
band then keeps only its own side, with prices converted at poe.ninja rates: search A counts
listings strictly below T and search B those at T or more. A listing exactly at T is never
"below" (each band reports the rest as `outside_band_listings`). Listings are grouped by the
notables they show. A listing showing any third notable the base can roll at any item level is
never counted as a pair listing. Each legal, priceable pair falls in one class:

| Class | Rule (below = price < T, at or above = price >= T) |
| --- | --- |
| `not_sellable` | `min_listings` or more listings below T |
| `candidate` | fewer than `min_listings` below T and at least one at or above T |
| `thin` | only below T, fewer than `min_listings` |
| `unlisted` | never seen |

**Provisional value (survey only).** A candidate's provisional value is the `min_listings`-th
cheapest listing at or above T, or the cheapest one when fewer are listed, less undercut (a
seller must undercut the cheapest competing listing). It is provisionally sellable when that
value is at least T. The survey feeds those values to the exact odds and reports
`p_any_provisional` and the hit value per attempt, with no miss value and no costs. It ranks
enchants by the unrounded `p_any_provisional`, then by the number of provisionally sellable
pairs, then by enchant id.

**Survey order.** Enchants whose two discovery searches are cached and fresh are surveyed first,
straight from the cached listings with no request. The rest are searched live, one band at a
time, until `max_seconds`, a rate limit or a stale reply (the trade site refused a new search
and an expired copy was served). The survey stops there with the retry hint, never uses the
stale copy, and lists that enchant and the rest as `pending`; calling again continues from the
cache. Enchants with a legacy tag, no trade option, no priceable pair or a notable without any
explicit trade id are skipped with a reason.

**Confirmation (`cluster_combos`).** Candidates are priced with their exact pair search, most
likely first (spawn-weight product). That search requires both notables and excludes every
other notable the base can roll at any item level. A pair is **sellable** when the p25 of its
cheapest listings, less the undercut, is at least T and it has at least `min_listings`
listings. Cached searches are reused first, then at most `max_pair_searches` live searches run
per call within `max_seconds`; candidates left over are reported as unconfirmed, and calling
again continues. The odds and economics count only the pairs confirmed sellable so far, so an
unconfirmed candidate is treated as a miss until it is confirmed. A copy served from an expired
cache (the trade site refused a new search) never confirms a pair.

## Miss value and the white base

A miss is valued as a rare with exactly one anchor notable and no other notable (a `count`
group of the anchors with min 1 and max 1, plus a `not` group of every other notable), at its
p25 less the undercut. When that search has no listings at all, a miss is worth 0; when its
listings cannot be priced, the expected value and profit are null.

The white base is a normal jewel with the enchant and passive count, item level `ilvl`..83
(`ilvl`..100 above 83), at its p25 without undercut. When the mod pool at the top of that range
differs from the pool at `ilvl`, a warning says so: a higher-level base changes the odds.

Cost per attempt = base + Transmutation + E[Alterations] x Alteration + E[Augmentations] x
Augmentation + Regal, at poe.ninja rates. Profit per hour uses `minutes_per_attempt`.

## Exclusions and the unidentifiable-notable guard

A pair, miss or discovery result must not count a jewel that carries a third notable. Exact
searches exclude, and discovery grouping rules out, every explicit trade id of every notable
the base can roll at `ilvl` **and** at item level 100, since sale searches accept any item
level. Shared or ambiguous ids are included in the exclusion even though such notables cannot
be priced on their own (their pairs are reported as unpriceable).

A notable with no explicit trade id at all cannot be excluded: a jewel carrying it would pass
any pair or miss search. When the enchant has one, `cluster_combos` runs discovery but confirms
and values nothing, and the survey skips the enchant. Refreshing the game data export usually
fixes it.

## Threshold and undercut

T is set in divines and converted at the poe.ninja Divine rate. Discovery price bounds use the
trade site's own chaos-equivalent conversion, while the bands are split at poe.ninja rates. A
listing priced very close to T in a currency the two convert differently can fall outside the
band of the search that returned it and be counted in neither band. Sale values subtract
`undercut_pct` (5% by default) to model listing below the competition; the white base and the
orbs are bought, so they have no undercut.

## Enchant trade option numbering

The trade filter `enchant.stat_3948993189` ("Added Small Passive Skills grant: #") picks the
enchant by an option number. The rule: an enchant's option is its 1-based position across every
`passive_skills` list in RePoE `cluster_jewels.json`, in file order (Large, then Medium, then
Small). The three attribute enchants (Strength, Dexterity, Intelligence; positions 51-53, told
apart by tag) have no option, so their jewels cannot be searched for and the survey skips them.

Evidence: the filter's options 1-55 in `https://www.pathofexile.com/api/trade/data/stats`
follow this order for the RePoE 3.30.0.4.3 export, checked on 2026-11-24. If a future export
reorders or adds enchants, the numbering must be checked again.

Enchants with a legacy tag (`old_do_not_use_`) come from older patches; the survey skips them.

## Known approximations

- An Alteration gives one or two affixes with equal chance; this is assumed, not from the game
  data.
- Tags added by an affix (`adds_tags`) apply only at the Regal step, not to the magic jewel's
  second affix.
- The added passive count does not change mod weights (the export has no such rule).
- Sale searches have no item level filter: a jewel that rolled the notables sells the same at
  any item level.
- Prices are the p25 of the cheapest listings (or, provisionally, one discovery listing), less
  an undercut. Listings can be stale, price-fixed or slow to sell; how long a sale takes is not
  modelled.
- Discovery fetches at most 100 listings per band, cheapest first. When a band is truncated, a
  pair may look scarcer below T than it is, and pairs listed only at higher prices are missed.
  The survey reports truncated bands per enchant.
- A rare with a sellable pair and a third notable (a Large jewel can hold three) is credited
  with its most valuable pair's price, although the pair searches exclude such jewels.
- A miss holding two notables that are not a sellable pair is valued like a one-anchor miss.
- Survey values are provisional: one discovery listing per pair, not the exact pair search.
  They can rank enchants but should not decide a craft; confirm with `cluster_combos`.
- The survey's discovery searches use T in chaos, so when the poe.ninja Divine rate changes the
  cached searches no longer match and are run again.
