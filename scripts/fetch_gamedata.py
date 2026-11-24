"""Download the RePoE-fork game data export into `<data_dir>/gamedata/`.

Usage: uv run python scripts/fetch_gamedata.py [--config path/to/config.toml]

Writes mods.json, base_items.json, essences.json, fossils.json, crafting_bench_options.json,
stat_translations.json and a manifest.json recording the source URLs, download time, the game
version the export was built from (the export's version.txt) and a sha256 per file. The new
export replaces the old one only after every file downloaded and validated.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from poe_craft_mcp.config import ConfigError, load_config
from poe_craft_mcp.gamedata.fetch import FetchError, fetch_export
from poe_craft_mcp.gamedata.loader import GAMEDATA_DIRNAME


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=None, help="config.toml to read")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.exit(f"fetch_gamedata: {exc}")
    target = config.data_dir / GAMEDATA_DIRNAME
    try:
        manifest = fetch_export(target, config.user_agent)
    except (FetchError, OSError) as exc:
        sys.exit(f"fetch_gamedata: {exc} (previous export left unchanged)")
    version = manifest["game_version"] or "unknown"
    print(f"saved RePoE export (game version {version}) to {target}", file=sys.stderr)


if __name__ == "__main__":
    main()
