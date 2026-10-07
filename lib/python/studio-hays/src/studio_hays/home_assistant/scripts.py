"""Scripts, over HA 2026.9's script config view.

HA has no list endpoint: scripts are listed from their ``script.*``
entities, whose ``id`` attribute (or registry ``unique_id``) is the config
id used by ``/api/config/script/config/{id}`` (GET / POST / DELETE). Saves
are validated first with the WebSocket ``validate_config`` command, which is
also what ``dry_run`` runs. Configs are passed through verbatim; HA accepts
both ``sequence`` and ``actions``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..core import NotFoundError, StudioHaysError
from .registry import Registry, slugify

if TYPE_CHECKING:
    from .client import Client

CONFIG_PATH = "/api/config/script/config/"


def _script_states(c: "Client") -> dict[str, dict]:
    """entity_id -> HA state dict, for every ``script.*`` entity with a state."""
    return {
        r["id"]: r["attributes"] for r in c.store.get_snapshot("entity")
        if r["id"].startswith("script.")
    }


def list_scripts(c: "Client", fresh: bool = False) -> dict:
    """Scripts from their entities, joined with the entity registry: area,
    labels, and disabled scripts (registered, but without a state)."""
    c._sync(fresh)
    reg = Registry(c.store)
    states = _script_states(c)
    entity_ids = sorted(set(states) | {e for e in reg.entries if e.startswith("script.")})
    rows = []
    for eid in entity_ids:
        state = states.get(eid) or {}
        attrs = state.get("attributes") or {}
        entry = reg.entries.get(eid) or {}
        rows.append({
            "entity_id": eid,
            "script_id": attrs.get("id") or entry.get("unique_id"),
            "alias": attrs.get("friendly_name") or entry.get("name") or entry.get("original_name"),
            "state": state.get("state"),
            "last_triggered": attrs.get("last_triggered"),
            "mode": attrs.get("mode"),
            "area": reg.area_ref(reg.area_of(eid)[0]),
            "labels": reg.label_refs(entry.get("labels") or []),
            "disabled": bool(entry.get("disabled_by")),
        })
    return c._envelope(scripts=rows)


def _fetch(c: "Client", script_id: str) -> dict:
    return c._request(
        "GET", CONFIG_PATH + script_id,
        missing=(
            f"no script with id {script_id!r} in scripts.yaml — list_scripts "
            "shows the ids (scripts from YAML packages can't be edited here)"
        ),
    )


def _entity_id(c: "Client", script_id: str) -> str | None:
    for eid, state in _script_states(c).items():
        if (state.get("attributes") or {}).get("id") == script_id:
            return eid
    return None


def get_script(c: "Client", script_id: str) -> dict:
    c._sync()
    config = _fetch(c, script_id)
    return c._envelope(
        script_id=script_id, entity_id=_entity_id(c, script_id), config=config
    )


def _validate(c: "Client", script_id: str, config: Mapping) -> None:
    value = config.get("actions", config.get("action", config.get("sequence")))
    sections: dict[str, Any] = {}
    if value is not None:
        sections["actions"] = value if isinstance(value, list) else [value]
    with c._ws() as session:
        report = session.call("validate_config", **sections) or {}
    errors = [f"{k}: {v.get('error')}" for k, v in report.items() if not v.get("valid", True)]
    if errors:
        raise StudioHaysError(f"script {script_id!r} is invalid: {'; '.join(errors)}")


def save_script(
    c: "Client", config: Mapping, script_id: str | None = None, dry_run: bool = False
) -> dict:
    """Create or replace a script (validated by HA first)."""
    c._sync()
    config = dict(config)
    given = config.get("id")
    if script_id and given and str(given) != script_id:
        raise StudioHaysError(
            f"config id {given!r} doesn't match script_id {script_id!r}"
        )
    explicit = bool(script_id or given)
    script_id = script_id or (str(given) if given else slugify(str(config.get("alias") or "")))
    if not script_id:
        raise StudioHaysError("a script needs an id or an alias to derive one from")
    config["id"] = script_id
    try:
        before: dict | None = _fetch(c, script_id)
    except NotFoundError:
        before = None
    if before is not None and not explicit:
        raise StudioHaysError(
            f"a script with id {script_id!r} already exists — pass "
            f"script_id={script_id!r} to replace it"
        )
    _validate(c, script_id, config)
    result: dict[str, Any] = {
        "script_id": script_id,
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
        verb, "script", script_id, config,
        lambda: c._request("POST", CONFIG_PATH + script_id, config),
    )
    c.store.log_op(verb, "script", script_id, args=config, result=reply)
    c._dirty = True  # HA reloads scripts; the script.* entity changes
    result["after"] = _fetch(c, script_id)
    c._sync()
    result["entity_id"] = _entity_id(c, script_id)
    return result


def delete_script(c: "Client", script_id: str, dry_run: bool = False) -> dict:
    c._sync()
    before = _fetch(c, script_id)
    result = {
        "script_id": script_id,
        "entity_id": _entity_id(c, script_id),
        "dry_run": dry_run,
        "before": before,
        "after": None,
    }
    if dry_run:
        return result
    reply = c._audited(
        "delete", "script", script_id, None,
        lambda: c._request("DELETE", CONFIG_PATH + script_id),
    )
    c.store.log_op("delete", "script", script_id, result=reply)
    c._dirty = True
    return result
