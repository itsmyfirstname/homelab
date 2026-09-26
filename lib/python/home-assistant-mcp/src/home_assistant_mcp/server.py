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
from studio_hays.home_assistant.client import service_data as checked_service_data

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


MUTATING = (
    "Mutating: changes Home Assistant's registry. Pass dry_run=true first to "
    "see exactly what would change."
)


def _given(**kwargs: Any) -> dict[str, Any]:
    """Only the arguments the caller actually passed (None = not passed)."""
    return {k: v for k, v in kwargs.items() if v is not None}


@mcp.tool()
def list_entities(
    domain: str | None = None,
    search: str | None = None,
    area: str | None = None,
    label: str | None = None,
    limit: int = 100,
) -> dict:
    """List Home Assistant entities from the local cache.

    The cache is a full snapshot of Home Assistant, refreshed before a read
    whenever it is older than 2 x SYNC_INTERVAL seconds, so "stale" is false
    unless Home Assistant could not be reached. Returns compact rows with
    entity_id, state, and friendly_name. Optionally filter by domain (e.g.
    "light", "sensor"), a case-insensitive search term matched against
    entity_id or friendly_name, an area (name or id; the entity's own area,
    else its device's) and/or a label (name or id, on the entity or its
    device). Use get_entity for full attributes of a single entity.
    """
    return _run(lambda c: c.get.entities(**_given(
        domain=domain, search=search, area=area, label=label, limit=limit,
    )))


@mcp.tool()
def get_entity(entity_id: str) -> dict:
    """Get the full state and complete attributes for one Home Assistant entity,
    plus its registry info: area (and whether it comes from the device),
    floor, device, labels, and any name override.

    Use list_entities first to find entity_ids.
    """
    env = _run(lambda c: c.get.entity(entity_id))
    return {
        "last_synced": env["last_synced"],
        "stale": env["stale"],
        **env["entity"],
        "registry": env["registry"],
    }


@mcp.tool()
def list_areas() -> dict:
    """List areas (rooms) with their floor and how many devices and entities are in each."""
    return _run(lambda c: c.get.areas())


@mcp.tool()
def list_labels() -> dict:
    """List labels with how many devices and entities carry each."""
    return _run(lambda c: c.get.labels())


@mcp.tool()
def get_device(device: str) -> dict:
    """Get a device - by name, device id, or one of its entity_ids - with its
    area, floor, labels and all of its entities (with any name/area overrides).
    """
    return _run(lambda c: c.get.device(device))


@mcp.tool(description=(
    "Rename an entity, move it to an area, add/remove labels, or change its "
    'entity_id. name="" clears a name override (the entity inherits its '
    'device name again); area="" clears the area override (it inherits the '
    "device's area). Areas and labels are names or ids. new_entity_id must keep "
    "the domain and breaks automations that use the old id. " + MUTATING
))
def update_entity(
    entity_id: str,
    name: str | None = None,
    area: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    new_entity_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    return _run(lambda c: c.update.entity(entity_id, dry_run=dry_run, **_given(
        name=name, area=area, add_labels=add_labels,
        remove_labels=remove_labels, new_entity_id=new_entity_id,
    )))


@mcp.tool(description=(
    "Rename a device, move it to an area (its entities that inherit the "
    "device's area move with it), or add/remove labels. device is a name, id, "
    'or one of its entity_ids. name="" / area="" clear. ' + MUTATING
))
def update_device(
    device: str,
    name: str | None = None,
    area: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    return _run(lambda c: c.update.device(device, dry_run=dry_run, **_given(
        name=name, area=area, add_labels=add_labels, remove_labels=remove_labels,
    )))


@mcp.tool(description=(
    "Repurpose a device that moved rooms and got a new job, e.g. a smart plug "
    'that was the "Reading Lamp" in the guest bedroom and is now the '
    '"Aquarium Light" in the living room: sets the device area and name, '
    "rewrites entity name overrides that mention the old name, swaps labels, "
    "and (rename_entity_ids=true, opt-in) rewrites entity_ids containing the "
    "old name - the plan then lists automations/scripts referencing each old "
    "id. Applies step by step and stops at the first failure; check ok, "
    "applied, not_applied and error. " + MUTATING
))
def repurpose_device(
    device: str,
    area: str | None = None,
    name: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    rename_entity_ids: bool = False,
    dry_run: bool = False,
) -> dict:
    return _run(lambda c: c.update.repurpose_device(
        device, rename_entity_ids=rename_entity_ids, dry_run=dry_run, **_given(
            area=area, name=name, add_labels=add_labels, remove_labels=remove_labels,
        ),
    ))


@mcp.tool(description="Create a label (fails if one with that name exists). " + MUTATING)
def create_label(name: str, dry_run: bool = False) -> dict:
    return _run(lambda c: c.create.label(name, dry_run=dry_run))


@mcp.tool(description=(
    "Rename an area and/or assign it to a floor. area and floor are names or "
    'ids; floor="" clears the floor. Area names must be unique after Home '
    "Assistant's normalization (case and spaces ignored), so renaming to an "
    "existing room's name fails - pick a distinct name. " + MUTATING
))
def update_area(
    area: str,
    name: str | None = None,
    floor: str | None = None,
    dry_run: bool = False,
) -> dict:
    return _run(lambda c: c.update.area(area, dry_run=dry_run, **_given(name=name, floor=floor)))


@mcp.tool(description=(
    "Create an area (assign its floor with update_area). Fails if another area's "
    "name matches after Home Assistant's normalization (case and spaces ignored). " + MUTATING
))
def create_area(name: str, dry_run: bool = False) -> dict:
    return _run(lambda c: c.create.area(name, dry_run=dry_run))


@mcp.tool()
def list_domains() -> dict:
    """List entity domains with entity counts (e.g. light: 16, sensor: 73).

    Useful for orienting in the house before filtering with list_entities.
    """
    return _run(lambda c: c.get.domains())


@mcp.tool()
def call_service(
    domain: str,
    service: str,
    service_data: dict | None = None,
    return_response: bool = False,
) -> dict:
    """Call a Home Assistant service.

    domain is the entity domain - the prefix of an entity_id before the first
    "." (e.g. "light" for light.bedroom). service is the service name (e.g.
    "turn_on"). service_data carries the service parameters, including
    "entity_id" to target an entity, e.g. call_service("light", "turn_on",
    {"entity_id": "light.bedroom", "brightness_pct": 50}).

    Set return_response=true for services that return data (e.g.
    weather.get_forecasts, calendar.get_events); Home Assistant rejects them
    otherwise, and rejects it for services that don't return data. Their data
    comes back under "response".

    Registry edits are not services: use update_entity, update_device,
    update_area and create_area/create_label instead (e.g. there is no
    homeassistant.update_area service). Errors include Home Assistant's reason.

    Returns the entity_ids whose state changed directly as a result. The next
    read refreshes the cache, so it reflects the call; slower changes (fades,
    transitions) show up once they land in Home Assistant.
    """
    return _run(lambda c: c.call.service(
        domain, service, return_response=return_response, **checked_service_data(service_data)
    ))
