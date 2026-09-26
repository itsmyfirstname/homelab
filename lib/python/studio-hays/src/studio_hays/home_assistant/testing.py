"""A fake Home Assistant for tests: REST via httpx.MockTransport, WebSocket
via a real ``websockets`` server on localhost, both over one shared model.

    with FakeHomeAssistant(states=[...], devices=[...], areas=[...]) as ha:
        Client(url=ha.url, token=ha.token).get.entities()

Entering installs the fake on :class:`Client`'s transport hooks; leaving
removes them. Only the slice of HA the module uses is modelled.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping
from typing import Any

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server, ServerConnection, serve

from .client import Client
from .writes import slugify as slug

HA_VERSION = "2026.9.3"


class CommandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FakeHomeAssistant:
    url = "http://ha.test:8123"

    def __init__(
        self,
        states: Iterable[Mapping] = (),
        *,
        token: str = "test-token",
        devices: Iterable[Mapping] = (),
        areas: Iterable[Mapping] = (),
        labels: Iterable[Mapping] = (),
        floors: Iterable[Mapping] = (),
        entries: Iterable[Mapping] = (),
        unregistered: Iterable[str] = (),
        ws_token: str | None = None,
    ) -> None:
        """``entries`` override the registry entry derived for each state;
        ``unregistered`` entity_ids get a state but no registry entry;
        ``ws_token`` makes the WebSocket API expect a different token."""
        self.token = token
        self.ws_token = ws_token
        self.states: dict[str, dict] = {s["entity_id"]: _copy(s) for s in states}
        self.devices: dict[str, dict] = {d["id"]: _device(d) for d in devices}
        self.areas: dict[str, dict] = {a["area_id"]: _area(a) for a in areas}
        self.labels: dict[str, dict] = {x["label_id"]: _label(x) for x in labels}
        self.floors: dict[str, dict] = {f["floor_id"]: dict(f) for f in floors}
        skip = set(unregistered)
        self.entries: dict[str, dict] = {
            eid: _entry(eid, (s.get("attributes") or {}).get("friendly_name"))
            for eid, s in self.states.items()
            if eid not in skip
        }
        for e in entries:
            base = self.entries.get(e["entity_id"]) or _entry(e["entity_id"], None)
            self.entries[e["entity_id"]] = {**base, **_copy(e)}
        #: search/related answers: entity_id -> {"automation": [...], "script": [...]}
        self.references: dict[str, dict[str, list[str]]] = {}
        self.calls: list[tuple[str, str, object]] = []  # REST (method, path, json)
        self.commands: list[tuple[str, dict]] = []  # WS (type, payload)
        self.down = False
        self.status_override: int | None = None
        #: command type -> (code, message) returned once as a WS error
        self.fail_next: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()
        self._server: Server | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def ws_url(self) -> str:
        assert self._server is not None, "use FakeHomeAssistant as a context manager"
        host, port = self._server.socket.getsockname()[:2]
        return f"ws://{host}:{port}/api/websocket"

    def __enter__(self) -> "FakeHomeAssistant":
        self._server = serve(self._ws_handler, "127.0.0.1", 0, compression=None)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        Client._transport = httpx.MockTransport(self.handler)
        Client._ws_url = self.ws_url
        return self

    def __exit__(self, *exc_info: object) -> None:
        Client._transport = None
        Client._ws_url = None
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ REST

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.status_override is not None:
            return httpx.Response(self.status_override)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401)
        path = request.url.path
        with self._lock:
            if request.method == "GET" and path == "/api/states":
                return httpx.Response(200, json=list(self.states.values()))
            if request.method == "GET" and path.startswith("/api/states/"):
                state = self.states.get(path.removeprefix("/api/states/"))
                return httpx.Response(200, json=state) if state else httpx.Response(404)
            if request.method == "POST" and path.startswith("/api/services/"):
                return self._service(path, body, "return_response" in request.url.params)
        return httpx.Response(404)

    def _service(self, path: str, body: Any, return_response: bool) -> httpx.Response:
        """HA 2026.9 semantics: unknown services are a 400 with a message;
        response-only services need ``?return_response`` and others reject it."""
        domain, _, service = path.removeprefix("/api/services/").partition("/")
        if (domain, service) == ("weather", "get_forecasts"):
            if not return_response:
                return _bad_request(
                    "Service call requires responses but caller did not ask for responses. "
                    "Add ?return_response to query parameters."
                )
            ids = (body or {}).get("entity_id")
            forecasts = {eid: {"forecast": [{"condition": "sunny"}]} for eid in [ids] if eid}
            return httpx.Response(200, json={"changed_states": [], "service_response": forecasts})
        if domain != "light" or service not in ("turn_on", "turn_off"):
            return _bad_request(f"Service {domain}.{service} not found.")
        if return_response:
            return _bad_request(
                "Service does not support responses. Remove return_response from request."
            )
        ids = (body or {}).get("entity_id")
        changed = []
        for eid in [ids] if isinstance(ids, str) else ids or []:
            if eid in self.states:
                self.states[eid]["state"] = "on" if service == "turn_on" else "off"
                changed.append(self.states[eid])
        return httpx.Response(200, json=changed)

    # ------------------------------------------------------------- WebSocket

    def _ws_handler(self, conn: ServerConnection) -> None:
        if self.down:
            conn.close()
            return
        try:
            conn.send(json.dumps({"type": "auth_required", "ha_version": HA_VERSION}))
            auth = json.loads(conn.recv())
            expected = self.ws_token if self.ws_token is not None else self.token
            if auth.get("type") != "auth" or auth.get("access_token") != expected:
                conn.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token or password"}))
                return
            conn.send(json.dumps({"type": "auth_ok", "ha_version": HA_VERSION}))
            for raw in conn:
                msg = json.loads(raw)
                conn.send(json.dumps(self._reply(msg)))
        except ConnectionClosed:
            return

    def _reply(self, msg: dict) -> dict:
        msg_id, kind = msg.get("id"), msg.get("type", "")
        payload = {k: v for k, v in msg.items() if k not in ("id", "type")}
        self.commands.append((kind, payload))
        try:
            if kind in self.fail_next:
                raise CommandError(*self.fail_next.pop(kind))
            with self._lock:
                result = self._command(kind, payload)
        except CommandError as exc:
            return {"id": msg_id, "type": "result", "success": False,
                    "error": {"code": exc.code, "message": str(exc)}}
        return {"id": msg_id, "type": "result", "success": True, "result": result}

    def _command(self, kind: str, p: dict) -> Any:
        if kind == "config/entity_registry/list":
            return [dict(e) for e in self.entries.values()]
        if kind == "config/device_registry/list":
            return [dict(d) for d in self.devices.values()]
        if kind == "config/area_registry/list":
            return [dict(a) for a in self.areas.values()]
        if kind == "config/label_registry/list":
            return [dict(x) for x in self.labels.values()]
        if kind == "config/floor_registry/list":
            return [dict(f) for f in self.floors.values()]
        if kind == "config/entity_registry/update":
            return {"entity_entry": self._update_entry(p)}
        if kind == "config/device_registry/update":
            return self._update_device(p)
        if kind == "config/area_registry/update":
            return self._update_area(p)
        if kind == "config/area_registry/create":
            return self._create(self.areas, "area_id", p["name"], _area)
        if kind == "config/label_registry/create":
            return self._create(self.labels, "label_id", p["name"], _label)
        if kind == "search/related":
            return self.references.get(p.get("item_id", ""), {})
        raise CommandError("unknown_command", f"Unknown command: {kind}")

    def _update_entry(self, p: dict) -> dict:
        eid = p["entity_id"]
        entry = self.entries.get(eid)
        if entry is None:
            raise CommandError("not_found", "Entity not found")
        for key in ("name", "area_id", "labels"):
            if key in p:
                entry[key] = p[key]
        new_id = p.get("new_entity_id")
        if new_id and new_id != eid:
            if new_id in self.entries or new_id in self.states:
                raise CommandError("invalid_info", "Entity with this ID is already registered")
            entry["entity_id"] = new_id
            self.entries[new_id] = self.entries.pop(eid)
            if eid in self.states:
                self.states[new_id] = {**self.states.pop(eid), "entity_id": new_id}
        self._sync_friendly_names()
        return dict(entry)

    def _update_device(self, p: dict) -> dict:
        device = self.devices.get(p["device_id"])
        if device is None:
            raise CommandError("not_found", "Device not found")
        for key in ("name_by_user", "area_id", "labels"):
            if key in p:
                device[key] = p[key]
        self._sync_friendly_names()
        return dict(device)

    def _update_area(self, p: dict) -> dict:
        area = self.areas.get(p["area_id"])
        if area is None:
            raise CommandError("not_found", "Area ID doesn't exist")
        if "name" in p:
            _check_unique(self.areas, "area_id", p["name"], exclude=area["area_id"])
            area["name"] = p["name"]
        if "floor_id" in p:
            area["floor_id"] = p["floor_id"]
        return dict(area)

    def _create(self, table: dict, id_key: str, name: str, shape: Any) -> dict:
        _check_unique(table, id_key, name)
        row = shape({id_key: slug(name), "name": name})
        table[row[id_key]] = row
        return dict(row)

    def _sync_friendly_names(self) -> None:
        """Mimic HA: a state's friendly_name follows its registry naming."""
        for eid, entry in self.entries.items():
            state = self.states.get(eid)
            if state is None:
                continue
            device = self.devices.get(entry.get("device_id") or "")
            if entry.get("name"):
                name = entry["name"]
            elif device is not None and entry.get("has_entity_name"):
                device_name = device.get("name_by_user") or device.get("name")
                suffix = entry.get("original_name")
                name = f"{device_name} {suffix}" if suffix else device_name
            else:
                name = entry.get("original_name")
            state.setdefault("attributes", {})["friendly_name"] = name


def _bad_request(message: str) -> httpx.Response:
    return httpx.Response(400, json={"message": message})


def _ha_normalize(name: str) -> str:
    """HA 2026.9's registry-name normalization, kept independent of the client's
    own copy so a wrong client normalization can't pass by construction."""
    return name.casefold().replace(" ", "")


def _check_unique(table: dict, id_key: str, name: str, exclude: str | None = None) -> None:
    wanted = _ha_normalize(name)
    for row in table.values():
        if row[id_key] != exclude and _ha_normalize(row["name"]) == wanted:
            raise CommandError("invalid_info", f"The name {name} ({wanted}) is already in use")


def _copy(obj: Mapping) -> dict:
    return json.loads(json.dumps(obj))


def _entry(entity_id: str, original_name: object) -> dict:
    return {
        "id": f"reg-{entity_id}",
        "entity_id": entity_id,
        "name": None,
        "original_name": original_name,
        "has_entity_name": False,
        "area_id": None,
        "device_id": None,
        "labels": [],
        "platform": "fake",
        "disabled_by": None,
    }


def _device(d: Mapping) -> dict:
    return {"name_by_user": None, "area_id": None, "labels": [], **_copy(d)}


def _area(a: Mapping) -> dict:
    return {"floor_id": None, "labels": [], "aliases": [], "icon": None, **_copy(a)}


def _label(x: Mapping) -> dict:
    return {"color": None, "icon": None, "description": None, **_copy(x)}
