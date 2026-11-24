"""Download the RePoE-fork export into a staging folder, validate it, then publish it whole.

A failed or partial run never touches the previously published export: under an exclusive
fetch lock, files are streamed into a private `<target>.staging-*` dir (each capped at
MAX_FILE_BYTES decoded), parsed and hash-checked there, and only then swapped in for `<target>`
by directory renames (see `publish`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from poe_craft_mcp.gamedata.loader import EXPORT_BASE_URL, EXPORT_FILES, MANIFEST_NAME
from poe_craft_mcp.gamedata.publish import (
    FetchError,
    clean_work_dirs,
    fetch_lock,
    publish,
    recover_layout,
)

__all__ = ["MAX_FILE_BYTES", "FetchError", "fetch_export"]

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_VERSION_BYTES = 1024
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def _stream(client: httpx.Client, url: str, dest: Path, max_bytes: int) -> str:
    """Stream `url` into `dest`, aborting past `max_bytes`; return the sha256 of the bytes."""
    hasher = hashlib.sha256()
    size = 0
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError(f"{url} is larger than {max_bytes} bytes; aborted")
                hasher.update(chunk)
                fh.write(chunk)
    return hasher.hexdigest()


def _validate(path: Path, sha256: str) -> int:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != sha256:
        raise FetchError(f"{path.name} changed on disk while downloading")
    try:
        json.loads(content)
    except ValueError as exc:
        raise FetchError(f"{path.name} is not valid JSON: {exc}") from exc
    return len(content)


def fetch_export(
    target: Path,
    user_agent: str,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = MAX_FILE_BYTES,
) -> dict[str, Any]:
    """Download, validate and publish the export to `target`; return the manifest written."""
    with fetch_lock(target):
        recover_layout(target)
        staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f"{target.name}.staging-"))
        try:
            manifest = _download_all(staging, user_agent, client, max_bytes)
            publish(staging, target)
            clean_work_dirs(target)
        except httpx.HTTPError as exc:
            raise FetchError(f"download failed: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return manifest


def _download_all(
    staging: Path, user_agent: str, client: httpx.Client | None, max_bytes: int
) -> dict[str, Any]:
    with ExitStack() as stack:
        if client is None:
            headers = {"User-Agent": user_agent}
            client = stack.enter_context(httpx.Client(headers=headers, timeout=TIMEOUT))
        version_url = f"{EXPORT_BASE_URL}/version.txt"
        version_path = staging / "version.txt"
        _stream(client, version_url, version_path, MAX_VERSION_BYTES)
        game_version = version_path.read_text(encoding="utf-8").strip() or None
        version_path.unlink()
        files: dict[str, dict[str, Any]] = {}
        for name in EXPORT_FILES:
            url = f"{EXPORT_BASE_URL}/{name}.min.json"
            log.info("downloading %s", url)
            dest = staging / f"{name}.json"
            sha256 = _stream(client, url, dest, max_bytes)
            files[name] = {"url": url, "bytes": _validate(dest, sha256), "sha256": sha256}
    manifest: dict[str, Any] = {
        "source": EXPORT_BASE_URL,
        "version_url": version_url,
        "game_version": game_version,
        "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": files,
    }
    (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
