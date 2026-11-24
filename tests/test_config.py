from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from poe_craft_mcp import REPO_URL, __version__
from poe_craft_mcp.config import CONFIG_ENV_VAR, Config, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_defaults_without_any_file() -> None:
    cfg = load_config()
    assert cfg.league == "auto"
    assert cfg.contact is None
    assert cfg.ninja_min_interval_min == 15
    assert cfg.trade_cache_min == 20
    assert cfg.trade_sample == 30
    assert cfg.undercut_pct == 5
    assert cfg.min_listings == 10
    assert cfg.include_unverified is False
    assert cfg.data_dir.name == "data"
    assert cfg.cache_path == cfg.data_dir / "cache.db"
    assert cfg.ninja_url(cfg.ninja_leagues_path) == "https://poe.ninja/poe1/api/economy/leagues"


def test_local_config_toml_is_used(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('league = "Standard"\ndata_dir = "store"\n')
    cfg = load_config()
    assert cfg.league == "Standard"
    assert cfg.data_dir == tmp_path / "store"


def test_env_var_overrides_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.toml").write_text('league = "Local"\n')
    other = tmp_path / "elsewhere" / "custom.toml"
    other.parent.mkdir()
    other.write_text('league = "FromEnv"\nundercut_pct = 7.5\n')
    monkeypatch.setenv(CONFIG_ENV_VAR, str(other))
    cfg = load_config()
    assert cfg.league == "FromEnv"
    assert cfg.undercut_pct == 7.5
    assert cfg.data_dir.parent.name != "elsewhere"


def test_env_var_pointing_nowhere_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, "does-not-exist.toml")
    with pytest.raises(ConfigError, match="missing file"):
        load_config()


@pytest.mark.parametrize(
    "body",
    [
        "undercut_pct = 51",
        "undercut_pct = -1",
        "ninja_min_interval_min = 14",
        "trade_sample = 0",
        "unknown_key = 1",
        'contact = "a\\nb"',
        'ninja_base_url = "poe.ninja"',
        'ninja_leagues_path = "no-slash"',
        "league = ",
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, body: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(body + "\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_user_agent_defaults_contact_to_repo_url() -> None:
    ua = Config().user_agent
    assert ua == f"poe-craft-mcp/{__version__} (+{REPO_URL}; contact: {REPO_URL})"


def test_user_agent_uses_configured_contact() -> None:
    ua = Config(contact="  someone@example.com ").user_agent
    assert ua.endswith("contact: someone@example.com)")
    assert REPO_URL in ua


def test_example_config_is_valid_and_matches_defaults() -> None:
    cfg = load_config(REPO_ROOT / "config.example.toml")
    defaults = Config()
    assert cfg.model_dump(exclude={"data_dir"}) == defaults.model_dump(exclude={"data_dir"})


def test_minimum_ninja_interval_is_fifteen_minutes(tmp_path: Path) -> None:
    path = tmp_path / "ok.toml"
    path.write_text("ninja_min_interval_min = 15\n")
    assert load_config(path).ninja_min_interval_min == 15


SECRET = "f00dfacecafebabe1234"


@pytest.mark.parametrize(
    "body",
    [
        f'POESESSID = "{SECRET}"',
        f'poesessid = "{SECRET}"',
        f'cookie = "{SECRET}"',
        f'league = ["{SECRET}"]',
        f'log_level = "{SECRET}"',
        f'[trade]\nsession = "{SECRET}"',
    ],
)
def test_config_errors_never_echo_input_values(tmp_path: Path, body: str) -> None:
    path = tmp_path / "leaky.toml"
    path.write_text(body + "\n")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert SECRET not in "".join(traceback.format_exception(info.value))


def test_poesessid_key_gets_a_helpful_error(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('POESESSID = "x"\n')
    with pytest.raises(ConfigError, match="keychain"):
        load_config(path)
