"""Optional POESESSID: OS keychain first, then the POESESSID environment variable.

The value is registered for log masking whenever it is read and is never returned by a
tool, printed, or placed in an exception message.

CLI: `python -m poe_craft_mcp.secrets set|clear|status`.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

import keyring
from keyring.errors import PasswordDeleteError

from poe_craft_mcp.log import register_secret

SERVICE = "poe-craft-mcp"
USERNAME = "POESESSID"
ENV_VAR = "POESESSID"

log = logging.getLogger(__name__)


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def _from_keyring() -> str | None:
    try:
        return _clean(keyring.get_password(SERVICE, USERNAME))
    except Exception as exc:  # no backend, locked keychain, backend bug: fall back to env
        log.debug("keyring unavailable (%s); using the %s env var", type(exc).__name__, ENV_VAR)
        return None


def _lookup() -> tuple[str | None, str | None]:
    """(value, source) where source is "keyring", "env" or None."""
    value = _from_keyring()
    source = "keyring" if value else None
    if value is None:
        value = _clean(os.environ.get(ENV_VAR))
        source = "env" if value else None
    register_secret(value)
    return value, source


def get_poesessid() -> str | None:
    """The POESESSID cookie value, or None. Internal use only: never return it from a tool."""
    return _lookup()[0]


def poesessid_set() -> bool:
    return get_poesessid() is not None


class KeychainError(Exception):
    """The keychain rejected an operation. The message names only the failure type."""


def set_poesessid(value: str) -> None:
    """Store the value in the OS keychain.

    Raises `ValueError` for an empty value and `KeychainError` (never quoting the value,
    whatever the backend said) when the keychain fails.
    """
    value = _clean(value) or ""
    if not value:
        raise ValueError("POESESSID is empty")
    register_secret(value)
    try:
        keyring.set_password(SERVICE, USERNAME, value)
    except Exception as exc:
        raise KeychainError(type(exc).__name__) from None


def clear_poesessid() -> bool:
    """Remove the keychain entry. Returns False if there was none."""
    try:
        keyring.delete_password(SERVICE, USERNAME)
    except PasswordDeleteError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m poe_craft_mcp.secrets")
    parser.add_argument("action", choices=["set", "clear", "status"])
    action = parser.parse_args(argv).action
    try:
        if action == "set":
            value = _clean(getpass.getpass("POESESSID (input hidden): "))
            if value is None:
                print("error: POESESSID is empty", file=sys.stderr)
                return 1
            register_secret(value)
            set_poesessid(value)
            print("POESESSID stored in the OS keychain.")
        elif action == "clear":
            print("POESESSID removed." if clear_poesessid() else "No POESESSID in the keychain.")
        else:
            _, source = _lookup()
            print(f"POESESSID: set ({source})" if source else "POESESSID: not set")
    except Exception as exc:  # report the type only: backend messages may quote the value
        name = str(exc) if isinstance(exc, KeychainError) else type(exc).__name__
        print(
            f"error: keychain unavailable ({name}); set the {ENV_VAR} environment variable instead",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
