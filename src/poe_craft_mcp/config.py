"""Server configuration, loaded from a TOML file with validated defaults."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from poe_craft_mcp import REPO_URL, __version__

CONFIG_ENV_VAR = "POE_CRAFT_CONFIG"
DEFAULT_CONFIG_NAME = "config.toml"


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable or invalid."""


def _project_root() -> Path:
    """Repo root for a source checkout (editable install), else the working directory."""
    package_dir = Path(__file__).resolve().parent
    candidate = package_dir.parent.parent
    if package_dir.parent.name == "src" and (candidate / "pyproject.toml").is_file():
        return candidate
    return Path.cwd()


def default_data_dir() -> Path:
    return _project_root() / "data"


class Config(BaseModel):
    """All user-tunable settings. Unknown keys are rejected to catch typos."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    league: str = Field("auto", min_length=1)
    contact: str | None = None
    ninja_min_interval_min: int = Field(15, ge=15, le=1440)
    trade_cache_min: int = Field(20, ge=1, le=1440)
    trade_sample: int = Field(30, ge=1, le=100)
    undercut_pct: float = Field(5, ge=0, le=50)
    min_listings: int = Field(10, ge=0)
    include_unverified: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    data_dir: Path = Field(default_factory=default_data_dir)

    ninja_base_url: str = "https://poe.ninja"
    ninja_leagues_path: str = "/poe1/api/economy/leagues"
    ninja_exchange_path: str = "/poe1/api/economy/exchange/current/overview"
    ninja_stash_item_path: str = "/poe1/api/economy/stash/current/item/overview"
    trade_base_url: str = "https://www.pathofexile.com"

    @field_validator("contact")
    @classmethod
    def _clean_contact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if any(ch in value for ch in "\r\n()"):
            raise ValueError("contact must be a single line without parentheses")
        return value or None

    @field_validator("ninja_base_url", "trade_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("base URL must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("ninja_leagues_path", "ninja_exchange_path", "ninja_stash_item_path")
    @classmethod
    def _leading_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("endpoint path must start with '/'")
        return value

    @property
    def user_agent(self) -> str:
        """Identifiable User-Agent sent to poe.ninja and the trade site."""
        contact = self.contact or REPO_URL
        return f"poe-craft-mcp/{__version__} (+{REPO_URL}; contact: {contact})"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "cache.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ninja_url(self, path: str) -> str:
        """Absolute poe.ninja URL for one of the configured endpoint paths."""
        return f"{self.ninja_base_url}{path}"


def find_config_file() -> Path | None:
    """Return the config file to load: $POE_CRAFT_CONFIG, then ./config.toml, else None."""
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"{CONFIG_ENV_VAR} points to a missing file: {path}")
        return path
    local = Path.cwd() / DEFAULT_CONFIG_NAME
    return local if local.is_file() else None


def load_config(path: Path | None = None) -> Config:
    """Load and validate the config. Relative `data_dir` resolves against the file's folder."""
    path = path or find_config_file()
    if path is None:
        return Config()
    try:
        raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if any(key.upper() == "POESESSID" for key in raw):
        raise ConfigError(
            f"{path}: POESESSID must not be stored in the config file; "
            "use the OS keychain or the POESESSID environment variable"
        )
    if "data_dir" in raw:
        data_dir = Path(str(raw["data_dir"])).expanduser()
        raw["data_dir"] = data_dir if data_dir.is_absolute() else path.resolve().parent / data_dir
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{_describe(exc)}") from None


def _describe(exc: ValidationError) -> str:
    """Error lines without the offending input values, which could be secrets."""
    lines = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)
