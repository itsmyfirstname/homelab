"""Registry writes (STU-10): plan → (dry run | apply over WebSocket).

Every write resolves references against the mirrored registries, computes
only the fields that actually change, and either returns that plan
(``dry_run=True``) or sends it and writes the entry HA returns through to
local state (mirror mode: no ``managed`` rows). Only fields a caller passes
are changed; ``""`` (or ``None`` from Python) clears a name or area.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from ..core import NotFoundError, StudioHaysError
from .automations import delete_automation, save_automation
from .scripts import delete_script, save_script
from .registry import Registry, device_name, normalize_name, slugify

if TYPE_CHECKING:
    from .client import Client


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


#: "not passed" (distinct from None/"", which clear the field)
UNSET: Any = _Unset()


def _refs(value: Iterable[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


def _labels(reg: Registry, current: Iterable[str] | None, add: Any, remove: Any) -> list[str] | None:
    """The new label list, or None when it doesn't change."""
    have = set(current or [])
    add_ids = {reg.label(r)["label_id"] for r in _refs(add)}
    remove_ids = {reg.label(r)["label_id"] for r in _refs(remove)}
    new = (have | add_ids) - remove_ids
    return sorted(new) if new != have else None


def _set(fields: dict, key: str, value: object, current: object) -> None:
    if value != current:
        fields[key] = value


def _registered(reg: Registry, entity_id: str, with_state: set[str]) -> dict:
    entry = reg.entries.get(entity_id)
    if entry is not None:
        return entry
    if entity_id in with_state:
        raise StudioHaysError(
            f"{entity_id} is not in the entity registry — can't be renamed, labelled or moved"
        )
    raise NotFoundError(f"entity not found: {entity_id} — run state.refresh() if it exists on the remote")


def plan_entity(
    reg: Registry,
    entity_id: str,
    with_state: set[str],
    *,
    name: Any = UNSET,
    area: Any = UNSET,
    add_labels: Any = None,
    remove_labels: Any = None,
    new_entity_id: str | None = None,
) -> tuple[dict, dict]:
    """(current registry entry, fields for ``config/entity_registry/update``)."""
    entry = _registered(reg, entity_id, with_state)
    fields: dict[str, Any] = {}
    if name is not UNSET:
        _set(fields, "name", name or None, entry.get("name"))
    if area is not UNSET:
        _set(fields, "area_id", reg.area(area)["area_id"] if area else None, entry.get("area_id"))
    labels = _labels(reg, entry.get("labels"), add_labels, remove_labels)
    if labels is not None:
        fields["labels"] = labels
    if new_entity_id and new_entity_id != entity_id:
        domain = entity_id.partition(".")[0]
        if new_entity_id.partition(".")[0] != domain or "." not in new_entity_id:
            raise StudioHaysError(f"new_entity_id {new_entity_id!r} must keep the domain {domain!r}")
        if new_entity_id in reg.entries or new_entity_id in with_state:
            raise StudioHaysError(f"{new_entity_id} already exists")
        fields["new_entity_id"] = new_entity_id
    return entry, fields


def _ensure_unique_name(
    kind: str, rows: Iterable[Mapping], name: str, exclude: str | None = None
) -> None:
    """Mirror HA 2026.9: registry names must be unique after normalization."""
    wanted = normalize_name(name)
    for row in rows:
        if row[f"{kind}_id"] != exclude and normalize_name(row["name"]) == wanted:
            raise StudioHaysError(
                f"name {name!r} is already used by {kind} {row['name']!r} — pick a distinct name"
            )


def plan_floor(
    reg: Registry, floor: str, *, name: Any = UNSET, level: Any = UNSET
) -> tuple[dict, dict]:
    """(current floor, fields for ``config/floor_registry/update``)."""
    current = reg.floor(floor)
    fields: dict[str, Any] = {}
    if name is not UNSET:
        if not name or not str(name).strip():
            raise StudioHaysError(f"floor {current['floor_id']}: the name can't be blank")
        _ensure_unique_name("floor", reg.floors.values(), name, exclude=current["floor_id"])
        _set(fields, "name", name, current.get("name"))
    if level is not UNSET:
        _set(fields, "level", level, current.get("level"))
    return current, fields


def plan_area(
    reg: Registry, area: str, *, name: Any = UNSET, floor: Any = UNSET
) -> tuple[dict, dict]:
    """(current area, fields for ``config/area_registry/update``)."""
    current = reg.area(area)
    fields: dict[str, Any] = {}
    if name is not UNSET:
        if not name or not name.strip():
            raise StudioHaysError(f"area {current['area_id']}: the name can't be blank")
        _ensure_unique_name("area", reg.areas.values(), name, exclude=current["area_id"])
        _set(fields, "name", name, current.get("name"))
    if floor is not UNSET:
        _set(fields, "floor_id", reg.floor(floor)["floor_id"] if floor else None, current.get("floor_id"))
    return current, fields


def plan_device(
    reg: Registry,
    device: str,
    *,
    name: Any = UNSET,
    area: Any = UNSET,
    add_labels: Any = None,
    remove_labels: Any = None,
) -> tuple[dict, dict]:
    """(current device, fields for ``config/device_registry/update``)."""
    current = reg.device(device)
    fields: dict[str, Any] = {}
    if name is not UNSET:
        _set(fields, "name_by_user", name or None, current.get("name_by_user"))
    if area is not UNSET:
        _set(fields, "area_id", reg.area(area)["area_id"] if area else None, current.get("area_id"))
    labels = _labels(reg, current.get("labels"), add_labels, remove_labels)
    if labels is not None:
        fields["labels"] = labels
    return current, fields


def plan_repurpose(
    reg: Registry,
    device: str,
    with_state: set[str],
    *,
    name: Any = UNSET,
    area: Any = UNSET,
    add_labels: Any = None,
    remove_labels: Any = None,
    rename_entity_ids: bool = False,
) -> tuple[dict, list[dict]]:
    """(device, ordered steps) for moving/renaming/relabelling a device.

    Steps: the device itself, then each of its entities whose name override
    mentions the old device name, carries a removed label, or (opt-in) has
    an entity_id containing the old device name's slug. Entities with
    inherited names follow the device on their own.
    """
    current = reg.device(device)
    old_name = device_name(current)
    steps: list[dict] = []
    _, device_fields = plan_device(
        reg, current["id"], name=name, area=area,
        add_labels=add_labels, remove_labels=remove_labels,
    )
    if device_fields:
        steps.append(_step("update_device", current["id"], current, device_fields))
    if name is not UNSET and not name:
        raise StudioHaysError(
            "repurpose_device needs a new name (to clear a device name, use update_device name=\"\")"
        )
    renaming = name is not UNSET and name != old_name
    old_slug, new_slug = slugify(old_name), slugify(name) if renaming else ""
    planned_ids: set[str] = set()
    for eid in reg.entities_of(current["id"]):
        entry = reg.entries[eid]
        kwargs: dict[str, Any] = {"remove_labels": [
            r for r in _refs(remove_labels)
            if reg.label(r)["label_id"] in (entry.get("labels") or [])
        ]}
        override = entry.get("name")
        if renaming and override and old_name.casefold() in override.casefold():
            kwargs["name"] = re.sub(
                re.escape(old_name), lambda _m: name, override, flags=re.IGNORECASE
            )
        domain, _, object_id = eid.partition(".")
        token = re.compile(rf"(?<![a-z0-9]){re.escape(old_slug)}(?![a-z0-9])")
        if renaming and rename_entity_ids and old_slug and token.search(object_id):
            new_id = f"{domain}.{token.sub(lambda _m: new_slug, object_id)}"
            if new_id in planned_ids:
                raise StudioHaysError(f"two entities would both become {new_id}")
            planned_ids.add(new_id)
            kwargs["new_entity_id"] = new_id
        _, fields = plan_entity(reg, eid, with_state, **kwargs)
        if fields:
            steps.append(_step("update_entity", eid, entry, fields))
    return current, steps


def _step(action: str, target: str, current: dict, fields: dict) -> dict:
    return {"action": action, "target": target, "changes": changes(current, fields),
            "current": current, "fields": fields}


def changes(current: Mapping, fields: Mapping) -> dict:
    was_key = {"new_entity_id": "entity_id"}
    return {k: {"was": current.get(was_key.get(k, k)), "is": v} for k, v in fields.items()}


def predicted(current: Mapping, fields: Mapping) -> dict:
    after = {**current, **{k: v for k, v in fields.items() if k != "new_entity_id"}}
    if "new_entity_id" in fields:
        after["entity_id"] = fields["new_entity_id"]
    return after


F = TypeVar("F", bound=Callable[..., dict])


def _serialized(method: F) -> F:
    """Plan + send + record under the client's lock, so concurrent MCP writes
    (worker threads) can't plan against a snapshot another write is changing."""

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> dict:
        with self._c._sync_lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class Update:
    """``ha.update``: registry edits. Mutating unless ``dry_run=True``."""

    def __init__(self, client: "Client") -> None:
        self._c = client

    @_serialized
    def entity(
        self,
        entity_id: str,
        name: Any = UNSET,
        area: Any = UNSET,
        add_labels: Iterable[str] | str | None = None,
        remove_labels: Iterable[str] | str | None = None,
        new_entity_id: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        c = self._c
        c._sync()
        entry, fields = plan_entity(
            Registry(c.store), entity_id, c._entity_ids(),
            name=name, area=area, add_labels=add_labels,
            remove_labels=remove_labels, new_entity_id=new_entity_id,
        )
        return c._apply_step(_step("update_entity", entity_id, entry, fields), dry_run)

    @_serialized
    def device(
        self,
        device: str,
        name: Any = UNSET,
        area: Any = UNSET,
        add_labels: Iterable[str] | str | None = None,
        remove_labels: Iterable[str] | str | None = None,
        dry_run: bool = False,
    ) -> dict:
        c = self._c
        c._sync()
        current, fields = plan_device(
            Registry(c.store), device,
            name=name, area=area, add_labels=add_labels, remove_labels=remove_labels,
        )
        return c._apply_step(_step("update_device", current["id"], current, fields), dry_run)

    @_serialized
    def area(
        self, area: str, name: Any = UNSET, floor: Any = UNSET, dry_run: bool = False
    ) -> dict:
        """Rename an area and/or assign its floor (``floor=""`` clears it)."""
        c = self._c
        c._sync()
        current, fields = plan_area(Registry(c.store), area, name=name, floor=floor)
        return c._apply_step(_step("update_area", current["area_id"], current, fields), dry_run)

    @_serialized
    def floor(
        self, floor: str, name: Any = UNSET, level: Any = UNSET, dry_run: bool = False
    ) -> dict:
        """Rename a floor and/or set its sort level."""
        c = self._c
        c._sync()
        current, fields = plan_floor(Registry(c.store), floor, name=name, level=level)
        return c._apply_step(_step("update_floor", current["floor_id"], current, fields), dry_run)


    @_serialized
    def automation(
        self, config: Mapping, automation_id: str | None = None, dry_run: bool = False
    ) -> dict:
        """Create or replace an automation (HA validates it first)."""
        return save_automation(self._c, config, automation_id, dry_run)

    @_serialized
    def script(
        self, config: Mapping, script_id: str | None = None, dry_run: bool = False
    ) -> dict:
        """Create or replace a script (HA validates it first)."""
        return save_script(self._c, config, script_id, dry_run)

    @_serialized
    def repurpose_device(
        self,
        device: str,
        area: Any = UNSET,
        name: Any = UNSET,
        add_labels: Iterable[str] | str | None = None,
        remove_labels: Iterable[str] | str | None = None,
        rename_entity_ids: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Move/rename/relabel a device and carry its entities along.

        Applies the plan step by step and stops at the first failure
        (no rollback); ``ok`` / ``applied`` / ``not_applied`` / ``error``
        report exactly what happened.
        """
        c = self._c
        c._sync()
        reg = Registry(c.store)
        current, steps = plan_repurpose(
            reg, device, c._entity_ids(), name=name, area=area,
            add_labels=add_labels, remove_labels=remove_labels,
            rename_entity_ids=rename_entity_ids,
        )
        result: dict[str, Any] = {
            "device_id": current["id"],
            "device": device_name(current),
            "dry_run": dry_run,
            "plan": [_public(s) for s in steps],
        }
        if rename_entity_ids:
            result["references"] = c._references(
                [s["target"] for s in steps if "new_entity_id" in s["fields"]]
            )
        if dry_run:
            return result
        applied: list[dict] = []
        for i, step in enumerate(steps):
            try:
                c._apply_step(step)
            except StudioHaysError as exc:
                return {**result, "ok": False, "error": str(exc), "applied": applied,
                        "not_applied": [_public(s) for s in steps[i:]]}
            applied.append(_public(step))
        return {**result, "ok": True, "error": None, "applied": applied, "not_applied": []}


def _public(step: dict) -> dict:
    return {"action": step["action"], "target": step["target"], "changes": step["changes"]}


class Delete:
    """``ha.delete``: remove areas, automations and scripts. Mutating unless ``dry_run=True``."""

    def __init__(self, client: "Client") -> None:
        self._c = client

    @_serialized
    def area(self, area: str, force: bool = False, dry_run: bool = False) -> dict:
        """Delete an area. Refuses while devices or entity overrides still point
        at it unless ``force=True`` (Home Assistant then unassigns them)."""
        c = self._c
        c._sync()
        reg = Registry(c.store)
        current = reg.area(area)
        area_id = current["area_id"]
        devices = sorted(
            (d for d in reg.devices.values() if d.get("area_id") == area_id),
            key=lambda d: device_name(d).casefold(),
        )
        overrides = sorted(eid for eid, e in reg.entries.items() if e.get("area_id") == area_id)
        impact = {
            "floor": (reg.floor_ref(area_id) or {}).get("name"),
            "labels": reg.label_refs(current.get("labels") or []),
            "device_count": len(devices),
            "devices": [{"id": d["id"], "name": device_name(d)} for d in devices],
            "entity_overrides": overrides,
            "entity_count": reg.entity_count(area_id, c._entity_ids()),
        }
        result: dict[str, Any] = {
            "area_id": area_id, "dry_run": dry_run, "needs_force": bool(devices or overrides),
            "impact": impact, "before": current, "after": None,
        }
        if dry_run:
            return result
        if result["needs_force"] and not force:
            parts = []
            if devices:
                parts.append(f"{len(devices)} device(s) ({', '.join(device_name(d) for d in devices)})")
            if overrides:
                parts.append(f"{len(overrides)} entity override(s) ({', '.join(overrides)})")
            raise StudioHaysError(
                f"area {current['name']!r} still has {' and '.join(parts)} — move them first, "
                "or pass force=true (Home Assistant then leaves them without an area)"
            )

        def send() -> object:
            with c._ws() as session:
                return session.call("config/area_registry/delete", area_id=area_id)

        c._deleted("area", area_id, send)
        c._dirty = True  # devices/entities in the area were unassigned by HA
        return result

    @_serialized
    def automation(self, automation_id: str, dry_run: bool = False) -> dict:
        """Delete an automation (removes it from automations.yaml)."""
        return delete_automation(self._c, automation_id, dry_run)

    @_serialized
    def script(self, script_id: str, dry_run: bool = False) -> dict:
        """Delete a script (removes it from scripts.yaml)."""
        return delete_script(self._c, script_id, dry_run)


class Create:
    """``ha.create``: new labels, areas and floors. Mutating unless ``dry_run=True``."""

    def __init__(self, client: "Client") -> None:
        self._c = client

    def label(self, name: str, dry_run: bool = False) -> dict:
        return self._create("label", name, dry_run)

    def area(self, name: str, dry_run: bool = False) -> dict:
        return self._create("area", name, dry_run)

    @_serialized
    def floor(self, name: str, level: int, dry_run: bool = False) -> dict:
        """Create a floor (``level`` is HA's sort order, 0 = lowest)."""
        c = self._c
        c._sync()
        _ensure_unique_name("floor", Registry(c.store).floors.values(), name)
        desired = {"name": name, "level": level}
        if dry_run:
            return {"dry_run": True, "before": None, "after": desired}

        def send() -> dict:
            with c._ws() as session:
                return session.call("config/floor_registry/create", name=name, level=level)

        after = c._created("floor", desired, send, manage=False)
        return {"dry_run": False, "before": None, "after": after}

    @_serialized
    def _create(self, kind: str, name: str, dry_run: bool) -> dict:
        c = self._c
        c._sync()
        reg = Registry(c.store)
        rows = reg.labels if kind == "label" else reg.areas
        _ensure_unique_name(kind, rows.values(), name)
        if dry_run:
            return {"dry_run": True, "before": None, "after": {"name": name}}

        def send() -> dict:
            with c._ws() as session:
                return session.call(f"config/{kind}_registry/create", name=name)

        after = c._created(kind, {"name": name}, send, manage=False)
        return {"dry_run": False, "before": None, "after": after}
