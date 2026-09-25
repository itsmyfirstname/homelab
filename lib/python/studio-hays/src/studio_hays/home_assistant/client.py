"""Home Assistant client (M2, mirror mode).

Entities mirror ``/api/states`` into the snapshot as resource type
``entity``. Reads come from local state, refreshing first when the state was
never synced (errors propagate) or is stale (best-effort: a failed refresh
serves cached data with ``stale: true``). Service calls have no state effect
(Q-010) but invalidate the snapshot so the next read refreshes.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, ClassVar, cast

import httpx

from ..core import (
    BaseClient,
    Key,
    ModuleSpec,
    NotFoundError,
    RemoteError,
    StateNamespace,
)

RESOURCE = "entity"


def _compact(state: Mapping) -> dict:
    return {
        "entity_id": state["entity_id"],
        "state": state.get("state"),
        "friendly_name": (state.get("attributes") or {}).get("friendly_name"),
    }


def _matching(
    client: "Client", domain: str | None, search: str | None
) -> list[dict]:
    """Snapshot entity states filtered by domain prefix and name/id substring."""
    needle = search.lower() if search else None
    out = []
    for row in client.store.get_snapshot(RESOURCE):
        state = row["attributes"]
        eid = state["entity_id"]
        if domain and not eid.startswith(f"{domain}."):
            continue
        if needle:
            name = str((state.get("attributes") or {}).get("friendly_name") or "")
            if needle not in eid.lower() and needle not in name.lower():
                continue
        out.append(state)
    return out


def _listing(
    client: "Client", domain: str | None, search: str | None, limit: int, fresh: bool
) -> dict:
    client._sync(fresh)
    rows = _matching(client, domain, search)
    return client._envelope(total=len(rows), entities=[_compact(s) for s in rows[:limit]])


class _Get:
    def __init__(self, client: "Client") -> None:
        self._c = client

    def domains(self, fresh: bool = False) -> dict:
        c = self._c
        c._sync(fresh)
        counts: dict[str, int] = {}
        for state in _matching(c, None, None):
            domain = state["entity_id"].partition(".")[0]
            counts[domain] = counts.get(domain, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return c._envelope(domains=[{"domain": d, "count": n} for d, n in ranked])

    def entities(
        self,
        domain: str | None = None,
        search: str | None = None,
        limit: int = 100,
        fresh: bool = False,
    ) -> dict:
        return _listing(self._c, domain, search, limit, fresh)

    def entity(self, entity_id: str, fresh: bool = False) -> dict:
        c = self._c
        c._sync(fresh)
        rows = c.store.get_snapshot(RESOURCE, entity_id)
        if not rows:
            raise NotFoundError(
                f"entity not found: {entity_id} — run state.refresh() if it exists on the remote"
            )
        return c._envelope(entity=rows[0]["attributes"])


class _Query:
    """``ha.query("kitchen")`` ≡ ``ha.query.entities("kitchen")``: search local state."""

    def __init__(self, client: "Client") -> None:
        self._c = client

    def entities(self, search: str, domain: str | None = None, limit: int = 100) -> dict:
        return _listing(self._c, domain, search, limit, fresh=False)

    __call__ = entities


class _Call:
    def __init__(self, client: "Client") -> None:
        self._c = client

    def service(self, domain: str, service: str, **data: object) -> dict:
        """Fire a HA service (e.g. ``light.turn_on``); no state effect (Q-010)."""
        c = self._c
        name = f"{domain}.{service}"
        changed = c._called(
            name,
            {"data": data},
            lambda: c._request(
                "POST",
                f"/api/services/{domain}/{service}",
                data,
                missing=f"unknown service {name} (HTTP 404)",
            ),
        )
        c._dirty = True
        return {
            "service": name,
            "changed_entities": [s["entity_id"] for s in changed or []],
        }


class _State(StateNamespace):
    def show(self, id: str | None = None) -> dict:
        """Dump the local entity snapshot (optionally one entity); no network."""
        c = self._c
        rows = c.store.get_snapshot(RESOURCE, id)
        return c._envelope(entities=[r["attributes"] for r in rows])


class Client(BaseClient):
    """Client for one Home Assistant instance."""

    module = "home_assistant"
    spec = ModuleSpec(
        module="home_assistant",
        env_segment="HA",
        keys=(Key("url", required=True), Key("token", required=True)),
    )
    identity_fields = {RESOURCE: ["entity_id", "attributes.friendly_name"]}
    staleness_window = 15.0

    #: test hook: route HTTP through this transport (e.g. httpx.MockTransport)
    _transport: ClassVar[httpx.BaseTransport | None] = None
    #: set by service calls: the next read refreshes regardless of staleness
    #: (in-process only; another process's calls surface via staleness)
    _dirty: bool = False
    #: serializes read-time refreshes (MCP runs sync tools on worker threads)
    _sync_lock: ClassVar[threading.Lock] = threading.Lock()

    # ------------------------------------------------------------ transport

    def _open_transport(self) -> httpx.Client:
        return httpx.Client(
            base_url=str(self.config.get("url")).rstrip("/"),
            headers={"Authorization": f"Bearer {self.config.get('token')}"},
            timeout=self.config.timeout,
            transport=self.__class__._transport,
        )

    def _request(
        self, method: str, path: str, json: Any = None, *, missing: str | None = None
    ) -> Any:
        """One HA REST call. A 404 raises ``NotFoundError(missing)`` when given."""
        http = cast(httpx.Client, self.transport)
        try:
            resp = http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise RemoteError(f"cannot reach Home Assistant at {self._host()}: {exc}") from exc
        if resp.status_code == 401:
            raise RemoteError("Home Assistant rejected STUDIOHAYS_HA_TOKEN (HTTP 401)")
        if resp.status_code == 404 and missing is not None:
            raise NotFoundError(missing)
        if not resp.is_success:
            raise RemoteError(
                f"Home Assistant returned HTTP {resp.status_code} for {method} {path}"
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise RemoteError(f"Home Assistant sent invalid JSON for {method} {path}") from exc

    def _pull(self) -> list[tuple[str, str, Mapping]]:
        states = self._request("GET", "/api/states")
        if not isinstance(states, list):
            raise RemoteError(f"unexpected /api/states payload from {self._host()}")
        return [(RESOURCE, str(s["entity_id"]), s) for s in states]

    def _sync(self, fresh: bool = False) -> None:
        """Refresh before a read when needed (see module docstring)."""
        with self._sync_lock:
            store = self.store
            if fresh or store.last_synced is None:
                self.state.refresh()
            elif self._dirty or store.is_stale(self.staleness_window):
                try:
                    self.state.refresh()
                except RemoteError:
                    return
            self._dirty = False

    def _envelope(self, **payload: object) -> dict:
        env = super()._envelope(**payload)
        env["stale"] = env["stale"] or self._dirty
        return env

    # ----------------------------------------------------------- namespaces

    @property
    def get(self) -> _Get:
        return _Get(self)

    @property
    def query(self) -> _Query:
        return _Query(self)

    @property
    def call(self) -> _Call:
        return _Call(self)

    @property
    def state(self) -> _State:
        return _State(self)
