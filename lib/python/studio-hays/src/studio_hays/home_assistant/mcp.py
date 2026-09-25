"""Per-module MCP server for Home Assistant (doc 04): tools ``ha_*``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.mcp import build_server
from .client import Client
from .operations import OPERATIONS

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

PREFIX = "ha"


def create_server(client: Client | None = None) -> "MCPServer":
    """Connects first (doc 04) so bad config fails before any tool call."""
    client = client or Client()
    client.connect()
    return build_server(f"studio-hays.{client.module}", [(PREFIX, client, OPERATIONS)])
