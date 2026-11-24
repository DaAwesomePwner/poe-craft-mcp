"""Load the parts of the RePoE-fork export that mod odds need."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from poe_craft_mcp.context import AppContext
from poe_craft_mcp.gamedata.models import (
    AFFIX_TYPES,
    FETCH_HINT,
    BaseItem,
    Essence,
    Fossil,
    GameData,
    GameDataError,
    ModDef,
    TagWeights,
)

log = logging.getLogger(__name__)

EXPORT_BASE_URL = "https://repoe-fork.github.io"
EXPORT_FILES = (
    "mods",
    "base_items",
    "essences",
    "fossils",
    "crafting_bench_options",
    "stat_translations",
)
REQUIRED_FILES = ("mods", "base_items", "essences", "fossils")
GAMEDATA_DIRNAME = "gamedata"
MANIFEST_NAME = "manifest.json"


def _tag_weights(raw: list[dict[str, Any]]) -> TagWeights:
    return tuple((str(w["tag"]), int(w["weight"])) for w in raw)


def _mod(mod_id: str, raw: dict[str, Any]) -> ModDef:
    return ModDef(
        id=mod_id,
        domain=raw["domain"],
        generation_type=raw["generation_type"],
        required_level=int(raw.get("required_level", 0)),
        groups=frozenset(raw.get("groups", ())),
        tags=frozenset(raw.get("implicit_tags", ())),
        spawn_weights=_tag_weights(raw.get("spawn_weights", [])),
        generation_weights=_tag_weights(raw.get("generation_weights", [])),
        is_essence_only=bool(raw.get("is_essence_only", False)),
        text=str(raw.get("text") or raw.get("name") or mod_id),
    )


def _fossil(fossil_id: str, raw: dict[str, Any]) -> Fossil:
    unmodelled = []
    if raw.get("corrupted_essence_chance"):
        unmodelled.append(f"{raw['corrupted_essence_chance']}% corrupted essence mod chance")
    if raw.get("mirrors"):
        unmodelled.append("creates a mirrored copy")
    return Fossil(
        id=fossil_id,
        name=raw["name"],
        weights=_tag_weights(raw["positive_mod_weights"] + raw["negative_mod_weights"]),
        added_mods=tuple(raw.get("added_mods", ())),
        forced_mods=tuple(raw.get("forced_mods", ())),
        allowed_tags=frozenset(raw.get("allowed_tags", ())),
        forbidden_tags=frozenset(raw.get("forbidden_tags", ())),
        unmodelled=tuple(unmodelled),
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GameDataError(f"cannot read game data file {path}: {exc}; {FETCH_HINT}") from exc


def load_gamedata(directory: Path) -> GameData:
    """Parse the export in `directory`. Raises GameDataError with a fetch hint if missing."""
    missing = [n for n in REQUIRED_FILES if not (directory / f"{n}.json").is_file()]
    if missing:
        raise GameDataError(
            f"game data export not found in {directory} (missing {', '.join(missing)}.json); "
            f"{FETCH_HINT}"
        )
    try:
        data = _parse(directory)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise GameDataError(
            f"game data export in {directory} has an unexpected format ({exc!r}); {FETCH_HINT}"
        ) from exc
    log.info(
        "loaded game data %s: %d affix mods, %d bases",
        data.game_version or "(unknown version)",
        len(data.mods),
        len(data.bases),
    )
    if data.stale_warning:
        log.warning(data.stale_warning)
    return data


def _parse(directory: Path) -> GameData:
    raw_fossils = _read_json(directory / "fossils.json")
    added = {
        m for f in raw_fossils.values() for m in f.get("added_mods", []) + f.get("forced_mods", [])
    }
    mods = {
        mod_id: _mod(mod_id, raw)
        for mod_id, raw in _read_json(directory / "mods.json").items()
        if raw["generation_type"] in AFFIX_TYPES or mod_id in added
    }
    bases = {
        base_id: BaseItem(
            base_id,
            raw["name"],
            raw["domain"],
            raw["item_class"],
            frozenset(raw["tags"]),
            raw.get("release_state", ""),
        )
        for base_id, raw in _read_json(directory / "base_items.json").items()
        if raw.get("name")
    }
    essences = {
        ess_id: Essence(ess_id, raw["name"], dict(raw["mods"]), raw.get("item_level_restriction"))
        for ess_id, raw in _read_json(directory / "essences.json").items()
    }
    fossils = {fid: _fossil(fid, raw) for fid, raw in raw_fossils.items() if raw.get("name")}
    manifest_path = directory / MANIFEST_NAME
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    downloaded = manifest.get("downloaded_at")
    return GameData(
        mods=mods,
        bases=bases,
        essences=essences,
        fossils=fossils,
        game_version=manifest.get("game_version"),
        downloaded_at=datetime.fromisoformat(downloaded) if downloaded else None,
        source_url=manifest.get("source", EXPORT_BASE_URL),
    )


def gamedata_dir(ctx: AppContext) -> Path:
    return ctx.config.data_dir / GAMEDATA_DIRNAME


def get_gamedata(ctx: AppContext) -> GameData:
    """The shared GameData, loaded on first use. Failures are not cached, so a retry after
    running the fetch script succeeds without restarting the server."""
    return ctx.service("gamedata", lambda c: load_gamedata(gamedata_dir(c)))
