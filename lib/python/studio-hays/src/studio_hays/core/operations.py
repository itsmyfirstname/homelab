"""The operations registry (D-006, doc 03).

Every public operation of a module client is declared once as an
:class:`Operation` in ``studio_hays/<module>/operations.py``. The CLI and
MCP surfaces are generated from these declarations, and the contract test
suite asserts parity with the Python API.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Param:
    """One parameter of an operation.

    ``schema_type`` is a JSON-schema primitive name ("string", "integer",
    "number", "boolean", "object") used to generate CLI flags and MCP tool
    input schemas.
    """

    name: str
    schema_type: str = "string"
    required: bool = False
    default: object = None
    description: str = ""
    #: CLI only: taken positionally on the ``cli`` path (``ha get entity ID``)
    positional: bool = False


@dataclass(frozen=True)
class Operation:
    """A single public operation of a module client.

    ``handler`` is a callable ``(client, **kwargs) -> JSON-serializable``.
    """

    name: str  # "get_widget", "state_drift"
    verb: str  # "get" | "query" | "create" | "update" | "delete" | "state" | "call"
    resource: str  # "widget" | "" for state verbs
    doc: str
    params: tuple[Param, ...] = ()
    handler: Callable | None = None
    #: CLI words that reach this op, e.g. ``("list", "entities")`` (doc 04);
    #: the op name itself is always accepted too
    cli: tuple[str, ...] = ()
    #: MCP tool name (without module prefix) when it differs from ``name``
    tool: str = ""

    @property
    def tool_name(self) -> str:
        return self.tool or self.name

    def to_schema(self, prefix: str) -> dict:
        """MCP tool description: ``{name, description, inputSchema}``."""
        properties: dict[str, dict] = {}
        required: list[str] = []
        for p in self.params:
            prop: dict[str, object] = {"type": p.schema_type}
            if p.description:
                prop["description"] = p.description
            if p.default is not None:
                prop["default"] = p.default
            properties[p.name] = prop
            if p.required:
                required.append(p.name)
        return {
            "name": f"{prefix}_{self.tool_name}",
            "description": self.doc,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }
