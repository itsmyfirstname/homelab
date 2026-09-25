"""studio-hays CLI dispatcher (M1 minimum; full command trees land in M2+).

Deliberately generic: commands are generated from each module's
``OPERATIONS`` registry (D-006), so the CLI works for any module that ships
a registry:

    studio-hays fixture --list-ops
    studio-hays fixture state_refresh
    studio-hays fixture create_widget name=beta
    studio-hays fixture get_widget id=w1

JSON to stdout by default (D-008); errors are one-line JSON on stdout with a
human trace on stderr. Exit codes: 0 ok, 1 error, 2 not found (doc 04).
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from . import __version__
from .core import BaseClient, NotFoundError, Operation, StudioHaysError

#: short name -> (package, client attr, operations attr)
MODULES: dict[str, tuple[str, str, str]] = {
    "fixture": ("studio_hays._fixture", "Client", "OPERATIONS"),
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


def _load(module: str) -> tuple[type[BaseClient], tuple[Operation, ...]]:
    try:
        package, client_attr, ops_attr = MODULES[module]
    except KeyError:
        known = ", ".join(sorted(MODULES))
        raise StudioHaysError(f"unknown module {module!r} (known: {known})")
    mod = importlib.import_module(package)
    return getattr(mod, client_attr), getattr(mod, ops_attr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="studio-hays",
        description="studio-hays: one client per homelab slice (doc 04).",
    )
    parser.add_argument(
        "--version", action="version", version=f"studio-hays {__version__}"
    )
    parser.add_argument("module", nargs="?", help="module name")
    parser.add_argument("op", nargs="?", help="operation name (see --list-ops)")
    parser.add_argument(
        "args", nargs="*", metavar="key=value", help="operation parameters"
    )
    parser.add_argument(
        "--list-ops", action="store_true", help="list a module's operations"
    )
    ns = parser.parse_args(argv)

    if not ns.module:
        parser.print_help()
        print(f"\nmodules: {', '.join(sorted(MODULES))}")
        return 0

    client_cls, operations = _load(ns.module)
    ops_by_name = {op.name: op for op in operations}

    if ns.list_ops or not ns.op:
        print(f"{ns.module}: {len(operations)} operations")
        for op in operations:
            params = ", ".join(
                f"{p.name}{'*' if p.required else ''}" for p in op.params
            )
            sig = f" {params}" if params else ""
            print(f"  {op.name}{sig:<28} {op.doc}")
        return 0

    op = ops_by_name.get(ns.op)
    if op is None or op.handler is None:
        print(
            json.dumps(
                {
                    "error": (
                        f"unknown op {ns.op!r} for {ns.module} "
                        "(see --list-ops)"
                    )
                }
            )
        )
        return 1

    try:
        client = client_cls()
        client.connect()
        result = op.handler(client, **_parse_kv(ns.args))
    except NotFoundError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except StudioHaysError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
