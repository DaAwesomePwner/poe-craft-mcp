"""Load the parts of the RePoE-fork export that mod odds need."""

from __future__ import annotations

import hashlib
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
AFFIX_LIMIT_STATS = ("local_maximum_prefixes_allowed_+", "local_maximum_suffixes_allowed_+")


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
    if raw.get("rolls_lucky"):
        unmodelled.append("lucky values and more common high-level mods (no exported weight rule)")
    if raw.get("corrupted_essence_chance"):
        unmodelled.append(f"{raw['corrupted_essence_chance']}% corrupted essence mod chance")
    if raw.get("mirrors"):
        unmodelled.append("fractures a random modifier")
    if raw.get("sell_price_mods"):
        unmodelled.append("adds vendor sell-price mods")
    weights = _tag_weights(raw["positive_mod_weights"] + raw["negative_mod_weights"])
    added, forced = tuple(raw.get("added_mods", ())), tuple(raw.get("forced_mods", ()))
    if not (weights or added or forced or unmodelled):
        descriptions = raw.get("descriptions", {}).values()
        text = "; ".join(" ".join(str(d).split()) for d in descriptions)
        unmodelled.append(f"effect not expressed as mod weights ({text or 'undescribed'})")
    return Fossil(
        id=fossil_id,
        name=raw["name"],
        weights=weights,
        added_mods=added,
        forced_mods=forced,
        allowed_tags=frozenset(raw.get("allowed_tags", ())),
        forbidden_tags=frozenset(raw.get("forbidden_tags", ())),
        unmodelled=tuple(unmodelled),
    )


def _affix_deltas(implicits: list[str], raw_mods: dict[str, Any]) -> tuple[int, int]:
    deltas = {stat: 0 for stat in AFFIX_LIMIT_STATS}
    for implicit in implicits:
        for stat in raw_mods.get(implicit, {}).get("stats", []):
            if stat["id"] in deltas:
                deltas[stat["id"]] += int(stat["min"])
    prefix_stat, suffix_stat = AFFIX_LIMIT_STATS
    return deltas[prefix_stat], deltas[suffix_stat]


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise GameDataError(f"cannot read game data file {path}: {exc}; {FETCH_HINT}") from exc


def _verify(name: str, content: bytes, manifest: dict[str, Any]) -> None:
    """With a fetch-script manifest, every required file must match its recorded sha256."""
    if "files" not in manifest:
        return
    expected = manifest["files"].get(name, {}).get("sha256")
    if expected != hashlib.sha256(content).hexdigest():
        raise GameDataError(
            f"game data file {name}.json does not match manifest.json (incomplete or mixed "
            f"download); {FETCH_HINT}"
        )


def _read_json(path: Path, manifest: dict[str, Any] | None = None) -> Any:
    content = _read_bytes(path)
    if manifest is not None:
        _verify(path.stem, content, manifest)
    try:
        return json.loads(content)
    except ValueError as exc:
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
    manifest_path = directory / MANIFEST_NAME
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    raw_mods = _read_json(directory / "mods.json", manifest)
    raw_fossils = _read_json(directory / "fossils.json", manifest)
    added = {
        m for f in raw_fossils.values() for m in f.get("added_mods", []) + f.get("forced_mods", [])
    }
    mods = {
        mod_id: _mod(mod_id, raw)
        for mod_id, raw in raw_mods.items()
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
            *_affix_deltas(raw.get("implicits", []), raw_mods),
        )
        for base_id, raw in _read_json(directory / "base_items.json", manifest).items()
        if raw.get("name")
    }
    essences = {
        ess_id: Essence(ess_id, raw["name"], dict(raw["mods"]), raw.get("item_level_restriction"))
        for ess_id, raw in _read_json(directory / "essences.json", manifest).items()
    }
    fossils = {fid: _fossil(fid, raw) for fid, raw in raw_fossils.items() if raw.get("name")}
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
