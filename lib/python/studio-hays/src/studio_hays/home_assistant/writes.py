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
from .registry import Registry, device_name

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


def slugify(text: str) -> str:
    """HA-style object_id slug: "Reading Lamp" -> "reading_lamp"."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


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


class Create:
    """``ha.create``: new labels and areas. Mutating unless ``dry_run=True``."""

    def __init__(self, client: "Client") -> None:
        self._c = client

    def label(self, name: str, dry_run: bool = False) -> dict:
        return self._create("label", name, dry_run)

    def area(self, name: str, dry_run: bool = False) -> dict:
        return self._create("area", name, dry_run)

    @_serialized
    def _create(self, kind: str, name: str, dry_run: bool) -> dict:
        c = self._c
        c._sync()
        reg = Registry(c.store)
        rows = reg.labels if kind == "label" else reg.areas
        if any(r["name"].casefold() == name.casefold() for r in rows.values()):
            raise StudioHaysError(f"{kind} {name!r} already exists")
        if dry_run:
            return {"dry_run": True, "before": None, "after": {"name": name}}

        def send() -> dict:
            with c._ws() as session:
                return session.call(f"config/{kind}_registry/create", name=name)

        after = c._created(kind, {"name": name}, send, manage=False)
        return {"dry_run": False, "before": None, "after": after}
