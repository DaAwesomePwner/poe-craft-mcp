"""MCP server factory and the `poe-craft-mcp` stdio entry point."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from poe_craft_mcp import __version__
from poe_craft_mcp.config import Config, ConfigError, load_config
from poe_craft_mcp.context import AppContext
from poe_craft_mcp.log import setup_logging
from poe_craft_mcp.tools import register_all

SERVER_NAME = "poe-craft-mcp"
INSTRUCTIONS = (
    "Path of Exile 1 crafting economics. Quote numbers only from tool results, together with "
    "their data_age_seconds and sources. Never invent crafts that are not in the recipe list."
)

log = logging.getLogger(__name__)


def build_server(config: Config | None = None, ctx: AppContext | None = None) -> MCPServer:
    """Create the server and register every tool module under `poe_craft_mcp.tools`."""
    if ctx is None:
        ctx = AppContext.create(config or load_config())

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[AppContext]:
        try:
            yield ctx
        finally:
            await ctx.aclose()

    server = MCPServer(
        SERVER_NAME, version=__version__, instructions=INSTRUCTIONS, lifespan=lifespan
    )
    register_all(server, ctx)
    return server


def main() -> None:
    """Console entry point: load config, set up logging, serve MCP over stdio."""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"poe-craft-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(config.logs_dir, config.log_level)
    log.info("starting %s %s (data_dir=%s)", SERVER_NAME, __version__, config.data_dir)
    build_server(config).run("stdio")


if __name__ == "__main__":
    main()
