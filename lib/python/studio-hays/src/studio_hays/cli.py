"""studio-hays CLI dispatcher (doc 04, D-008).

Commands are generated from each module's ``OPERATIONS`` registry (D-006).
Every operation is reachable two ways:

    studio-hays ha list entities --domain light     # Operation.cli path
    studio-hays ha get_entities domain=light        # op name + key=value

JSON to stdout by default; errors are one-line JSON on stdout. Exit codes:
0 ok, 1 error, 2 not found (doc 04).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable

from . import __version__
from .core import BaseClient, NotFoundError, Operation, Param, StudioHaysError

#: CLI name -> (package, client attr, operations attr)
MODULES: dict[str, tuple[str, str, str]] = {
    "fixture": ("studio_hays._fixture", "Client", "OPERATIONS"),
    "ha": ("studio_hays.home_assistant", "Client", "OPERATIONS"),
}
#: package-style names accepted wherever a CLI name is
ALIASES: dict[str, str] = {"home_assistant": "ha"}

#: argparse converters; "object" stays a string and is JSON-parsed in _run
#: so bad JSON is a one-line JSON error (exit 1), not an argparse usage exit
_TYPES: dict[str, Callable[[str], object]] = {
    "integer": int,
    "number": float,
    "string": str,
}


def _coerce(raw: str) -> object:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _parse_kv(pairs: list[str]) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    for pair in pairs:
        if "=" not in pair:
            raise StudioHaysError(f"expected key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        kwargs[key.strip()] = _coerce(raw.strip())
    return kwargs


def _json_param(name: str, raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise StudioHaysError(f"--{name.replace('_', '-')} is not valid JSON: {exc}") from exc


def resolve_module(name: str) -> str:
    """CLI name for ``name`` (a CLI name or package alias)."""
    cli_name = ALIASES.get(name, name)
    if cli_name not in MODULES:
        known = ", ".join(sorted(MODULES))
        raise StudioHaysError(f"unknown module {name!r} (known: {known})")
    return cli_name


def _load(module: str) -> tuple[type[BaseClient], tuple[Operation, ...]]:
    package, client_attr, ops_attr = MODULES[resolve_module(module)]
    mod = importlib.import_module(package)
    return getattr(mod, client_attr), getattr(mod, ops_attr)


def _add_param(parser: argparse.ArgumentParser, p: Param, positional: bool) -> None:
    if positional:
        parser.add_argument(
            p.name,
            type=_TYPES.get(p.schema_type, str),
            nargs=None if p.required else "?",
            default=None,
            help=p.description,
        )
    elif p.schema_type == "boolean":
        parser.add_argument(
            f"--{p.name.replace('_', '-')}", dest=p.name, action="store_true",
            default=None, help=p.description,
        )
    else:
        parser.add_argument(
            f"--{p.name.replace('_', '-')}", dest=p.name,
            type=_TYPES.get(p.schema_type, str), default=None, help=p.description,
        )


def _module_parser(module: str, operations: tuple[Operation, ...]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"studio-hays {module}")
    parser.add_argument("--list-ops", action="store_true", help="list operations")
    top = parser.add_subparsers(dest="_command", metavar="command")
    groups: dict[str, argparse._SubParsersAction] = {}

    def leaf(words: tuple[str, ...], op: Operation, by_name: bool) -> None:
        subs = top
        for word in words[:-1]:
            if word not in groups:
                group = top.add_parser(word, help=f"{word} ...")
                groups[word] = group.add_subparsers(dest="_sub", metavar="resource", required=True)
            subs = groups[word]
        p = subs.add_parser(words[-1], help=op.doc, description=op.doc)
        for param in op.params:
            _add_param(p, param, positional=param.positional and not by_name)
        if by_name:
            p.add_argument("_kv", nargs="*", metavar="key=value")
        p.set_defaults(_op=op)

    for op in operations:
        if op.handler is None:
            continue
        leaf((op.name,), op, by_name=True)
        if op.cli:
            leaf(op.cli, op, by_name=False)
    return parser


def _list_ops(module: str, operations: tuple[Operation, ...]) -> None:
    print(f"{module}: {len(operations)} operations")
    for op in operations:
        path = " ".join(op.cli) if op.cli else op.name
        print(f"  {path:<24} {op.doc}")


def _serve_mcp(modules: list[str]) -> int:
    """Serve the given modules' tools over stdio (logging goes to stderr)."""
    import logging

    from .core.mcp import build_server

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        surfaces = []
        for name in modules:
            cli_name = resolve_module(name)
            client_cls, operations = _load(cli_name)
            client = client_cls()
            client.connect()  # fail the handshake with a readable error
            surfaces.append((cli_name, client, operations))
    except StudioHaysError as exc:
        print(json.dumps({"error": str(exc)}))
        print(f"studio-hays mcp: {exc}", file=sys.stderr)
        return 1
    name = f"studio-hays.{surfaces[0][1].module}" if len(surfaces) == 1 else "studio-hays"
    build_server(name, surfaces).run()
    return 0


def _run(module: str, argv: list[str]) -> int:
    if argv[:1] == ["mcp"]:
        return _serve_mcp([module])
    client_cls, operations = _load(module)
    ns = _module_parser(module, operations).parse_args(argv)
    op: Operation | None = getattr(ns, "_op", None)
    if ns.list_ops or op is None:
        _list_ops(module, operations)
        return 0
    assert op.handler is not None
    kwargs = {p.name: getattr(ns, p.name) for p in op.params if getattr(ns, p.name, None) is not None}
    try:
        for p in op.params:
            if p.schema_type == "object" and isinstance(kwargs.get(p.name), str):
                kwargs[p.name] = _json_param(p.name, kwargs[p.name])
        kwargs.update(_parse_kv(getattr(ns, "_kv", None) or []))
        client = client_cls()
        client.connect()
        result = op.handler(client, **kwargs)
    except NotFoundError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except StudioHaysError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-"):
        parser = argparse.ArgumentParser(
            prog="studio-hays",
            description="studio-hays: one client per homelab slice (doc 04).",
        )
        parser.add_argument("--version", action="version", version=f"studio-hays {__version__}")
        parser.add_argument("module", nargs="?", help=f"one of: {', '.join(sorted(MODULES))}")
        parser.parse_args(argv)
        parser.print_help()
        return 0
    if argv[0] == "mcp":
        parser = argparse.ArgumentParser(prog="studio-hays mcp")
        parser.add_argument(
            "--modules", required=True, help="comma-separated, e.g. home_assistant,proxmox"
        )
        ns = parser.parse_args(argv[1:])
        return _serve_mcp([m.strip() for m in ns.modules.split(",") if m.strip()])
    try:
        module = resolve_module(argv[0])
    except StudioHaysError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    return _run(module, argv[1:])


if __name__ == "__main__":
    sys.exit(main())
