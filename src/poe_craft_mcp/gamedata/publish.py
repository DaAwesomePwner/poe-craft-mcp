"""Publishing a downloaded export: fetch lock, recoverable directory swap, crash recovery.

Layout next to `<data_dir>/gamedata`:
- `gamedata.fetch.lock`: held for a whole fetch. If no fetch is running (e.g. one was killed),
  delete it by hand.
- `gamedata.previous`: the export replaced by the last fetch. If a fetch dies mid-publish, the
  next fetch's `recover_layout` restores `gamedata` and `gamedata.previous` before deleting
  anything. The MCP loader never moves directories.
- `gamedata.staging-*` / `gamedata.discard-*`: per-run work dirs, removed only after a later
  fetch has published successfully.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


class FetchError(Exception):
    """The download failed, was too large, invalid or locked; nothing was published."""


def previous_dir(target: Path) -> Path:
    return target.with_name(target.name + ".previous")


def lock_path(target: Path) -> Path:
    return target.with_name(target.name + ".fetch.lock")


def recover_previous(target: Path) -> None:
    """Restore `gamedata.previous` if `gamedata` is missing. Call only while holding the lock."""
    previous = previous_dir(target)
    if not target.exists() and previous.is_dir():
        previous.rename(target)
        log.warning("restored %s from %s after an interrupted fetch", target.name, previous.name)


def recover_layout(target: Path) -> None:
    """Undo a half-finished publish before anything is deleted. Call only while holding the lock.

    Restores `gamedata` from `gamedata.previous`, then refills `gamedata.previous` from the
    newest displaced `gamedata.discard-*` backup if it is missing."""
    recover_previous(target)
    previous = previous_dir(target)
    discards = sorted(
        target.parent.glob(f"{target.name}.discard-*"), key=lambda p: p.stat().st_mtime
    )
    if discards and not previous.exists():
        discards[-1].rename(previous)
        log.warning("restored %s from %s", previous.name, discards[-1].name)


@contextmanager
def fetch_lock(target: Path) -> Iterator[None]:
    path = lock_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(f"pid {os.getpid()} at {datetime.now(UTC).isoformat(timespec='seconds')}\n")
    except FileExistsError:
        raise FetchError(
            f"another game data fetch is running (lock file {path}); if none is running, "
            "delete that file and retry"
        ) from None
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def clean_work_dirs(target: Path) -> None:
    """Remove staging/discard leftovers. Call only under the lock, after a successful publish."""
    for prefix in (".staging-", ".discard-"):
        for leftover in target.parent.glob(f"{target.name}{prefix}*"):
            shutil.rmtree(leftover, ignore_errors=True)


def _swap_in(staging: Path, target: Path) -> None:
    staging.rename(target)


def publish(staging: Path, target: Path) -> None:
    """Move `target` to `gamedata.previous` and `staging` to `target`.

    The older previous export is deleted only after the new one is in place. If the second
    rename fails, the export just moved aside is restored.
    """
    previous = previous_dir(target)
    discard: Path | None = None
    if previous.exists() and target.exists():
        discard = target.with_name(f"{target.name}.discard-{uuid.uuid4().hex[:8]}")
        previous.rename(discard)
    if target.exists():
        target.rename(previous)
    try:
        _swap_in(staging, target)
    except BaseException:
        recover_previous(target)
        raise
    if discard is not None:
        shutil.rmtree(discard, ignore_errors=True)
