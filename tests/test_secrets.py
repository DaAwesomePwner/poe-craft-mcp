from __future__ import annotations

import logging
from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

from poe_craft_mcp import secrets

SECRET = "f00d-POESESSID-1234abcd"


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


@pytest.fixture
def ring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def no_backend() -> Iterator[None]:
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    yield
    keyring.set_keyring(previous)


def test_keyring_wins_over_env(ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    ring.store[(secrets.SERVICE, secrets.USERNAME)] = SECRET
    monkeypatch.setenv("POESESSID", "env-value")
    assert secrets.get_poesessid() == SECRET
    assert secrets.poesessid_set()


def test_env_used_when_keyring_empty(ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POESESSID", f"  {SECRET}\n")
    assert secrets.get_poesessid() == SECRET


def test_missing_backend_falls_back_to_env_silently(
    no_backend: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("POESESSID", SECRET)
    assert secrets.get_poesessid() == SECRET
    assert "keyring unavailable (NoKeyringError)" in caplog.text
    assert not [r for r in caplog.records if r.levelno > logging.DEBUG]


def test_not_set_anywhere(ring: MemoryKeyring) -> None:
    assert secrets.get_poesessid() is None
    assert not secrets.poesessid_set()


def test_reading_registers_value_for_masking(
    ring: MemoryKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    ring.store[(secrets.SERVICE, secrets.USERNAME)] = SECRET
    secrets.get_poesessid()
    logging.getLogger("anything").warning("value %s", SECRET)
    assert "value ***" in caplog.text and SECRET not in caplog.text


def test_cli_set_status_clear_never_print_value(
    ring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(secrets.getpass, "getpass", lambda prompt: SECRET)
    assert secrets.main(["set"]) == 0
    assert ring.store[(secrets.SERVICE, secrets.USERNAME)] == SECRET
    assert secrets.main(["status"]) == 0
    assert secrets.main(["clear"]) == 0
    assert secrets.main(["clear"]) == 0
    assert secrets.main(["status"]) == 0
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert out.out.splitlines() == [
        "POESESSID stored in the OS keychain.",
        "POESESSID: set (keyring)",
        "POESESSID removed.",
        "No POESESSID in the keychain.",
        "POESESSID: not set",
    ]


def test_cli_set_without_backend_fails_cleanly(
    no_backend: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(secrets.getpass, "getpass", lambda prompt: SECRET)
    assert secrets.main(["set"]) == 1
    err = capsys.readouterr().err
    assert "environment variable" in err and SECRET not in err
    monkeypatch.setattr(secrets.getpass, "getpass", lambda prompt: "   ")
    assert secrets.main(["set"]) == 1
