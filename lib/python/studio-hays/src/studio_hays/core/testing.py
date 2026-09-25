"""Test kit: FakeRemote and contract-suite helpers (doc 01, ``testing.py``).

``FakeRemote`` is a minimal in-memory "remote API server" with a route table,
so a module's transport can round-trip CRUD without a network. The contract
suite (``tests/test_contract.py``) runs the same assertions against every
module client.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping


class FakeRemote:
    """In-memory stand-in for a remote API server.

    Routes are ``(METHOD, path_pattern)`` where ``{param}`` segments are
    captured, e.g. ``("GET", "/api/widgets/{id}")``.
    """

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}  # resource id -> object
        self.routes: dict[tuple[str, str], Callable] = {}
        self.calls: list[tuple[str, str, object]] = []  # (method, path, json)
        self.fail_next: Exception | None = None

    def route(
        self, method: str, pattern: str, handler: Callable
    ) -> None:
        self.routes[(method.upper(), pattern)] = handler

    def request(
        self, method: str, path: str, json: object = None
    ) -> object:
        self.calls.append((method.upper(), path, json))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        for (m, pattern), handler in self.routes.items():
            if m != method.upper():
                continue
            params = self._match(pattern, path)
            if params is not None:
                return handler(json, **params)
        raise LookupError(f"FakeRemote: no route for {method.upper()} {path}")

    @staticmethod
    def _match(pattern: str, path: str) -> dict[str, str] | None:
        pat_parts = pattern.strip("/").split("/")
        path_parts = path.strip("/").split("/")
        if len(pat_parts) != len(path_parts):
            return None
        params: dict[str, str] = {}
        for p, a in zip(pat_parts, path_parts):
            if p.startswith("{") and p.endswith("}"):
                params[p[1:-1]] = a
            elif p != a:
                return None
        return params

    def reset(self) -> None:
        self.store.clear()
        self.calls.clear()
        self.fail_next = None


def contract_suite(client_cls: type) -> Iterable[str]:
    """Names of the contract checks every module client must pass.

    The actual tests live in ``tests/test_contract.py`` and are parametrized
    over module clients; this list is the contract's table of contents.
    """
    del client_cls  # the suite is uniform across modules
    return (
        "lazy_construction",
        "singleton_identity",
        "env_config",
        "kwargs_override_env",
        "crud_write_through",
        "refresh_rebuilds",
        "drift_detection",
        "registry_parity",
    )


def _as_dict(obj: object) -> Mapping:
    return obj if isinstance(obj, Mapping) else obj  # type: ignore[return-value]
