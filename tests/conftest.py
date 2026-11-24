"""Shared fixtures. Tests never touch the network or the real data/ folder."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from poe_craft_mcp.config import CONFIG_ENV_VAR, Config
from poe_craft_mcp.http.cache import ResponseCache
from poe_craft_mcp.log import clear_secrets

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeClock:
    """Manually advanced clock, usable wherever a `Clock` is accepted."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.delenv("POESESSID", raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    clear_secrets()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")


@pytest.fixture
def cache(config: Config, clock: FakeClock) -> ResponseCache:
    return ResponseCache(config.cache_path, clock=clock)
