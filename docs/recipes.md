# Writing recipes

One YAML file per craft in `recipes/` (or the folder in `POE_CRAFT_RECIPES`). Every file is
validated against [`recipe.schema.json`](../src/poe_craft_mcp/recipes/recipe.schema.json) plus a
few cross-checks when the server starts; an invalid file is skipped, logged and reported under
`load_errors` by `list_recipes`. Restart the server after editing. The three `example-*.yaml`
files in `recipes/` are unverified placeholders showing the structure.

Fastest start: paste [`scripts/import_recipe.md`](../scripts/import_recipe.md) and a guide into
Claude, review the draft, save it under `recipes/`.

## Fields

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | Lowercase letters, digits, `-`, `_`; unique across files |
| `name` | yes | Readable title |
| `tags` | no | For example `essence`, `fossil`, `alt`, `bench`, `base-flip`, `bulk`; used by the `tags` filter |
| `status` | yes | `verified`, `unverified` or `deprecated` |
| `verified_patch` | for `verified` | A **quoted** string such as `"3.30.0"`. Unquoted `3.30` is the number 3.3 in YAML |
| `sources` | no | Guide URLs |
| `requires` | no | League or atlas content needed, for example `harvest`, `gwennen` |
| `budget_div` | no | Rough cost of one attempt in divines; only used by the budget filter of `list_recipes` |
| `base` | yes | Starting item: `lookup` or `trade_url` (exactly one) |
| `steps` | yes | One or more steps, in order |
| `outcomes` | yes | End states of one full attempt; probabilities sum to 1 |
| `salvage` | yes | What a failed attempt is worth |
| `minutes` | yes | Hands-on minutes for one full attempt (greater than 0) |
| `notes` | no | Free text Claude can quote when walking through the craft |

### `base`

```yaml
base:
  lookup: { source: ninja, type: BaseType, name: "Astral Plate", ilvl_min: 84 }   # influence: "Shaper" optional
# or
base:
  trade_url: "https://www.pathofexile.com/trade/search/<league>/<id>"
```

### `steps`

```yaml
- action: essence_spam                # any short name
  materials:                          # priced per attempt
    - { source: ninja, category: Essence, name: "Deafening Essence of Greed", qty: 1 }   # or `id:` instead of `name:`
    - { source: fixed, chaos: 2, label: "Bench crafting fee" }                          # for things poe.ninja does not price
  repeat_until: "T1 life prefix"      # repeated step: needs p_success
  p_success: { p_source: guide, value: 0.1, source_url: "https://example.com" }
```

A repeated step costs `materials x 1/p` on average (and `x n90` on a bad run, for the `low`
risk filter). A step without `repeat_until` is paid once. `p_success` is one of:

| `p_source` | Fields | Use for |
| --- | --- | --- |
| `guide` | `value` (0 to 1, above 0), optional `source_url` | A number from a guide |
| `user` | `value` | Your own record |
| `weights` | `method`, `target_mods`, optional `base`, `ilvl`, `enchant`, `passives`, `existing_mods` | Computed like the `mod_odds` tool from RePoE spawn weights (needs `scripts/fetch_gamedata.py`). `method`: `chaos`, `alt`, `alt_aug`, `regal`, `essence:<essence>` or `fossil:<fossil>[,<fossil>...]`; `target_mods`: ids from the export, such as `IncreasedLife7` |

Chances below 1e-12 are unsupported.

#### Magic-item methods and cluster jewels

- `alt`: one Orb of Alteration per attempt (1 or 2 affixes, half each).
- `alt_aug`: one cycle is an Alteration plus, when it gave a single non-target affix, an
  Augmentation. Spending rule: the Augmentation is used only when the open affix side can still
  roll a target (a lone non-target prefix when every target is a prefix gets the next
  Alteration instead). Success odds do not depend on this rule; the currency counts do, and
  they are reported under it (also listed in the result's `assumptions`). The odds report
  `expected_materials` per success, e.g. `{alteration: 5.2, augmentation: 0.9}`. Step
  materials are per attempt (cycle), so list the Alteration with `qty: 1` and the Augmentation
  with `qty` = augmentation / alteration (Augmentations used per cycle).
- `regal`: the chance a Regal Orb on the magic item whose mods are `existing_mods` (mods.json
  ids, at most one prefix and one suffix) adds a target. `existing_mods` is the whole magic
  item, not only the notable: list its second affix too. Targets already present are not
  counted. Mod groups of the existing mods are
  blocked, the rare side limits apply (3 + 3 on items, 2 + 2 on jewels) and tags the existing
  mods add (`adds_tags`) count.
- Cluster jewels (`Small/Medium/Large Cluster Jewel`) need `enchant`: the "Added Small Passive
  Skills grant" enchant by name, id or text, e.g. `Chaos Damage over Time`, `Minion Life`,
  `Spell Damage`. Its tag (RePoE `cluster_jewels.json`) is what notables' spawn weights
  reference, so it decides which notables can roll. `passives` (added passive count) is
  optional and only checked against the jewel's range. Only `alt`, `alt_aug` and `regal` are
  modelled for cluster jewels.

A two-step cluster craft (notable A, then notable B). Step 1 can end in two states: notable
A alone (only when the Alteration gave one affix and it was A) or A plus a suffix (two-affix
Alteration, or a lone suffix then the Augmentation). The Regal's chance differs between them,
because the suffix blocks its mod group and takes a suffix slot. This example models the
branch "Wicked Pall + the T1 fire resistance suffix"; a recipe for the lone-notable branch
lists only `AfflictionNotableWickedPall_`. Write the branch you expect, and say which in
`notes`.

```yaml
- action: alt_aug
  repeat_until: "Wicked Pall"
  p_success: { p_source: weights, method: alt_aug, target_mods: [AfflictionNotableWickedPall_],
               enchant: "Chaos Damage over Time", ilvl: 84 }
- action: regal
  repeat_until: "Eternal Suffering"
  p_success: { p_source: weights, method: regal, target_mods: [AfflictionNotableEternalSuffering],
               enchant: "Chaos Damage over Time", ilvl: 84,
               existing_mods: [AfflictionNotableWickedPall_, AfflictionJewelSmallPassivesGrantFireRes] }
```

A missed Regal leaves a rare without notable B: it must be scoured (or sold) and step 1 redone.
`repeat_until` on the Regal step prices only the Regals, so account for the redo separately.

### `outcomes` and `salvage`

```yaml
outcomes:
  - { name: "good hit", probability: 0.25, trade_url: "https://www.pathofexile.com/trade/search/<league>/<id>" }
  - { name: "dud",      probability: 0.55, use_salvage: true }
  - { name: "brick",    probability: 0.20, fixed_chaos: 0 }
salvage:
  fixed_chaos: 1           # or `trade_url:` or `lookup: { source: ninja, type: ..., name: ... }`
```

Each outcome has exactly one of `trade_url` (priced from live listings), `fixed_chaos` or
`use_salvage: true`. Failure is an ordinary outcome: include it so the probabilities sum to 1.
`fixed_chaos` and `use_salvage` are failure values only: a successful sale must use `trade_url`,
and a recipe with no `trade_url` sale (or whose sale price you override) is evaluated but not ranked.
Value uses the 25th percentile of the listings minus the configured undercut.

## Trade URLs

Build the search for the *finished* item on pathofexile.com/trade, run it and copy the URL
(`.../trade/search/<league>/<id>`, or `.../trade/exchange/...`). Paste it **unchanged**: the
server needs the original league to look up a saved search, then swaps in the current league
when it runs it. Keep the filters tight enough that the cheapest listings
are the item you would really sell. A URL containing `<` (placeholder) cannot be priced, and
the recipe carries a warning until you replace it.

## Verification

A recipe is verified only with `status: verified` and `verified_patch` of `3.30` or later.
Verified recipes are ranked; everything else is hidden unless `include_unverified` is on, and
`deprecated` is never listed. Before promoting a draft:

1. Check every step, quantity and probability against a current (3.30+) source.
2. Replace every placeholder (`<mod id>`, `<league>`, `TODO`) and example URL.
3. Run `list_recipes` and `get_recipe`, then `evaluate_recipe`, and compare the cost and outcome
   prices with what you see in game.
4. Set `status: verified` and `verified_patch: "3.30.0"` (quoted).
