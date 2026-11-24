# Decisions

Current project decisions that later work must respect. One line each; when a decision is
reversed, rewrite its line and note what it replaces.

## Project and workflow

- The repo is public at DaAwesomePwner/poe-craft-mcp under the MIT license. Why: public CI. (2026-11-23, v1 build, PR #1)
- Each milestone lands on main as a PR, merged once CI is green and its code review approves. Why: the owner wants reviewable milestones. (2026-11-23, v1 build, PR #1)
- Code is reviewed cross-model: Claude implements and Codex reviews (gpt-6.2-astra for medium/high-risk changes, gpt-6.2-sol for routine ones). Why: owner's choice. (2026-11-23, v1 build)
- Python ≥ 3.12; CI runs ubuntu and windows × Python 3.12 and 3.13. Why: design doc stack, Windows is the main user platform. (2026-11-23, v1 build, PR #1)

## Privacy and secrets

- The User-Agent contact defaults to the repo URL; a personal email goes only in the gitignored config.toml. Why: the repo is public. (2026-11-23, v1 build, PR #1)
- POESESSID is optional, read from the OS keychain then the POESESSID env var, never from config; the tool works without it on IP-based limits. Why: it grants full account access. (2026-11-23, v1 build, PR #2)

## Recipes

- The repo ships 3 unverified structural example recipes plus unverified starter crafts drafted by agents from public guides (real trade searches, odds from the game data, listed in docs/starter-recipes.md); only the user promotes a recipe to verified, after checking it in game. Why: the owner wants real crafts to evaluate out of the box, and 3.30 crafts can't be verified by agents. (2026-11-24, starter recipes, R1) Replaces: the repo ships only 3 unverified structural example recipes; real crafts are added by the user after checking them in game.
- `fixed_chaos` and `use_salvage` outcomes are failure values only; a successful sale must be priced by `trade_url`, and a recipe without one (or with its sale price overridden) is evaluated but never ranked. Why: never rank a craft on an asserted sale price with no live listings behind it. (2026-11-24, v1 build, phase 3)
- A recipe step repeats only when it has `repeat_until`; `p_success` without it is ignored with a warning and the step is paid once. Why: a one-shot step must not be costed as a repeated one. (2026-11-24, v1 build, phase 3)

## Profit engine

- Success probabilities below 1e-12 are outside the supported range and raise a clear error. Why: the n90 calculation stays exact with a wide float margin down to 1e-12, while far smaller values eventually make adjacent attempt counts indistinguishable; no real craft is that rare. (2026-11-24, v1 build, phase 3)
