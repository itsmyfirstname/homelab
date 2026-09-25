"""MCP tools over ``studio_hays.home_assistant``.

The tool names, parameters and result shapes are the ones this server has
always exposed; the HTTP client, SQLite cache and sync policy now live in
studio-hays. There is no background poller: a read refreshes the cache first
when it is older than ``SYNC_INTERVAL * 2`` seconds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from studio_hays.core import StudioHaysError
from studio_hays.home_assistant import Client

from .config import load_settings

STALE_FACTOR = 2

mcp = MCPServer("home-assistant")


def _client() -> Client:
    settings = load_settings()
    if settings is None:
        raise ToolError("HA_URL and HA_TOKEN must be set in the .env file")
    client = Client(url=settings.ha_url, token=settings.ha_token, db=str(settings.db_path))
    client.staleness_window = settings.sync_interval * STALE_FACTOR
    return client


def _run(op: Callable[[Client], dict[str, Any]]) -> dict[str, Any]:
    client = _client()
    try:
        return op(client)
    except StudioHaysError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def list_entities(domain: str | None = None, search: str | None = None, limit: int = 100) -> dict:
    """List Home Assistant entities from the local cache.

    The cache is a full snapshot of Home Assistant, refreshed before a read
    whenever it is older than 2 x SYNC_INTERVAL seconds, so "stale" is false
    unless Home Assistant could not be reached. Returns compact rows with
    entity_id, state, and friendly_name. Optionally filter by domain (e.g.
    "light", "sensor") and/or a case-insensitive search term matched against
    entity_id or friendly_name. Use get_entity for full attributes of a
    single entity.
    """
    return _run(lambda c: c.get.entities(domain=domain, search=search, limit=limit))


@mcp.tool()
def get_entity(entity_id: str) -> dict:
    """Get the full state and complete attributes for one Home Assistant entity.

    Use list_entities first to find entity_ids.
    """
    env = _run(lambda c: c.get.entity(entity_id))
    return {"last_synced": env["last_synced"], "stale": env["stale"], **env["entity"]}


@mcp.tool()
def list_domains() -> dict:
    """List entity domains with entity counts (e.g. light: 16, sensor: 73).

    Useful for orienting in the house before filtering with list_entities.
    """
    return _run(lambda c: c.get.domains())


@mcp.tool()
def call_service(domain: str, service: str, service_data: dict | None = None) -> dict:
    """Call a Home Assistant service.

    domain is the entity domain - the prefix of an entity_id before the first
    "." (e.g. "light" for light.bedroom). service is the service name (e.g.
    "turn_on"). service_data carries the service parameters, including
    "entity_id" to target an entity, e.g. call_service("light", "turn_on",
    {"entity_id": "light.bedroom", "brightness_pct": 50}).

    Returns the entity_ids whose state changed directly as a result. The next
    read refreshes the cache, so it reflects the call; slower changes (fades,
    transitions) show up once they land in Home Assistant.
    """
    return _run(lambda c: c.call.service(domain, service, **(service_data or {})))
