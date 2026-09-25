"""Home Assistant operations registry (D-006).

Names follow ``<namespace>_<method>`` (checked by the contract suite);
``cli`` gives the doc-04 command path and ``tool`` the MCP tool name.
"""

from ..core import Operation, Param

FRESH = Param("fresh", schema_type="boolean", default=False, description="refresh from Home Assistant first")
DOMAIN = Param("domain", description='entity domain, e.g. "light" or "sensor"')
LIMIT = Param("limit", schema_type="integer", default=100, description="max rows")
AREA = Param("area", description="area name or id; matches the entity's own area, else its device's")
LABEL = Param("label", description="label name or id, on the entity or its device")
DEVICE = Param(
    "device", required=True, positional=True,
    description="device name, id, or one of its entity_ids",
)

DRY_RUN = Param("dry_run", schema_type="boolean", default=False, description="return the planned change without applying it")
NAME = Param("name", description='new name; "" clears it (inherit again)')
MOVE_AREA = Param("area", description='area name or id; "" clears the assignment')
ADD_LABELS = Param("add_labels", schema_type="array", description="label names or ids to add")
REMOVE_LABELS = Param("remove_labels", schema_type="array", description="label names or ids to remove")
MUTATING = "Mutating: changes Home Assistant's registry (use dry_run=true to preview)."

OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "get_domains",
        "get",
        "domain",
        "List entity domains with entity counts, largest first (orient before filtering).",
        params=(FRESH,),
        handler=lambda c, **kw: c.get.domains(**kw),
        cli=("list", "domains"),
        tool="list_domains",
    ),
    Operation(
        "get_entities",
        "get",
        "entity",
        "List entities as compact rows (entity_id, state, friendly_name), filtered by domain, search, area and/or label.",
        params=(
            DOMAIN,
            Param("search", description="case-insensitive substring of entity_id or friendly name"),
            AREA,
            LABEL,
            LIMIT,
            FRESH,
        ),
        handler=lambda c, **kw: c.get.entities(**kw),
        cli=("list", "entities"),
        tool="list_entities",
    ),
    Operation(
        "get_entity",
        "get",
        "entity",
        "Get one entity's full state and attributes plus its registry info: area, floor, device, labels.",
        params=(
            Param("entity_id", required=True, positional=True, description='e.g. "light.kitchen"'),
            FRESH,
        ),
        handler=lambda c, **kw: c.get.entity(**kw),
        cli=("get", "entity"),
    ),
    Operation(
        "get_areas",
        "get",
        "area",
        "List areas with their floor and device/entity counts.",
        params=(FRESH,),
        handler=lambda c, **kw: c.get.areas(**kw),
        cli=("list", "areas"),
        tool="list_areas",
    ),
    Operation(
        "get_labels",
        "get",
        "label",
        "List labels with how many devices and entities carry each.",
        params=(FRESH,),
        handler=lambda c, **kw: c.get.labels(**kw),
        cli=("list", "labels"),
        tool="list_labels",
    ),
    Operation(
        "get_device",
        "get",
        "device",
        "Get a device (by name, id, or one of its entity_ids) with its area, labels and entities.",
        params=(DEVICE, FRESH),
        handler=lambda c, **kw: c.get.device(**kw),
        cli=("get", "device"),
    ),
    Operation(
        "query_entities",
        "query",
        "entity",
        "Search entities by id or friendly name (local state, refreshed first when stale).",
        params=(
            Param("search", required=True, positional=True, description="case-insensitive text"),
            DOMAIN,
            LIMIT,
        ),
        handler=lambda c, **kw: c.query.entities(**kw),
        cli=("query",),
        tool="query",
    ),
    Operation(
        "call_service",
        "call",
        "",
        'Call a Home Assistant service, e.g. domain="light", service="turn_on", '
        'data={"entity_id": "light.kitchen"}. Mutating: acts on the house.',
        params=(
            Param("domain", required=True, positional=True, description='service domain, e.g. "light"'),
            Param("service", required=True, positional=True, description='service name, e.g. "turn_on"'),
            Param("data", schema_type="object", description="service data, including entity_id"),
        ),
        handler=lambda c, domain, service, data=None: c.call.service(domain, service, **(data or {})),
        cli=("call",),
    ),
    Operation(
        "update_entity",
        "update",
        "entity",
        "Rename an entity, move it to an area, add/remove labels, or change its entity_id. " + MUTATING,
        params=(
            Param("entity_id", required=True, positional=True, description='e.g. "switch.reading_lamp"'),
            NAME,
            MOVE_AREA,
            ADD_LABELS,
            REMOVE_LABELS,
            Param("new_entity_id", description="new entity_id, same domain (breaks automations using the old one)"),
            DRY_RUN,
        ),
        handler=lambda c, **kw: c.update.entity(**kw),
        cli=("update", "entity"),
    ),
    Operation(
        "update_device",
        "update",
        "device",
        "Rename a device, move it (and entities inheriting its area) to an area, add/remove labels. " + MUTATING,
        params=(DEVICE, NAME, MOVE_AREA, ADD_LABELS, REMOVE_LABELS, DRY_RUN),
        handler=lambda c, **kw: c.update.device(**kw),
        cli=("update", "device"),
    ),
    Operation(
        "update_repurpose_device",
        "update",
        "device",
        "Repurpose a device (e.g. a plug moved rooms with a new job): move its area, rename it and the "
        "entity name overrides that mention the old name, swap labels, optionally rewrite entity_ids "
        "(the plan lists automations/scripts that reference them). Runs step by step and stops at the "
        "first failure; check ok/applied/not_applied. " + MUTATING,
        params=(
            DEVICE,
            MOVE_AREA,
            Param("name", description="the device's new name"),
            ADD_LABELS,
            REMOVE_LABELS,
            Param("rename_entity_ids", schema_type="boolean", default=False,
                  description="also rewrite entity_ids containing the old name (breaks references)"),
            DRY_RUN,
        ),
        handler=lambda c, **kw: c.update.repurpose_device(**kw),
        cli=("repurpose",),
        tool="repurpose_device",
    ),
    Operation(
        "create_label",
        "create",
        "label",
        "Create a label. " + MUTATING,
        params=(Param("name", required=True, positional=True, description="label name"), DRY_RUN),
        handler=lambda c, **kw: c.create.label(**kw),
        cli=("create", "label"),
    ),
    Operation(
        "create_area",
        "create",
        "area",
        "Create an area (no floor assignment). " + MUTATING,
        params=(Param("name", required=True, positional=True, description="area name"), DRY_RUN),
        handler=lambda c, **kw: c.create.area(**kw),
        cli=("create", "area"),
    ),
    Operation(
        "state_refresh",
        "state",
        "",
        "Pull all entity states from Home Assistant into local state.",
        handler=lambda c: c.state.refresh(),
        cli=("state", "refresh"),
        tool="refresh",
    ),
    Operation(
        "state_drift",
        "state",
        "",
        "Compare local state with a fresh pull: added/removed entities and renamed friendly names.",
        handler=lambda c: c.state.drift(),
        cli=("state", "drift"),
        tool="drift",
    ),
    Operation(
        "state_show",
        "state",
        "",
        "Dump the local entity snapshot, optionally one entity (no network).",
        params=(Param("id", description="entity_id to show"),),
        handler=lambda c, **kw: c.state.show(**kw),
        cli=("state", "show"),
        tool="show",
    ),
    Operation(
        "state_reset",
        "state",
        "",
        "Delete the local state DB; the next read rebuilds it from Home Assistant.",
        handler=lambda c: c.state.reset(),
        cli=("state", "reset"),
        tool="reset",
    ),
    Operation(
        "state_history",
        "state",
        "",
        "Show the last N operations from the audit log.",
        params=(Param("n", schema_type="integer", default=20, positional=True, description="how many"),),
        handler=lambda c, **kw: c.state.history(**kw),
        cli=("state", "history"),
        tool="history",
    ),
)
