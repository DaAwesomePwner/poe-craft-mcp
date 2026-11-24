"""Download the RePoE-fork game data export into `<data_dir>/gamedata/`.

Usage: uv run python scripts/fetch_gamedata.py [--config path/to/config.toml]

Writes mods.json, base_items.json, essences.json, fossils.json, crafting_bench_options.json,
stat_translations.json and a manifest.json recording the source URLs, download time, the game
version the export was built from (the export's version.txt) and a sha256 per file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from poe_craft_mcp.config import ConfigError, load_config
from poe_craft_mcp.gamedata.loader import (
    EXPORT_BASE_URL,
    EXPORT_FILES,
    GAMEDATA_DIRNAME,
    MANIFEST_NAME,
)

TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def _write_atomic(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(content)
    tmp.replace(path)


def fetch(target: Path, user_agent: str) -> dict[str, object]:
    target.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, object]] = {}
    with httpx.Client(headers={"User-Agent": user_agent}, timeout=TIMEOUT) as client:
        version_url = f"{EXPORT_BASE_URL}/version.txt"
        resp = client.get(version_url)
        resp.raise_for_status()
        game_version = resp.text.strip() or None
        for name in EXPORT_FILES:
            url = f"{EXPORT_BASE_URL}/{name}.min.json"
            print(f"downloading {url}", file=sys.stderr)
            resp = client.get(url)
            resp.raise_for_status()
            content = resp.content
            json.loads(content)
            _write_atomic(target / f"{name}.json", content)
            files[name] = {
                "url": url,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    manifest: dict[str, object] = {
        "source": EXPORT_BASE_URL,
        "version_url": version_url,
        "game_version": game_version,
        "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": files,
    }
    _write_atomic(target / MANIFEST_NAME, json.dumps(manifest, indent=2).encode("utf-8"))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=None, help="config.toml to read")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.exit(f"fetch_gamedata: {exc}")
    target = config.data_dir / GAMEDATA_DIRNAME
    try:
        manifest = fetch(target, config.user_agent)
    except (httpx.HTTPError, ValueError) as exc:
        sys.exit(f"fetch_gamedata: download failed: {exc}")
    print(
        f"saved RePoE export (game version {manifest['game_version'] or 'unknown'}) to {target}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
