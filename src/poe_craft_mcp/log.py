"""Logging setup. stdout belongs to the MCP stdio transport, so logs go to stderr and files."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MASK = "***"
_HANDLER_TAG = "poe_craft_mcp"

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask `value` in every log line from now on, whatever logger or handler emits it.

    Empty values are ignored. See `install_masking` for how coverage is guaranteed.
    """
    if value:
        install_masking()
        _secrets.add(value)


def clear_secrets() -> None:
    """Forget all registered secrets (for tests)."""
    _secrets.clear()


def mask_secrets(text: str) -> str:
    """Replace every registered secret in `text` with `***`."""
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


def mask_record(record: logging.LogRecord) -> None:
    """Mask the message, args, exception/stack text and every string attribute (`extra`)."""
    if not _secrets:
        return
    try:
        message = record.getMessage()
    except Exception:  # malformed %-args: keep the raw parts so nothing is lost
        message = f"{record.msg} {record.args!r}"
    record.msg = message
    record.args = None
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    for name, value in list(vars(record).items()):
        if isinstance(value, str):
            setattr(record, name, mask_secrets(value))


def install_masking() -> None:
    """Patch logging once, process-wide, so masking can't be bypassed by later code.

    `Logger.makeRecord` masks each record after `extra` is merged; `Handler.format` masks
    the final formatted line, catching secrets inside non-string extras (dicts, objects).
    """
    if getattr(logging.Logger.makeRecord, "_poe_craft_masking", False):
        return
    make_record = logging.Logger.makeRecord
    handler_format = logging.Handler.format

    def masked_make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = make_record(self, *args, **kwargs)
        mask_record(record)
        return record

    def masked_format(self: logging.Handler, record: logging.LogRecord) -> str:
        return mask_secrets(handler_format(self, record))

    masked_make_record._poe_craft_masking = True  # type: ignore[attr-defined]
    logging.Logger.makeRecord = masked_make_record  # type: ignore[method-assign]
    logging.Handler.format = masked_format  # type: ignore[method-assign]


class SecretMaskingFilter(logging.Filter):
    """Handler-level safety net for records not built by `Logger.makeRecord`."""

    def filter(self, record: logging.LogRecord) -> bool:
        mask_record(record)
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
    install_masking()
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
    """Also write `logger_name` to its own rotating file (e.g. `ratelimit.log`), secret-masked."""
    install_masking()
    logger = logging.getLogger(logger_name)
    target = str(path.resolve())
    if not any(getattr(h, "baseFilename", None) == target for h in logger.handlers):
        logger.addHandler(_file_handler(path))
    return logger
