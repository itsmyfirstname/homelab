"""Fixture client: namespaced verbs over an in-memory widget API.

Mirrors the shape a real module will have: a ``Client(BaseClient)`` with
``get`` / ``create`` / ``update`` / ``delete`` / ``call`` / ``state``
namespaces, write-through CRUD, and a mirror-mode state layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from ..core import (
    BaseClient,
    FakeRemote,
    Key,
    ModuleSpec,
    NotFoundError,
    RemoteError,
    StateNamespace,
)

RESOURCE = "widget"


class _RemoteNotFound(Exception):
    """Raised by FixtureRemote; mapped to NotFoundError at the boundary."""


class FixtureRemote(FakeRemote):
    """In-memory widget API.

    GET/POST ``/api/widgets``, PUT/DELETE ``/api/widgets/{id}``, GET
    ``/api/ping``.
    """

    def __init__(self, seed: Iterable[Mapping] | None = None) -> None:
        super().__init__()
        for w in seed or ():
            self.store[str(w["id"])] = dict(w)
        self.route("GET", "/api/widgets", self._list)
        self.route("GET", "/api/widgets/{id}", self._get)
        self.route("POST", "/api/widgets", self._create)
        self.route("PUT", "/api/widgets/{id}", self._update)
        self.route("DELETE", "/api/widgets/{id}", self._delete)
        self.route("GET", "/api/ping", self._ping)

    def _list(self, json: Any = None) -> list[dict]:
        return sorted(self.store.values(), key=lambda w: w["id"])

    def _get(self, json: Any = None, id: str = "") -> dict:
        if id not in self.store:
            raise _RemoteNotFound(id)
        return dict(self.store[id])

    def _create(self, json: Any = None) -> dict:
        data = dict(json or {})
        n = len(self.store) + 1
        widget = {"id": f"w{n}", "name": str(data.get("name", f"widget-{n}"))}
        self.store[widget["id"]] = widget
        return dict(widget)

    def _update(self, json: Any = None, id: str = "") -> dict:
        if id not in self.store:
            raise _RemoteNotFound(id)
        self.store[id].update(json or {})
        return dict(self.store[id])

    def _delete(self, json: Any = None, id: str = "") -> dict:
        if id not in self.store:
            raise _RemoteNotFound(id)
        return self.store.pop(id)

    def _ping(self, json: Any = None) -> dict:
        return {"pong": True}


class _Get:
    def __init__(self, client: "Client") -> None:
        self._c = client

    def widgets(self, limit: int = 100) -> dict:
        c = self._c
        rows = c.store.get_snapshot(RESOURCE)[:limit]
        return c._envelope(widgets=[r["attributes"] for r in rows])

    def widget(self, id: str, fresh: bool = False) -> dict:
        c = self._c
        if fresh:
            c.state.refresh()
        rows = c.store.get_snapshot(RESOURCE, id)
        if not rows:
            raise NotFoundError(
                f"widget not found: {id} (run state.refresh if it exists remotely)"
            )
        return c._envelope(widget=rows[0]["attributes"])


class _Create:
    def __init__(self, client: "Client") -> None:
        self._c = client

    def widget(self, name: str) -> dict:
        c = self._c
        desired = {"name": name}
        return c._created(
            RESOURCE, desired, lambda: c._request("POST", "/api/widgets", desired)
        )


class _Update:
    def __init__(self, client: "Client") -> None:
        self._c = client

    def widget(self, id: str, **fields: object) -> dict:
        c = self._c
        return c._updated(
            RESOURCE, id, fields, lambda: c._request("PUT", f"/api/widgets/{id}", fields)
        )


class _Delete:
    def __init__(self, client: "Client") -> None:
        self._c = client

    def widget(self, id: str) -> dict:
        c = self._c
        return c._deleted(
            RESOURCE, id, lambda: c._request("DELETE", f"/api/widgets/{id}")
        )


class _Call:
    def __init__(self, client: "Client") -> None:
        self._c = client

    def ping(self) -> dict:
        c = self._c
        return c._called("ping", {}, lambda: c._request("GET", "/api/ping"))


class _State(StateNamespace):
    def show(self, id: str | None = None) -> dict:
        c = self._c
        rows = c.store.get_snapshot(RESOURCE, id)
        return c._envelope(
            widgets=[r["attributes"] for r in rows],
            managed=c.store.get_managed(RESOURCE),
        )


class Client(BaseClient):
    """Client for the toy widget API (the contract-suite stand-in)."""

    module = "fixture"
    spec = ModuleSpec(
        module="fixture",
        env_segment="FIX",
        keys=(
            Key("url", default="http://fake.local"),
            Key("token", default="fake-token"),
        ),
    )
    identity_fields = {RESOURCE: ["name"]}

    #: test hook — point the client at a seeded fake remote
    _fake: ClassVar[FakeRemote | None] = None

    # ------------------------------------------------------------ transport

    def _open_transport(self) -> object:
        fake = self.__class__._fake
        return fake if fake is not None else FixtureRemote()

    def _request(self, method: str, path: str, json: Any = None) -> Any:
        try:
            return self.transport.request(method, path, json)  # type: ignore[union-attr]
        except _RemoteNotFound as exc:
            raise NotFoundError(f"widget not found on remote: {exc}") from exc
        except LookupError as exc:
            raise RemoteError(str(exc)) from exc

    def _pull(self) -> list[tuple[str, str, Mapping]]:
        rows = self._request("GET", "/api/widgets")
        return [(RESOURCE, str(r["id"]), r) for r in rows]

    # ----------------------------------------------------------- namespaces

    @property
    def get(self) -> _Get:
        return _Get(self)

    @property
    def create(self) -> _Create:
        return _Create(self)

    @property
    def update(self) -> _Update:
        return _Update(self)

    @property
    def delete(self) -> _Delete:
        return _Delete(self)

    @property
    def call(self) -> _Call:
        return _Call(self)

    @property
    def state(self) -> _State:
        return _State(self)
