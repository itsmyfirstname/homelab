"""Home Assistant client (M2, mirror mode).

Entities mirror ``/api/states`` into the snapshot as resource type
``entity``; the entity/device/area/label/floor registries (WebSocket API)
mirror as ``registry_entry`` (keyed by the stable registry id), ``device``,
``area``, ``label`` and ``floor``. Reads come from local state, refreshing first when the state was
never synced (errors propagate) or is stale (best-effort: a failed refresh
serves cached data with ``stale: true``). Service calls have no state effect
(Q-010) but invalidate the snapshot so the next read refreshes.
``call.set_state`` is the exception: it write-throughs a simulated state.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, ClassVar, cast

import httpx

from . import ws
from .automations import get_automation, list_automations
from .scripts import get_script, list_scripts
from .registry import Registry, device_name
from .writes import Create, Delete, Update, changes, predicted
from ..core import (
    BaseClient,
    Key,
    ModuleSpec,
    NotFoundError,
    RemoteError,
    StateNamespace,
    StudioHaysError,
)

RESOURCE = "entity"

#: (resource type, WS list command, id field) pulled on every refresh
REGISTRIES = (
    ("registry_entry", "config/entity_registry/list", "id"),
    ("device", "config/device_registry/list", "id"),
    ("area", "config/area_registry/list", "area_id"),
    ("label", "config/label_registry/list", "label_id"),
    ("floor", "config/floor_registry/list", "floor_id"),
)

_ID_FIELDS = {kind: id_field for kind, _, id_field in REGISTRIES}


REASON_LIMIT = 300


class HTTPStatusError(RemoteError):
    """A non-2xx REST reply; ``status`` lets callers refine the error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def service_data(data: Mapping[str, object] | None) -> dict[str, object]:
    """Service data for ``call.service(**data)``; ``return_response`` is a call
    option (it selects ``?return_response``), not a data key."""
    data = dict(data or {})
    if "return_response" in data:
        raise StudioHaysError(
            "return_response is a call option, not service data — pass it as its own argument"
        )
    return data


def _reason(resp: httpx.Response) -> str:
    """HA's explanation of a failed request: its JSON ``message``, else the body."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    text = body.get("message") if isinstance(body, dict) and body.get("message") else resp.text
    text = " ".join(str(text or "").split())
    return text if len(text) <= REASON_LIMIT else text[: REASON_LIMIT - 1] + "…"


def _compact(state: Mapping) -> dict:
    return {
        "entity_id": state["entity_id"],
        "state": state.get("state"),
        "friendly_name": (state.get("attributes") or {}).get("friendly_name"),
    }


def _matching(
    client: "Client",
    domain: str | None,
    search: str | None,
    area: str | None = None,
    label: str | None = None,
) -> list[dict]:
    """Entity states filtered by domain prefix, name/id substring, effective
    area and label (on the entity or its device)."""
    needle = search.lower() if search else None
    reg = Registry(client.store) if area or label else None
    area_id = reg.area(area)["area_id"] if reg and area else None
    label_id = reg.label(label)["label_id"] if reg and label else None
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
        if reg is not None:
            if area_id and reg.area_of(eid)[0] != area_id:
                continue
            if label_id and label_id not in reg.labels_of(eid):
                continue
        out.append(state)
    return out


def _listing(
    client: "Client",
    domain: str | None,
    search: str | None,
    limit: int,
    fresh: bool,
    area: str | None = None,
    label: str | None = None,
) -> dict:
    client._sync(fresh)
    rows = _matching(client, domain, search, area, label)
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
        area: str | None = None,
        label: str | None = None,
        limit: int = 100,
        fresh: bool = False,
    ) -> dict:
        return _listing(self._c, domain, search, limit, fresh, area, label)

    def entity(self, entity_id: str, fresh: bool = False) -> dict:
        c = self._c
        c._sync(fresh)
        rows = c.store.get_snapshot(RESOURCE, entity_id)
        if not rows:
            raise NotFoundError(
                f"entity not found: {entity_id} — run state.refresh() if it exists on the remote"
            )
        return c._envelope(
            entity=rows[0]["attributes"], registry=Registry(c.store).entity_info(entity_id)
        )

    def areas(self, fresh: bool = False) -> dict:
        """Areas with their floor and device/entity counts.

        ``entity_count`` counts entities that have a state (what
        ``entities(area=...)`` returns); ``registry_entity_count`` also counts
        disabled and stateless registry entries. Both use the effective area.
        """
        c = self._c
        c._sync(fresh)
        reg = Registry(c.store)
        with_state = c._entity_ids()
        registry_areas = [reg.area_of(eid)[0] for eid in reg.entries]
        rows = [
            {
                "area_id": a["area_id"],
                "name": a["name"],
                "floor": (reg.floor_ref(a["area_id"]) or {}).get("name"),
                "device_count": sum(d.get("area_id") == a["area_id"] for d in reg.devices.values()),
                "entity_count": reg.entity_count(a["area_id"], with_state),
                "registry_entity_count": registry_areas.count(a["area_id"]),
            }
            for a in sorted(reg.areas.values(), key=lambda a: a["name"].casefold())
        ]
        return c._envelope(areas=rows)

    def labels(self, fresh: bool = False) -> dict:
        """Labels with how many devices and entities carry each directly."""
        c = self._c
        c._sync(fresh)
        reg = Registry(c.store)
        rows = [
            {
                "label_id": x["label_id"],
                "name": x["name"],
                "device_count": sum(x["label_id"] in (d.get("labels") or []) for d in reg.devices.values()),
                "entity_count": sum(x["label_id"] in (e.get("labels") or []) for e in reg.entries.values()),
            }
            for x in sorted(reg.labels.values(), key=lambda x: x["name"].casefold())
        ]
        return c._envelope(labels=rows)

    def automations(self, fresh: bool = False) -> dict:
        """Automations, from their automation.* entities (HA has no list API)."""
        return list_automations(self._c, fresh)

    def automation(self, automation_id: str) -> dict:
        """One automation's stored config (verbatim, as in automations.yaml)."""
        return get_automation(self._c, automation_id)

    def scripts(self, fresh: bool = False) -> dict:
        """Scripts, from their script.* entities (HA has no list API)."""
        return list_scripts(self._c, fresh)

    def script(self, script_id: str) -> dict:
        """One script's stored config (verbatim, as in scripts.yaml)."""
        return get_script(self._c, script_id)

    def device(self, device: str, fresh: bool = False) -> dict:
        """A device (by name, id or one of its entity_ids) and its entities."""
        c = self._c
        c._sync(fresh)
        reg = Registry(c.store)
        d = reg.device(device)
        states = {r["id"]: r["attributes"] for r in c.store.get_snapshot(RESOURCE)}
        entities = []
        for eid in reg.entities_of(d["id"]):
            entry = reg.entries[eid]
            row = _compact(states[eid]) if eid in states else {"entity_id": eid, "state": None, "friendly_name": None}
            entities.append({
                **row,
                "name_override": entry.get("name"),
                "area_override": reg.area_ref(entry.get("area_id")),
                "labels": reg.label_refs(entry.get("labels") or []),
            })
        return c._envelope(device=_device_view(reg, d), entities=entities)


def _device_view(reg: Registry, d: Mapping) -> dict:
    return {
        "id": d["id"],
        "name": device_name(d),
        "name_by_user": d.get("name_by_user"),
        "integration_name": d.get("name"),
        "manufacturer": d.get("manufacturer"),
        "model": d.get("model"),
        "area": reg.area_ref(d.get("area_id")),
        "floor": reg.floor_ref(d.get("area_id")),
        "labels": reg.label_refs(d.get("labels") or []),
    }


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

    def service(
        self, domain: str, service: str, return_response: bool = False, **data: object
    ) -> dict:
        """Fire a HA service (e.g. ``light.turn_on``); no state effect (Q-010).

        ``return_response=True`` is required by response-only services (e.g.
        ``weather.get_forecasts``) and returns their data under ``response``.
        """
        c = self._c
        name = f"{domain}.{service}"
        targets = _targets(c, domain, data)
        reply = c._called(
            name,
            {"data": data, **({"return_response": True} if return_response else {})},
            lambda: c._post_service(domain, service, data, return_response),
        )
        c._dirty = True
        if isinstance(reply, dict):
            changed, response = reply.get("changed_states") or [], reply.get("service_response")
        else:
            changed, response = reply or [], None
        changed_ids = [s["entity_id"] for s in changed]
        result: dict[str, Any] = {
            "service": name,
            "targets": targets,
            "changed_entities": changed_ids,
        }
        pending = [t for t in targets if t not in changed_ids]
        if pending and not return_response:
            result["note"] = (
                f"no state change reported yet for {', '.join(pending)}: Home Assistant only "
                "lists states that changed before the call returned, and some integrations "
                "(e.g. Lutron, transitions) apply changes afterwards — verify with get_entity "
                "rather than treating this as a failure"
            )
        if return_response:
            result["response"] = response
        return result

    def set_state(
        self,
        entity_id: str,
        state: str,
        attributes: Mapping[str, object] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Simulate an entity's state (does not command the device).

        POSTs ``/api/states/<entity_id>``. The value lasts until the
        integration reports again; ``state_changed`` still fires, so
        state-based automations run. Omitted ``attributes`` keeps the
        current ones; a passed dict replaces them.
        """
        c = self._c
        with c._sync_lock:
            c._sync()
            rows = c.store.get_snapshot(RESOURCE, entity_id)
            if not rows:
                raise NotFoundError(
                    f"entity not found: {entity_id} — run state.refresh() if it exists on the remote"
                )
            before = rows[0]["attributes"]
            new_attrs = dict(attributes) if attributes is not None else dict(before.get("attributes") or {})
            predicted_state = {**before, "state": str(state), "attributes": new_attrs}
            result: dict[str, Any] = {
                "entity_id": entity_id,
                "dry_run": dry_run,
                "before": before,
                "after": predicted_state,
            }
            if dry_run:
                return result

            def send() -> dict:
                return c._request(
                    "POST",
                    f"/api/states/{entity_id}",
                    {"state": state, "attributes": new_attrs},
                )

            result["after"] = c._updated(
                RESOURCE, entity_id, {"state": str(state), "attributes": new_attrs}, send
            )
            c._dirty = True
            return result


def _ids(value: object) -> list[str]:
    """Ids from a service-call target field ("a, b" strings and lists alike)."""
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _targets(c: "Client", domain: str, data: Mapping[str, object]) -> list[str]:
    """entity_ids a service call targets: explicit ``entity_id`` plus entities
    reached through ``area_id`` / ``device_id`` / ``label_id`` (limited to the
    service's domain, as HA does, except for the generic ``homeassistant``)."""
    targets = _ids(data.get("entity_id"))
    areas, devices, labels = (_ids(data.get(k)) for k in ("area_id", "device_id", "label_id"))
    if not (areas or devices or labels):
        return targets
    c._sync()
    reg = Registry(c.store)
    with_state = c._entity_ids()
    for eid in sorted(reg.entries):
        if eid not in with_state or eid in targets:
            continue
        if domain != "homeassistant" and not eid.startswith(f"{domain}."):
            continue
        entry = reg.entries[eid]
        if (
            reg.area_of(eid)[0] in areas
            or entry.get("device_id") in devices
            or reg.labels_of(eid) & set(labels)
        ):
            targets.append(eid)
    return targets


class _State(StateNamespace):
    def refresh(self) -> dict:
        result = super().refresh()
        self._c._dirty = False  # type: ignore[attr-defined]
        return result

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
    identity_fields = {
        RESOURCE: ["entity_id", "attributes.friendly_name"],
        "registry_entry": ["entity_id", "name", "area_id", "labels"],
        "device": ["name_by_user", "area_id", "labels"],
        "area": ["name", "floor_id"],
        "label": ["name"],
        "floor": ["name", "level"],
    }
    staleness_window = 15.0

    #: test hooks: route HTTP through this transport (e.g. httpx.MockTransport)
    #: and the WebSocket API to this URL (see ``home_assistant.testing``)
    _transport: ClassVar[httpx.BaseTransport | None] = None
    _ws_url: ClassVar[str | None] = None
    #: serializes read-time refreshes and registry writes (MCP runs sync
    #: tools on worker threads); reentrant because writes refresh first
    _sync_lock: ClassVar[threading.RLock] = threading.RLock()

    # ------------------------------------------------------------ transport

    def _open_transport(self) -> httpx.Client:
        return httpx.Client(
            base_url=str(self.config.get("url")).rstrip("/"),
            headers={"Authorization": f"Bearer {self.config.get('token')}"},
            timeout=self.config.timeout,
            transport=self.__class__._transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        json: Any = None,
        *,
        missing: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        """One HA REST call. A 404 raises ``NotFoundError(missing)`` when given;
        any other failure carries HA's own reason."""
        http = cast(httpx.Client, self.transport)
        try:
            resp = http.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise RemoteError(f"cannot reach Home Assistant at {self._host()}: {exc}") from exc
        if resp.status_code == 401:
            raise RemoteError(
                "Home Assistant rejected the access token (HTTP 401) — "
                "check STUDIOHAYS_HA_TOKEN or token="
            )
        if resp.status_code == 404 and missing is not None:
            raise NotFoundError(missing)
        if not resp.is_success:
            reason = _reason(resp)
            raise HTTPStatusError(
                resp.status_code,
                f"Home Assistant returned HTTP {resp.status_code} for {method} {path}"
                + (f": {reason}" if reason else ""),
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise RemoteError(f"Home Assistant sent invalid JSON for {method} {path}") from exc

    def _post_service(
        self, domain: str, service: str, data: Mapping, return_response: bool
    ) -> Any:
        name = f"{domain}.{service}"
        try:
            return self._request(
                "POST",
                f"/api/services/{domain}/{service}",
                data,
                missing=f"unknown service {name} (HTTP 404)",
                params={"return_response": ""} if return_response else None,
            )
        except HTTPStatusError as exc:
            # HA 2026.9 reports an unknown service as a bare "400: Bad Request"
            if exc.status == 400 and self._service_exists(domain, service) is False:
                raise NotFoundError(
                    f"Home Assistant has no service {name} — check the service name; registry "
                    "edits are not services (use update_entity/update_device/update_area)"
                ) from exc
            raise

    def _service_exists(self, domain: str, service: str) -> bool | None:
        """Whether HA lists the service (None if the list can't be fetched)."""
        try:
            listing = self._request("GET", "/api/services")
        except RemoteError:
            return None
        return any(
            d.get("domain") == domain and service in (d.get("services") or {})
            for d in listing or []
        )

    def _id_of(self, resource: str, obj: Mapping) -> str:
        return str(obj[_ID_FIELDS.get(resource, "id")])

    def _entity_ids(self) -> set[str]:
        return {r["id"] for r in self.store.get_snapshot(RESOURCE)}

    def _apply_update(
        self,
        resource: str,
        rid: str,
        current: dict,
        fields: dict,
        dry_run: bool,
        command: str,
        target: dict,
        *,
        subject: dict,
        unwrap: str | None = None,
    ) -> dict:
        """Dry-run or send one registry update; write the result through."""
        result = {
            **subject,
            "dry_run": dry_run,
            "changes": changes(current, fields),
            "before": current,
            "after": predicted(current, fields),
        }
        if dry_run or not fields:
            return result

        def send() -> dict:
            with self._ws() as session:
                reply = session.call(command, **target, **fields)
            return reply[unwrap] if unwrap else reply

        result["after"] = self._updated(resource, rid, fields, send)
        self._dirty = True  # friendly names / entity_ids in states follow the registry
        return result

    def _apply_step(self, step: dict, dry_run: bool = False) -> dict:
        """Dry-run or send one planned registry step (``writes._step``)."""
        current, fields = step["current"], step["fields"]
        if step["action"] == "update_area":
            return self._apply_update(
                "area", current["area_id"], current, fields, dry_run,
                "config/area_registry/update", {"area_id": current["area_id"]},
                subject={"area_id": current["area_id"]},
            )
        if step["action"] == "update_floor":
            return self._apply_update(
                "floor", current["floor_id"], current, fields, dry_run,
                "config/floor_registry/update", {"floor_id": current["floor_id"]},
                subject={"floor_id": current["floor_id"]},
            )
        if step["action"] == "update_device":
            return self._apply_update(
                "device", current["id"], current, fields, dry_run,
                "config/device_registry/update", {"device_id": current["id"]},
                subject={"device_id": current["id"]},
            )
        return self._apply_update(
            "registry_entry", current["id"], current, fields, dry_run,
            "config/entity_registry/update", {"entity_id": step["target"]},
            unwrap="entity_entry", subject={"entity_id": step["target"]},
        )

    def _references(self, entity_ids: list[str]) -> dict[str, dict[str, list[str]]]:
        """Automations/scripts that mention each entity_id (WS search/related)."""
        if not entity_ids:
            return {}
        out: dict[str, dict[str, list[str]]] = {}
        with self._ws() as session:
            for eid in entity_ids:
                related = session.call("search/related", item_type="entity", item_id=eid) or {}
                refs = {k: sorted(related[k]) for k in ("automation", "script") if related.get(k)}
                if refs:
                    out[eid] = refs
        return out

    def _ws(self):
        """A short-lived authenticated WebSocket session (context manager)."""
        self._ensure()
        url = self.__class__._ws_url or ws.ws_url(str(self.config.get("url")))
        return ws.session(url, str(self.config.get("token")), self.config.timeout)

    def _pull(self) -> list[tuple[str, str, Mapping]]:
        states = self._request("GET", "/api/states")
        if not isinstance(states, list):
            raise RemoteError(f"unexpected /api/states payload from {self._host()}")
        rows: list[tuple[str, str, Mapping]] = [(RESOURCE, str(s["entity_id"]), s) for s in states]
        with self._ws() as session:
            for kind, command, id_field in REGISTRIES:
                rows += [(kind, str(item[id_field]), item) for item in session.call(command)]
        return rows

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

    @property
    def _dirty(self) -> bool:
        """Set by service calls and registry writes: the next read refreshes
        regardless of staleness. Persisted in state meta, so it holds across
        processes (a CLI write invalidates a running MCP server's reads)."""
        return self.store.get_meta("dirty") == "1"

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        if value != self._dirty:
            self.store.set_meta("dirty", "1" if value else "0")

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
    def update(self) -> Update:
        return Update(self)

    @property
    def create(self) -> Create:
        return Create(self)

    @property
    def delete(self) -> Delete:
        return Delete(self)

    @property
    def call(self) -> _Call:
        return _Call(self)

    @property
    def state(self) -> _State:
        return _State(self)
