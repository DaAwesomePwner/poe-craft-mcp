"""Parse a `mod_odds` method string into the rarity model, fossils and forced mods it implies."""

from __future__ import annotations

from dataclasses import dataclass

from poe_craft_mcp.gamedata.draws import MAGIC, RARE, CountModel
from poe_craft_mcp.gamedata.lookup import resolve_essence, resolve_fossils
from poe_craft_mcp.gamedata.models import BaseItem, Fossil, GameData, GameDataError, ModDef

RARE_DOMAINS = frozenset({"item"})
MAGIC_DOMAINS = frozenset({"item", "flask", "jewel", "abyss_jewel"})
METHOD_HELP = "use 'chaos', 'alt', 'essence:<essence>' or 'fossil:<fossil>[,<fossil>...]'"

RARE_COUNT = (
    "Rare items get 4 affixes (8/12), 5 (3/12) or 6 (1/12), at most 3 prefixes and 3 suffixes; "
    "this is the community estimate, not exported data"
)
MAGIC_COUNT = (
    "Alteration gives 1 or 2 affixes with equal chance (assumed), at most 1 prefix and 1 suffix"
)


@dataclass(frozen=True)
class CraftSetup:
    label: str
    model: CountModel
    fossils: tuple[Fossil, ...] = ()
    forced: tuple[ModDef, ...] = ()
    warnings: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


def _forced_mod(data: GameData, mod_id: str, owner: str) -> ModDef:
    mod = data.mods.get(mod_id)
    if mod is None:
        raise GameDataError(f"{owner} forces mod {mod_id!r}, which is not in the export")
    return mod


def _check_domain(base: BaseItem, model: CountModel, label: str) -> None:
    domains = RARE_DOMAINS if model is RARE else MAGIC_DOMAINS
    if base.domain not in domains:
        raise GameDataError(
            f"method {label!r} is not modelled for {base.name} (domain {base.domain!r}); "
            f"{model.name} items are supported for domains: {', '.join(sorted(domains))}"
        )


def _essence(data: GameData, base: BaseItem, ilvl: int, query: str) -> CraftSetup:
    essence = resolve_essence(data, query)
    mod_id = essence.mods.get(base.item_class)
    if mod_id is None:
        classes = ", ".join(sorted(essence.mods))
        raise GameDataError(
            f"{essence.name} has no mod for item class {base.item_class!r} (has: {classes})"
        )
    warnings = []
    limit = essence.item_level_restriction
    if limit is not None and ilvl > limit:
        warnings.append(
            f"export lists item_level_restriction={limit} for {essence.name}; item level {ilvl} "
            "is above it, so the game may not accept this essence on this item"
        )
    assumption = (
        f"{essence.name} forces {mod_id}; it takes one of the rare's affix slots and blocks its "
        "mod group, and the remaining affixes roll from the normal pool"
    )
    return CraftSetup(
        label=f"essence:{essence.name}",
        model=RARE,
        forced=(_forced_mod(data, mod_id, essence.name),),
        warnings=tuple(warnings),
        assumptions=(assumption,),
    )


def _fossils(data: GameData, base: BaseItem, query: str) -> CraftSetup:
    fossils = resolve_fossils(data, [q for q in query.split(",") if q.strip()])
    warnings: list[str] = []
    forced: list[ModDef] = []
    for fossil in fossils:
        if fossil.allowed_tags and not fossil.allowed_tags & base.tags:
            raise GameDataError(f"{fossil.name} cannot be used on {base.name}")
        if fossil.forbidden_tags & base.tags:
            raise GameDataError(f"{fossil.name} cannot be used on {base.name}")
        forced.extend(_forced_mod(data, m, fossil.name) for m in fossil.forced_mods)
        warnings.extend(f"{fossil.name}: {what} is not modelled" for what in fossil.unmodelled)
    assumptions = (
        "Fossil weight modifiers multiply a mod's weight by value/100 for every modifier tag the "
        "mod has (several fossils multiply together); a 0 modifier removes the mod",
        "Mods a fossil adds (added_mods) join the pool with their own spawn weights for the base",
    )
    return CraftSetup(
        label="fossil:" + ",".join(f.name for f in fossils),
        model=RARE,
        fossils=tuple(fossils),
        forced=tuple(forced),
        warnings=tuple(warnings),
        assumptions=assumptions,
    )


def parse_method(data: GameData, base: BaseItem, ilvl: int, method: str) -> CraftSetup:
    kind, _, arg = method.strip().partition(":")
    kind = kind.strip().lower()
    if kind == "chaos" and not arg:
        setup = CraftSetup(label="chaos", model=RARE)
    elif kind in ("alt", "alteration") and not arg:
        setup = CraftSetup(label="alt", model=MAGIC)
    elif kind == "essence" and arg.strip():
        setup = _essence(data, base, ilvl, arg.strip())
    elif kind in ("fossil", "fossils") and arg.strip():
        setup = _fossils(data, base, arg)
    else:
        raise GameDataError(f"unknown method {method!r}; {METHOD_HELP}")
    _check_domain(base, setup.model, setup.label)
    count = RARE_COUNT if setup.model is RARE else MAGIC_COUNT
    return CraftSetup(
        setup.label,
        setup.model,
        setup.fossils,
        setup.forced,
        setup.warnings,
        (count, *setup.assumptions),
    )
