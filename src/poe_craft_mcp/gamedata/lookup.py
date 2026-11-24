"""Resolve user-supplied names (bases, mods, essences, fossils) with close-match hints."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping

from poe_craft_mcp.gamedata.models import BaseItem, Essence, Fossil, GameData, GameDataError, ModDef


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _hint(query: str, choices: Iterable[str]) -> str:
    close = difflib.get_close_matches(query, sorted(set(choices)), n=5, cutoff=0.5)
    return f" Closest: {', '.join(close)}." if close else ""


def resolve_base(data: GameData, query: str) -> tuple[BaseItem, list[str]]:
    """Find a base by name (case-insensitive) or metadata id; prefer released bases."""
    warnings: list[str] = []
    if query in data.bases:
        return data.bases[query], warnings
    key = normalize(query)
    hits = [b for b in data.bases.values() if normalize(b.name) == key]
    if not hits:
        names = [b.name for b in data.bases.values() if b.release_state == "released"]
        raise GameDataError(f"unknown base {query!r}.{_hint(query, names)}")
    hits.sort(key=lambda b: (b.release_state != "released", b.id))
    chosen = hits[0]
    if len({(b.tags, b.domain, b.item_class) for b in hits}) > 1:
        warnings.append(
            f"{len(hits)} base items are named {chosen.name!r} with different tags; used "
            f"{chosen.id} (pass a metadata id to choose another)"
        )
    return chosen, warnings


def resolve_mods(data: GameData, mod_ids: Iterable[str]) -> list[ModDef]:
    found: list[ModDef] = []
    for mod_id in mod_ids:
        mod = data.mods.get(mod_id)
        if mod is None:
            raise GameDataError(
                f"unknown mod id {mod_id!r} (use mods.json ids such as 'IncreasedLife7')."
                f"{_hint(mod_id, data.mods)}"
            )
        found.append(mod)
    return found


def _resolve_named[T: (Essence, Fossil)](
    table: Mapping[str, T], query: str, kind: str, strip: str = ""
) -> T:
    if query in table:
        return table[query]
    key = normalize(query).removesuffix(strip)
    for item_id, item in table.items():
        short_id = normalize(item_id.rsplit("/", 1)[-1])
        bare_id = short_id.removeprefix("currencyessence").removeprefix("currencydelvecrafting")
        if key in (normalize(item.name).removesuffix(strip), short_id, bare_id):
            return item
    names = [item.name for item in table.values()]
    raise GameDataError(f"unknown {kind} {query!r}.{_hint(query, names)}")


def resolve_essence(data: GameData, query: str) -> Essence:
    return _resolve_named(data.essences, query, "essence")


def resolve_fossils(data: GameData, queries: Iterable[str]) -> list[Fossil]:
    fossils = [_resolve_named(data.fossils, q.strip(), "fossil", "fossil") for q in queries]
    if not fossils or len(fossils) > 4:
        raise GameDataError("fossil method needs 1 to 4 fossils, e.g. 'fossil:pristine,scorched'")
    if len({f.id for f in fossils}) != len(fossils):
        raise GameDataError("each fossil can be used only once per resonator")
    return fossils
