"""Logging setup. stdout belongs to the MCP stdio transport, so logs go to stderr and files."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MASK = "***"
_HANDLER_TAG = "poe_craft_mcp"

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask `value` in every log line from now on. Empty values are ignored."""
    if value:
        _secrets.add(value)


def clear_secrets() -> None:
    """Forget all registered secrets (for tests)."""
    _secrets.clear()


def mask_secrets(text: str) -> str:
    """Replace every registered secret in `text` with `***`."""
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


class SecretMaskingFilter(logging.Filter):
    """Rewrites a record's message and exception text so no registered secret survives."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _secrets:
            return True
        record.msg = mask_secrets(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = mask_secrets(record.stack_info)
        return True


def _prepare(handler: logging.Handler) -> logging.Handler:
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(SecretMaskingFilter())
    handler.set_name(_HANDLER_TAG)
    return handler


def _file_handler(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    return _prepare(RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))


def setup_logging(logs_dir: Path, level: str = "INFO") -> None:
    """Send root logs to stderr and `<logs_dir>/server.log`, both secret-masked.

    Calling it again replaces the handlers it added earlier and leaves others alone.
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if h.get_name() == _HANDLER_TAG]:
        root.removeHandler(handler)
        handler.close()
    for handler in root.handlers:
        if not any(isinstance(f, SecretMaskingFilter) for f in handler.filters):
            handler.addFilter(SecretMaskingFilter())
    root.addHandler(_prepare(logging.StreamHandler(sys.stderr)))
    root.addHandler(_file_handler(logs_dir / "server.log"))
    root.setLevel(level)


def add_file_log(logger_name: str, path: Path) -> logging.Logger:
    """Also write `logger_name` to its own rotating file (e.g. `ratelimit.log`)."""
    logger = logging.getLogger(logger_name)
    target = str(path.resolve())
    if not any(getattr(h, "baseFilename", None) == target for h in logger.handlers):
        logger.addHandler(_file_handler(path))
    return logger
