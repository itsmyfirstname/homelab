"""Home Assistant operations registry (D-006).

Names follow ``<namespace>_<method>`` (checked by the contract suite);
``cli`` gives the doc-04 command path and ``tool`` the MCP tool name.
"""

from ..core import Operation, Param

FRESH = Param("fresh", schema_type="boolean", default=False, description="refresh from Home Assistant first")
DOMAIN = Param("domain", description='entity domain, e.g. "light" or "sensor"')
LIMIT = Param("limit", schema_type="integer", default=100, description="max rows")

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
        "List entities as compact rows (entity_id, state, friendly_name), filtered by domain and/or search.",
        params=(
            DOMAIN,
            Param("search", description="case-insensitive substring of entity_id or friendly name"),
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
        "Get one entity's full state and attributes (use list_entities to find entity_ids).",
        params=(
            Param("entity_id", required=True, positional=True, description='e.g. "light.kitchen"'),
            FRESH,
        ),
        handler=lambda c, **kw: c.get.entity(**kw),
        cli=("get", "entity"),
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
