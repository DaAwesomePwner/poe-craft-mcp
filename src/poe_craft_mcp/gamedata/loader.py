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
    GameData,
    GameDataError,
)
from poe_craft_mcp.gamedata.publish import lock_path
from poe_craft_mcp.gamedata.records import (
    affix_deltas,
    parse_clusters,
    parse_fossil,
    parse_mod,
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
    "cluster_jewels",
)
REQUIRED_FILES = ("mods", "base_items", "essences", "fossils")
GAMEDATA_DIRNAME = "gamedata"
MANIFEST_NAME = "manifest.json"


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


def _read_optional(directory: Path, name: str, manifest: dict[str, Any]) -> Any:
    """A file only some odds need (cluster_jewels), or None when absent or not part of the
    manifest's download (an export fetched before the file was added)."""
    path = directory / f"{name}.json"
    if not path.is_file() or ("files" in manifest and name not in manifest["files"]):
        return None
    return _read_json(path, manifest)


def load_gamedata(directory: Path) -> GameData:
    """Parse the export in `directory`. Raises GameDataError with a fetch hint if missing.

    Read-only: it never moves directories. Recovery from an interrupted fetch is done by the
    fetch script, which holds the fetch lock."""
    if lock_path(directory).exists():
        raise GameDataError(
            f"a game data fetch is in progress or was interrupted ({lock_path(directory)} "
            "exists); wait for it to finish, or if none is running delete that file and re-run "
            "`uv run python scripts/fetch_gamedata.py`"
        )
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
        mod_id: parse_mod(mod_id, raw)
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
            *affix_deltas(raw.get("implicits", []), raw_mods),
        )
        for base_id, raw in _read_json(directory / "base_items.json", manifest).items()
        if raw.get("name")
    }
    essences = {
        ess_id: Essence(ess_id, raw["name"], dict(raw["mods"]), raw.get("item_level_restriction"))
        for ess_id, raw in _read_json(directory / "essences.json", manifest).items()
    }
    fossils = {fid: parse_fossil(fid, raw) for fid, raw in raw_fossils.items() if raw.get("name")}
    raw_clusters = _read_optional(directory, "cluster_jewels", manifest)
    downloaded = manifest.get("downloaded_at")
    return GameData(
        mods=mods,
        bases=bases,
        essences=essences,
        fossils=fossils,
        game_version=manifest.get("game_version"),
        downloaded_at=datetime.fromisoformat(downloaded) if downloaded else None,
        source_url=manifest.get("source", EXPORT_BASE_URL),
        clusters=parse_clusters(raw_clusters) if raw_clusters else {},
    )


def gamedata_dir(ctx: AppContext) -> Path:
    return ctx.config.data_dir / GAMEDATA_DIRNAME


def get_gamedata(ctx: AppContext) -> GameData:
    """The shared GameData, loaded on first use. Failures are not cached, so a retry after
    running the fetch script succeeds without restarting the server."""
    return ctx.service("gamedata", lambda c: load_gamedata(gamedata_dir(c)))
