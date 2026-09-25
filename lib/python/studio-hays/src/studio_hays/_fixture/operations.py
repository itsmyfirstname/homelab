"""Fixture operations registry (D-006).

Operation names follow the ``<namespace>_<method>`` convention so the
contract suite can check registry/Python-API parity mechanically.
"""

from ..core import Operation, Param
from .client import Client

OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "get_widgets",
        "get",
        "widget",
        "List widgets from local state.",
        params=(Param("limit", schema_type="integer", default=100),),
        handler=lambda c: c.get.widgets(),
    ),
    Operation(
        "get_widget",
        "get",
        "widget",
        "Get one widget from local state.",
        params=(
            Param("id", required=True, description="widget id"),
            Param("fresh", schema_type="boolean", default=False),
        ),
        handler=lambda c, **kw: c.get.widget(**kw),
    ),
    Operation(
        "create_widget",
        "create",
        "widget",
        "Create a widget on the remote (write-through).",
        params=(Param("name", required=True, description="widget name"),),
        handler=lambda c, **kw: c.create.widget(**kw),
    ),
    Operation(
        "update_widget",
        "update",
        "widget",
        "Update a widget on the remote (write-through).",
        params=(Param("id", required=True, description="widget id"),),
        handler=lambda c, id, **fields: c.update.widget(id, **fields),
    ),
    Operation(
        "delete_widget",
        "delete",
        "widget",
        "Delete a widget on the remote (write-through).",
        params=(Param("id", required=True, description="widget id"),),
        handler=lambda c, **kw: c.delete.widget(**kw),
    ),
    Operation(
        "call_ping",
        "call",
        "",
        "Ping the remote (no state effect).",
        handler=lambda c: c.call.ping(),
    ),
    Operation(
        "state_refresh",
        "state",
        "",
        "Pull the remote and atomically replace the local snapshot.",
        handler=lambda c: c.state.refresh(),
    ),
    Operation(
        "state_drift",
        "state",
        "",
        "Compare stored snapshot with a fresh pull; returns added/removed/changed.",
        handler=lambda c: c.state.drift(),
    ),
    Operation(
        "state_show",
        "state",
        "",
        "Dump local state (snapshot + managed).",
        params=(Param("id", description="filter to one widget id"),),
        handler=lambda c, **kw: c.state.show(**kw),
    ),
    Operation(
        "state_reset",
        "state",
        "",
        "Delete the local state DB; the next op rebuilds it.",
        handler=lambda c: c.state.reset(),
    ),
    Operation(
        "state_history",
        "state",
        "",
        "Show the last N operations from the audit log.",
        params=(Param("n", schema_type="integer", default=20),),
        handler=lambda c, **kw: c.state.history(**kw),
    ),
)
