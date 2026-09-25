"""MCP surface builder: tools generated from operations registries (doc 04).

The MCP SDK is imported inside :func:`build_server`, so ``studio_hays.core``
itself stays importable without it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Annotated, Any

from .base import BaseClient
from .errors import StudioHaysError
from .operations import Operation, Param

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

#: one module exposed on a server: (tool prefix, client, its operations)
Surface = tuple[str, BaseClient, Iterable[Operation]]

_PY_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
}


def build_server(name: str, surfaces: Iterable[Surface]) -> "MCPServer":
    """One MCP server exposing every operation as ``<prefix>_<tool_name>``."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name)
    for prefix, client, operations in surfaces:
        for op in operations:
            if op.handler is not None:
                server.add_tool(
                    _tool_function(op, client),
                    name=f"{prefix}_{op.tool_name}",
                    description=op.doc,
                )
    return server


def _annotation(p: Param) -> Any:
    from pydantic import Field

    py = _PY_TYPES.get(p.schema_type, str)
    return Annotated[py if p.required else py | None, Field(description=p.description or None)]


def _tool_function(op: Operation, client: BaseClient) -> Callable[..., dict[str, Any]]:
    """A keyword-only function whose signature mirrors ``op.params``.

    The SDK derives the tool's input schema from the signature; omitted
    optional arguments are dropped so the handler's own defaults apply.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    handler = op.handler
    assert handler is not None

    def tool(**kwargs: Any) -> dict[str, Any]:
        args = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return handler(client, **args)
        except StudioHaysError as exc:
            raise ToolError(str(exc)) from exc

    params = [
        inspect.Parameter(
            p.name,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if p.required else p.default,
            annotation=_annotation(p),
        )
        for p in op.params
    ]
    tool.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict[str, Any]}
    tool.__name__ = op.name
    tool.__doc__ = op.doc
    return tool
