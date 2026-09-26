"""A read-only view over the mirrored HA registries (one snapshot read).

Joins entities to their registry entry, device, effective area (the entry's
own ``area_id``, else its device's) and labels, and resolves user references
(name or id, case-insensitive) to registry rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..core import NotFoundError, StateStore, StudioHaysError


def _index(store: StateStore, resource_type: str) -> dict[str, dict]:
    return {r["id"]: r["attributes"] for r in store.get_snapshot(resource_type)}


def normalize_name(name: str) -> str:
    """HA's registry name normalization (2026.9): names that normalize equal
    collide ("Hallway" vs "hallway" vs "Hall way"; "Hall-way" stays distinct)."""
    return name.casefold().replace(" ", "")


def device_name(device: Mapping) -> str:
    return device.get("name_by_user") or device.get("name") or device["id"]


class Registry:
    def __init__(self, store: StateStore) -> None:
        entries = _index(store, "registry_entry").values()
        self.entries: dict[str, dict] = {e["entity_id"]: e for e in entries}
        self.devices = _index(store, "device")
        self.areas = _index(store, "area")
        self.labels = _index(store, "label")
        self.floors = _index(store, "floor")

    # --------------------------------------------------------------- joins

    def device_of(self, entity_id: str) -> dict | None:
        entry = self.entries.get(entity_id)
        return self.devices.get(entry.get("device_id") or "") if entry else None

    def area_of(self, entity_id: str) -> tuple[str | None, str | None]:
        """(effective area_id, source) where source is "entity" or "device"."""
        entry = self.entries.get(entity_id)
        if entry is None:
            return None, None
        if entry.get("area_id"):
            return entry["area_id"], "entity"
        device = self.device_of(entity_id)
        if device is not None and device.get("area_id"):
            return device["area_id"], "device"
        return None, None

    def labels_of(self, entity_id: str) -> set[str]:
        """Label ids on the entity or on its device."""
        entry = self.entries.get(entity_id) or {}
        device = self.device_of(entity_id) or {}
        return set(entry.get("labels") or []) | set(device.get("labels") or [])

    def entities_of(self, device_id: str) -> list[str]:
        return sorted(eid for eid, e in self.entries.items() if e.get("device_id") == device_id)

    # ------------------------------------------------------------ rendering

    def area_ref(self, area_id: str | None) -> dict | None:
        area = self.areas.get(area_id or "")
        return {"area_id": area["area_id"], "name": area["name"]} if area else None

    def floor_ref(self, area_id: str | None) -> dict | None:
        area = self.areas.get(area_id or "") or {}
        floor = self.floors.get(area.get("floor_id") or "")
        return {"floor_id": floor["floor_id"], "name": floor["name"]} if floor else None

    def label_refs(self, label_ids: Iterable[str]) -> list[dict]:
        return [
            {"label_id": lid, "name": self.labels.get(lid, {}).get("name", lid)}
            for lid in sorted(label_ids)
        ]

    def entity_info(self, entity_id: str) -> dict:
        entry = self.entries.get(entity_id)
        if entry is None:
            return {"registered": False}
        area_id, source = self.area_of(entity_id)
        device = self.device_of(entity_id)
        return {
            "registered": True,
            "registry_id": entry["id"],
            "name": entry.get("name"),
            "area": self.area_ref(area_id),
            "area_source": source,
            "floor": self.floor_ref(area_id),
            "device": {"id": device["id"], "name": device_name(device)} if device else None,
            "labels": self.label_refs(entry.get("labels") or []),
            "device_labels": self.label_refs((device or {}).get("labels") or []),
        }

    # ------------------------------------------------------------ resolving

    def area(self, ref: str) -> dict:
        return _resolve("area", ref, self.areas.values(), "area_id", lambda a: [a["name"]])

    def floor(self, ref: str) -> dict:
        return _resolve("floor", ref, self.floors.values(), "floor_id", lambda f: [f["name"]])

    def label(self, ref: str) -> dict:
        return _resolve("label", ref, self.labels.values(), "label_id", lambda x: [x["name"]])

    def device(self, ref: str) -> dict:
        """A device by id, name (user-set or integration), or one of its entity_ids."""
        entry = self.entries.get(ref)
        if entry is not None:
            device = self.devices.get(entry.get("device_id") or "")
            if device is None:
                raise NotFoundError(
                    f"entity {ref} has no device — use update_entity to edit it directly"
                )
            return device
        return _resolve(
            "device", ref, self.devices.values(), "id",
            lambda d: [n for n in (d.get("name_by_user"), d.get("name")) if n],
        )


def _resolve(kind: str, ref: str, rows: Iterable[dict], id_key: str, names) -> dict:
    rows = list(rows)
    for row in rows:
        if row[id_key] == ref:
            return row
    wanted = ref.casefold()
    hits = [r for r in rows if wanted in {n.casefold() for n in names(r)} or r[id_key].casefold() == wanted]
    if len(hits) == 1:
        return hits[0]
    if hits:
        matches = ", ".join(
            f"{r[id_key]} ({names(r)[0]})" if names(r) else r[id_key]
            for r in sorted(hits, key=lambda r: r[id_key])
        )
        raise StudioHaysError(f"ambiguous {kind} {ref!r} — matches: {matches} (use the id)")
    known = ", ".join(sorted({names(r)[0] if names(r) else r[id_key] for r in rows}, key=str.casefold))
    raise NotFoundError(f"unknown {kind} {ref!r} — known: {known or 'none'}")
