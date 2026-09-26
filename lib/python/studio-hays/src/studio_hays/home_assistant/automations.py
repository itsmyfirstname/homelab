"""Automations (STU-16), over HA 2026.9's automation config view.

HA has no list endpoint: automations are listed from their ``automation.*``
entities, whose ``id`` attribute is the config id used by
``/api/config/automation/config/{id}`` (GET / POST / DELETE). Saves are
validated first with the WebSocket ``validate_config`` command, which is also
what ``dry_run`` runs. Configs are passed through verbatim; HA accepts both
the 2026 plural keys (``triggers``/``conditions``/``actions``) and the older
singular ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..core import NotFoundError, StudioHaysError
from .registry import Registry, slugify

if TYPE_CHECKING:
    from .client import Client

CONFIG_PATH = "/api/config/automation/config/"
SECTIONS = (("triggers", "trigger"), ("conditions", "condition"), ("actions", "action"))


def _automation_states(c: "Client") -> dict[str, dict]:
    """entity_id -> HA state dict, for every ``automation.*`` entity with a state."""
    return {
        r["id"]: r["attributes"] for r in c.store.get_snapshot("entity")
        if r["id"].startswith("automation.")
    }


def list_automations(c: "Client", fresh: bool = False) -> dict:
    """Automations from their entities, joined with the entity registry: area,
    labels, and disabled automations (registered, but without a state)."""
    c._sync(fresh)
    reg = Registry(c.store)
    states = _automation_states(c)
    entity_ids = sorted(set(states) | {e for e in reg.entries if e.startswith("automation.")})
    rows = []
    for eid in entity_ids:
        state = states.get(eid) or {}
        attrs = state.get("attributes") or {}
        entry = reg.entries.get(eid) or {}
        rows.append({
            "entity_id": eid,
            # the config id; for entities it is also the registry unique_id
            "automation_id": attrs.get("id") or entry.get("unique_id"),
            "alias": attrs.get("friendly_name") or entry.get("name") or entry.get("original_name"),
            "state": state.get("state"),
            "last_triggered": attrs.get("last_triggered"),
            "mode": attrs.get("mode"),
            "area": reg.area_ref(reg.area_of(eid)[0]),
            "labels": reg.label_refs(entry.get("labels") or []),
            "disabled": bool(entry.get("disabled_by")),
        })
    return c._envelope(automations=rows)


def _fetch(c: "Client", automation_id: str) -> dict:
    return c._request(
        "GET", CONFIG_PATH + automation_id,
        missing=(
            f"no automation with id {automation_id!r} in automations.yaml — list_automations "
            "shows the ids (automations from YAML packages can't be edited here)"
        ),
    )


def _entity_id(c: "Client", automation_id: str) -> str | None:
    for eid, state in _automation_states(c).items():
        if (state.get("attributes") or {}).get("id") == automation_id:
            return eid
    return None


def get_automation(c: "Client", automation_id: str) -> dict:
    c._sync()
    config = _fetch(c, automation_id)
    return c._envelope(
        automation_id=automation_id, entity_id=_entity_id(c, automation_id), config=config
    )


def _validate(c: "Client", automation_id: str, config: Mapping) -> None:
    sections = {}
    for plural, singular in SECTIONS:
        value = config.get(plural, config.get(singular))
        if value is not None:
            sections[plural] = value if isinstance(value, list) else [value]
    with c._ws() as session:
        report = session.call("validate_config", **sections) or {}
    errors = [f"{k}: {v.get('error')}" for k, v in report.items() if not v.get("valid", True)]
    if errors:
        raise StudioHaysError(f"automation {automation_id!r} is invalid: {'; '.join(errors)}")


def save_automation(
    c: "Client", config: Mapping, automation_id: str | None = None, dry_run: bool = False
) -> dict:
    """Create or replace an automation (validated by HA first)."""
    c._sync()
    config = dict(config)
    given = config.get("id")
    if automation_id and given and str(given) != automation_id:
        raise StudioHaysError(
            f"config id {given!r} doesn't match automation_id {automation_id!r}"
        )
    explicit = bool(automation_id or given)
    automation_id = automation_id or (str(given) if given else slugify(str(config.get("alias") or "")))
    if not automation_id:
        raise StudioHaysError("an automation needs an id or an alias to derive one from")
    config["id"] = automation_id
    try:
        before: dict | None = _fetch(c, automation_id)
    except NotFoundError:
        before = None
    if before is not None and not explicit:
        raise StudioHaysError(
            f"an automation with id {automation_id!r} already exists — pass "
            f"automation_id={automation_id!r} to replace it"
        )
    _validate(c, automation_id, config)
    result: dict[str, Any] = {
        "automation_id": automation_id,
        "dry_run": dry_run,
        "action": "replace" if before else "create",
        "valid": True,
        "before": before,
        "after": config,
    }
    if dry_run:
        return result
    verb = "update" if before else "create"
    reply = c._audited(
        verb, "automation", automation_id, config,
        lambda: c._request("POST", CONFIG_PATH + automation_id, config),
    )
    c.store.log_op(verb, "automation", automation_id, args=config, result=reply)
    c._dirty = True  # HA reloads automations; the automation.* entity changes
    result["after"] = _fetch(c, automation_id)
    c._sync()
    result["entity_id"] = _entity_id(c, automation_id)
    return result


def delete_automation(c: "Client", automation_id: str, dry_run: bool = False) -> dict:
    c._sync()
    before = _fetch(c, automation_id)
    result = {
        "automation_id": automation_id,
        "entity_id": _entity_id(c, automation_id),
        "dry_run": dry_run,
        "before": before,
        "after": None,
    }
    if dry_run:
        return result
    reply = c._audited(
        "delete", "automation", automation_id, None,
        lambda: c._request("DELETE", CONFIG_PATH + automation_id),
    )
    c.store.log_op("delete", "automation", automation_id, result=reply)
    c._dirty = True
    return result
