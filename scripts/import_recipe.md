# Import a crafting guide as a draft recipe

Paste everything below the line into Claude, then paste the guide's text (or its main steps and
numbers) after it. Claude answers with one YAML file. Save it as `recipes/<id>.yaml`, work
through the checklist at the end and restart the server (or the Claude app) to load it.

The draft is always `status: unverified`, so it stays hidden from rankings until you promote it.

---

You turn a Path of Exile 1 crafting guide into ONE draft recipe file for the poe-craft-mcp
server. Use only what the guide says. Never invent numbers, mod ids, item names or URLs. When
something is missing, write an obvious placeholder (starting with `TODO`) and list it in the
checklist.

## Output

Reply with one YAML code block, then the checklist below it. Nothing else.

The YAML must validate against `src/poe_craft_mcp/recipes/recipe.schema.json`:

- Top-level keys only: `id`, `name`, `tags`, `status`, `verified_patch`, `sources`, `requires`,
  `budget_div`, `base`, `steps`, `outcomes`, `salvage`, `minutes`, `notes`. No other keys.
- `id`: lowercase letters, digits, `-` or `_`, starting with a letter or digit (for example
  `essence-spam-life-chest`). `name`: a short readable title.
- `tags`: a few lowercase words, for example `essence`, `fossil`, `harvest`, `bench`,
  `base-flip`, `bulk`.
- `status: unverified` and `verified_patch: null`. ALWAYS, even if the guide says it is current.
  Only the user may change these after checking the craft.
- `sources`: the guide's URL (and author/video title in `notes` if there is no URL). If the user
  gave no URL, use `["TODO: guide URL"]`.
- `requires`: league or atlas content the craft needs (for example `harvest`, `bestiary`,
  `gwennen`), or `[]`.
- `budget_div`: rough cost of one attempt in divines, only if the guide gives enough to say;
  otherwise omit it.
- `base`: exactly one of
  - `lookup: { source: ninja, type: BaseType, name: "<base type>", ilvl_min: <n> }`
    (`influence: "<Shaper|Elder|...>"` is optional), or
  - `trade_url: "<saved trade search URL>"`.
- `steps`: in order, each with `action` (short snake_case name such as `essence_spam`,
  `alt_spam`, `fossil_craft`, `bench_craft`, `exalt_slam`) and optional:
  - `materials`: a list of either
    `{ source: ninja, category: <Currency|Essence|Fossil|Resonator|Oil|Fragment|...>, name: "<exact item name>", qty: <number per attempt> }`
    or `{ source: fixed, chaos: <number>, label: "<what it is>" }` for things poe.ninja does
    not price.
  - `repeat_until`: plain text for the stop condition. A step with `repeat_until` MUST also
    have `p_success`.
  - `p_success`: the chance one repetition succeeds. Pick one form:
    - `{ p_source: guide, value: <0-1>, source_url: "<url>" }` when the guide states the odds
      or gives numbers you can turn into odds (show the arithmetic in `notes`);
    - `{ p_source: weights, method: "<chaos|alt|essence:<name>|fossil:<a>[,<b>...]>", target_mods: ["<mods.json id>"], base: "<base type>", ilvl: <n> }`
      only when you know the exact mod ids, otherwise use the placeholder `"<mod id>"` and
      flag it;
    - `{ p_source: user, value: <0-1> }` when the user told you their own number.
  Do not put a probability on a step you cannot attribute to the guide; leave it out and
  say so.
- `outcomes`: every possible end state of ONE full attempt, failure included. Each has `name`,
  `probability` (greater than 0, at most 1) and exactly one of `trade_url`, `fixed_chaos`
  (use `0` for a total loss) or `use_salvage: true`. The probabilities MUST sum to 1: if the
  guide does not give them, put your best split from its wording, mark it in the checklist
  and normalise it so it sums to exactly 1. Always include at least one failure outcome
  (usually `use_salvage: true` or `fixed_chaos`).
- Use a trade URL placeholder like
  `"https://www.pathofexile.com/trade/search/<league>/TODO-search-id"` for outcomes that need
  pricing. The user will replace it with a saved search.
- `salvage`: exactly one of `fixed_chaos: <n>`, `lookup: { source: ninja, type: ..., name: ... }`
  or `trade_url`. It is what a failed attempt is worth.
- `minutes`: hands-on minutes for one full attempt (a number greater than 0). If the guide
  does not say, estimate and flag it.
- `notes`: the steps in plain words, as the guide explains them, so Claude can walk the user
  through them. Include warnings (for example things the guide says changed with a patch).

YAML rules: quote strings that contain `:`, `#`, or start with a special character. Item names
in double quotes. Never write `verified_patch` as a bare number.

## Checklist (after the YAML)

Print this list, tick each line `[x]` or `[ ]` for what you could fill from the guide, and
name every placeholder:

1. [ ] Guide is for the current patch (3.30 or later); the guide's patch/date: `<...>`
2. [ ] Base item and `ilvl_min` match the guide
3. [ ] Every material name is the exact in-game / poe.ninja name and the quantity is per attempt
4. [ ] Every `p_success` has a real source (guide number, mod weights or your own data)
5. [ ] Outcome probabilities are realistic and sum to 1; failure outcome included
6. [ ] Every `TODO` trade URL replaced with a saved search from pathofexile.com/trade
7. [ ] `salvage` is what a failed item really sells for
8. [ ] `minutes` is your real time for one attempt
9. [ ] Placeholders I used: `<list each one with its field path>`
10. [ ] Assumptions I made that the guide did not state: `<list>`

## The user's guide follows

(The user pastes the guide text after this line.)
