# Starter recipes

Three crafts taken from public guides and written as recipe files, so `rank_crafts` has real
crafts to evaluate. All three are **unverified**: nobody has checked the steps in game, and
they stay hidden from rankings unless you pass `include_unverified=true`. The `notes` of each
recipe walk through the craft step by step; `get_recipe <id>` shows them.

| Recipe id | What you make | Guide (patch) |
| --- | --- | --- |
| `quicksilver-experimenters-cheetah` | Magic Quicksilver Flask, Experimenter's (36-40% duration) + of the Cheetah (12-14% movement speed) | [Exile's Foundry](https://exilesfoundry.com/blog/poe-league-start-profit-crafting/) (3.30, Nov 2026); method from the [PoE Vault flask guide](https://poe-vault.com/guides/ultimate-flask-crafting-guide) |
| `granite-flagellants-armadillo` | Magic Granite Flask, Flagellant's (3 charges when hit) + of the Armadillo (56-60% armour) | same |
| `convoking-wand-spell-minion-levels` | Rare Convoking Wand with +1 spell and +1 minion gem levels | [PoE Vault](https://poe-vault.com/guides/crafting-2-generic-minion-wand) (**pre-3.30**, July 2024) |

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
  weight). That is the sale outcome's probability; the other 0.9736842 is a flask with the
  right suffix and another prefix, priced by a salvage trade search. The Augmentation quantity
  (0.4226 and 0.4215) is the share of hits where the suffix came alone. The guide's own route
  (keep alt+aug spamming until both mods are on one flask) is about 1 in 2366 Alterations for
  the Quicksilver and 1 in 3103 for the Granite; it is described in the notes but not modelled.
- **Wand.** `mod_odds` with Corroded + Jagged + Shuddering + Metallic Fossils on an ilvl 84
  Convoking Wand gives P(+1 spell) = 0.1102, P(+1 minion) = 0.3211 and P(either) = 0.3497
  (Monte Carlo, 20000 samples, one seed). Both together = 0.1102 + 0.3211 - 0.3497 = 0.0816
  per resonator, recorded as `p_source: user` because `mod_odds` reports "at least one target"
  only. The guide's "around 10-12 tries" agrees (1 / 0.0816 = 12.3).

Minutes are estimates (clicks per attempt times a few seconds, plus buying and listing); each
recipe's notes give the basis.

## Live snapshot

`evaluate_recipe` for each recipe and `rank_crafts(budget_div=30, include_unverified=true)`,
run 2026-11-24 03:46 UTC, league Covens, 1 divine = 197.7 chaos, 10 trade listings per search,
5% undercut, `min_listings` 10. Chaos values; C = expected cost of one attempt, C90 = cost of a
bad (90th percentile) run, V = expected value.

| Recipe | C | C90 | V | Profit | Profit/hr | Rankable | Key warnings |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `quicksilver-experimenters-cheetah` | 33.6 | 54.2 | 15.7 | -17.9 | -179 | yes, but excluded by `rank_crafts` | sale search had **1** listing (1 div): illiquid (< 10); white ilvl 85 base costs 17.5 c (17 listed) |
| `granite-flagellants-armadillo` | 26.1 | 53.2 | 84.6 | (58.6) | (502) | **no** | sale search found **0** listings (valued at 0); salvage price from only 6 usable listings (4 c to 3 div), so V and profit are not meaningful |
| `convoking-wand-spell-minion-levels` | 4623 | 10557 | 0 | -4623 | -23115 | **no** | sale search found **0** listings; each attempt costs about 377 c (Prime Chaotic Resonator 296 c) so the craft costs ~23.4 div on average |

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
   or sell against (sale, salvage). Quality and enchants are not filtered.
4. Time one attempt and replace `minutes` with your own number.
5. Then set `status: verified` and `verified_patch: "3.30.0"` (quoted), per
   [recipes.md](recipes.md#verification).

## Researched but not added

- **Righteous Fire rings and gloves** ([pohx.net rings](https://pohx.net/crafts/rings/),
  [gloves](https://pohx.net/crafts/gloves/), updated 2026-11-11). Both start from a
  *fractured* base and spam essences. `mod_odds` has no input for a fractured mod, which takes
  an affix slot and blocks its mod group, so `essence:<name>` odds would overstate the chance
  of the other mods. The guides also aim at a combination of several mods ("life, chaos/fire
  res, dexterity"), while `mod_odds` gives the chance of at least one target, and the sale value
  depends on that whole combination. Modelling them honestly needs fractured-mod support in
  `mod_odds`.
- **Cluster jewels**: Alteration odds are not modelled for cluster jewels (domain
  `affliction_jewel`).
- **Harvest reforges, beastcrafting, Rog, Gwennen**: no published per-attempt odds, and
  `mod_odds` supports only chaos, alteration, essence and fossil methods.
- **Elder Stygian Vise**: influence mods are not modelled by `mod_odds`.
