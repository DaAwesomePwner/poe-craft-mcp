from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from poe_craft_mcp.config import Config
from poe_craft_mcp.envelope import envelope, source
from poe_craft_mcp.log import SecretMaskingFilter, register_secret, setup_logging
from poe_craft_mcp.server import SERVER_NAME, build_server

SECRET = "a1b2c3d4e5f6deadbeef"


async def test_server_lists_and_calls_get_league_status(config: Config) -> None:
    server = build_server(config)
    assert server.name == SERVER_NAME
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert "get_league_status" in tools
        annotations = tools["get_league_status"].annotations
        assert annotations is not None and annotations.read_only_hint is True

        result = await client.call_tool("get_league_status", {})
    assert not result.is_error
    data: dict[str, Any] = result.structured_content or {}
    assert data["league"] == "auto"
    assert data["chaos_per_divine"] is None
    assert data["poesessid_set"] is False
    assert data["data_age_seconds"] is None
    assert data["sources"] == []
    assert data["note"] == "stub"


def test_envelope_takes_oldest_source_age() -> None:
    out = envelope(
        {"rows": []},
        [source("a", age_seconds=10.04), source("b", age_seconds=300, stale=True)],
    )
    assert out["data_age_seconds"] == 300
    assert [s["name"] for s in out["sources"]] == ["a", "b"]
    assert out["sources"][0]["age_seconds"] == 10.0
    with pytest.raises(ValueError):
        envelope({"sources": []})


def test_masking_filter_hides_registered_secret() -> None:
    register_secret(SECRET)
    register_secret("")
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "cookie=%s header=%r", (SECRET, {"POESESSID": SECRET}), None
    )
    assert SecretMaskingFilter().filter(record)
    message = record.getMessage()
    assert SECRET not in message
    assert message.count("***") == 2


def test_setup_logging_masks_stderr_and_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        setup_logging(tmp_path / "logs")
        register_secret(SECRET)
        try:
            raise RuntimeError(f"boom {SECRET}")
        except RuntimeError:
            logging.getLogger("poe_craft_mcp.test").exception("failed with %s", SECRET)
        for handler in root.handlers:
            handler.flush()
        captured = capsys.readouterr()
        logged = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(level)
    assert captured.out == ""
    assert "failed with ***" in captured.err and "boom ***" in captured.err
    assert "failed with ***" in logged and "boom ***" in logged
    assert SECRET not in captured.err + logged
