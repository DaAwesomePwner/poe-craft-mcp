"""MCP tools. Each module here defines `register(server, ctx)` and is loaded automatically.

To add tools, create `tools/<name>.py` with:

    def register(server: MCPServer, ctx: AppContext) -> None:
        @server.tool(annotations=ToolAnnotations(read_only_hint=True))
        async def my_tool(...) -> dict[str, Any]:
            return envelope({...}, sources=[...])
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from poe_craft_mcp.context import AppContext


def tool_modules() -> list[str]:
    """Fully qualified names of all tool modules, in a stable order."""
    names = sorted(info.name for info in pkgutil.iter_modules(__path__))
    return [f"{__name__}.{name}" for name in names if not name.startswith("_")]


def register_all(server: MCPServer, ctx: AppContext) -> None:
    """Import every tool module and call its `register(server, ctx)`."""
    for name in tool_modules():
        module = importlib.import_module(name)
        register = getattr(module, "register", None)
        if register is None:
            raise RuntimeError(f"tool module {name} has no register(server, ctx) function")
        register(server, ctx)
