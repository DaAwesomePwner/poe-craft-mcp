"""Parse a `mod_odds` method string into the rarity model, fossils and forced mods it implies."""

from __future__ import annotations

from dataclasses import dataclass, replace

from poe_craft_mcp.gamedata.draws import MAGIC, RARE, CountModel
from poe_craft_mcp.gamedata.lookup import resolve_essence, resolve_fossils
from poe_craft_mcp.gamedata.models import BaseItem, Fossil, GameData, GameDataError, ModDef

RARE_DOMAINS = frozenset({"item"})
MAGIC_DOMAINS = frozenset({"item", "flask", "jewel", "abyss_jewel"})
METHOD_HELP = "use 'chaos', 'alt', 'essence:<essence>' or 'fossil:<fossil>[,<fossil>...]'"

RARE_COUNT = (
    "Rare items get 4 affixes (8/12), 5 (3/12) or 6 (1/12), at most 3 prefixes and 3 suffixes "
    "unless the base's implicit changes those limits; this is the community estimate, not "
    "exported data"
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
    ilvl_cap: int | None = None
    warnings: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


def _forced_mod(data: GameData, mod_id: str, owner: str) -> ModDef:
    mod = data.mods.get(mod_id)
    if mod is None:
        raise GameDataError(f"{owner} forces mod {mod_id!r}, which is not in the export")
    return mod


def _check_domain(base: BaseItem, model: CountModel, label: str) -> None:
    domains = RARE_DOMAINS if model.name == "rare" else MAGIC_DOMAINS
    if base.domain not in domains:
        raise GameDataError(
            f"method {label!r} is not modelled for {base.name} (domain {base.domain!r}); "
            f"{model.name} items are supported for domains: {', '.join(sorted(domains))}"
        )


def _apply_affix_limits(base: BaseItem, setup: CraftSetup) -> CraftSetup:
    """Apply implicit prefix/suffix limits (e.g. Helical Ring) to the rare model."""
    if not (base.prefix_delta or base.suffix_delta):
        return setup
    if setup.model.name != "rare":
        raise GameDataError(
            f"method {setup.label!r} is not modelled for {base.name}: its implicit changes the "
            f"allowed prefixes/suffixes ({base.prefix_delta:+d}/{base.suffix_delta:+d}) and the "
            "export gives no rule for how that affects magic items"
        )
    max_prefix = max(0, RARE.max_prefix + base.prefix_delta)
    max_suffix = max(0, RARE.max_suffix + base.suffix_delta)
    model = replace(setup.model, max_prefix=max_prefix, max_suffix=max_suffix)
    warning = (
        f"{base.name} allows {max_prefix} prefix(es) and {max_suffix} suffix(es) (implicit); "
        "the 4-6 affix count is assumed unchanged and truncated to the slots that fit, which "
        "is not confirmed for affix-limited bases"
    )
    return replace(setup, model=model, warnings=(*setup.warnings, warning))


def _essence(data: GameData, base: BaseItem, query: str) -> CraftSetup:
    essence = resolve_essence(data, query)
    mod_id = essence.mods.get(base.item_class)
    if mod_id is None:
        classes = ", ".join(sorted(essence.mods))
        raise GameDataError(
            f"{essence.name} has no mod for item class {base.item_class!r} (has: {classes})"
        )
    cap = essence.item_level_restriction
    assumptions = [
        f"{essence.name} forces {mod_id}; it takes one of the rare's affix slots and blocks its "
        "mod group, and the remaining affixes roll from the normal pool"
    ]
    if cap is not None:
        assumptions.append(
            f"{essence.name} limits the item level to {cap} when selecting the other mods "
            "(export field item_level_restriction)"
        )
    return CraftSetup(
        label=f"essence:{essence.name}",
        model=RARE,
        forced=(_forced_mod(data, mod_id, essence.name),),
        ilvl_cap=cap,
        assumptions=tuple(assumptions),
    )


def _fossils(data: GameData, base: BaseItem, query: str) -> CraftSetup:
    fossils = resolve_fossils(data, [q for q in query.split(",") if q.strip()])
    forced: list[ModDef] = []
    for fossil in fossils:
        if fossil.unmodelled:
            raise GameDataError(
                f"{fossil.name} is not modelled: {'; '.join(fossil.unmodelled)}. Only fossils "
                "whose effect is mod weight modifiers, added or forced mods are supported"
            )
        if fossil.allowed_tags and not fossil.allowed_tags & base.tags:
            raise GameDataError(f"{fossil.name} cannot be used on {base.name}")
        if fossil.forbidden_tags & base.tags:
            raise GameDataError(f"{fossil.name} cannot be used on {base.name}")
        forced.extend(_forced_mod(data, m, fossil.name) for m in fossil.forced_mods)
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
        assumptions=assumptions,
    )


def parse_method(data: GameData, base: BaseItem, method: str) -> CraftSetup:
    kind, _, arg = method.strip().partition(":")
    kind = kind.strip().lower()
    if kind == "chaos" and not arg:
        setup = CraftSetup(label="chaos", model=RARE)
    elif kind in ("alt", "alteration") and not arg:
        setup = CraftSetup(label="alt", model=MAGIC)
    elif kind == "essence" and arg.strip():
        setup = _essence(data, base, arg.strip())
    elif kind in ("fossil", "fossils") and arg.strip():
        setup = _fossils(data, base, arg)
    else:
        raise GameDataError(f"unknown method {method!r}; {METHOD_HELP}")
    _check_domain(base, setup.model, setup.label)
    setup = _apply_affix_limits(base, setup)
    count = RARE_COUNT if setup.model.name == "rare" else MAGIC_COUNT
    return replace(setup, assumptions=(count, *setup.assumptions))
