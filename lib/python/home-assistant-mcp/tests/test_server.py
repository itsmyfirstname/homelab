"""The MCP tool surface opencode relies on, backed by studio-hays.

``FakeHomeAssistant`` (from studio-hays) fakes REST at the transport hook and
serves the WebSocket API from a real local server, so the real client, cache
and tool wiring all run.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client as MCPClient
from studio_hays.home_assistant import Client
from studio_hays.home_assistant.testing import FakeHomeAssistant

from home_assistant_mcp.server import mcp

URL = FakeHomeAssistant.url
TOKEN = "test-token"
PLUG = "dev-plug"


def _state(entity_id: str, state: str, name: str) -> dict:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {"friendly_name": name},
        "last_changed": "2026-09-24T00:00:00+00:00",
        "last_updated": "2026-09-24T00:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HA_URL", URL)
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("STUDIOHAYS_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("STUDIOHAYS_DATA_DIR", str(tmp_path / "data"))
    Client.reset()
    with FakeHomeAssistant(
        [
            _state("light.kitchen", "off", "Kitchen Light"),
            _state("light.bedroom", "on", "Bedroom Light"),
            _state("sensor.hall_temp", "21.5", "Hallway Temperature"),
            _state("switch.reading_lamp", "off", "Reading Lamp"),
        ],
        token=TOKEN,
        areas=[
            {"area_id": "guest_bedroom", "name": "Guest bedroom"},
            {"area_id": "living_room", "name": "Living Room"},
        ],
        labels=[{"label_id": "first_floor", "name": "first_floor"},
                {"label_id": "third_floor", "name": "third_floor"}],
        devices=[{"id": PLUG, "name": "Smart Plug", "name_by_user": "Reading Lamp",
                  "area_id": "guest_bedroom", "labels": ["third_floor"]}],
        entries=[{"entity_id": "switch.reading_lamp", "device_id": PLUG, "has_entity_name": True}],
    ) as ha:
        yield ha
    Client.reset()


def call(name: str, args: dict | None = None):
    async def main():
        async with MCPClient(mcp) as session:
            return await session.call_tool(name, args or {})

    return asyncio.run(main())


def text(result) -> str:
    return getattr(result.content[0], "text")


def payload(result) -> dict:
    assert not result.is_error, text(result)
    return json.loads(text(result))


def tools() -> dict:
    async def main():
        async with MCPClient(mcp) as session:
            return {t.name: t for t in (await session.list_tools()).tools}

    return asyncio.run(main())


# the original four tools: names and shapes unchanged ---------------------------------

def test_original_tools_are_still_there():
    assert {"list_entities", "get_entity", "list_domains", "call_service"} <= set(tools())


def test_list_entities_filters_and_counts():
    out = payload(call("list_entities", {"domain": "light"}))
    assert out["stale"] is False and out["total"] == 2
    assert out["entities"] == [
        {"entity_id": "light.bedroom", "state": "on", "friendly_name": "Bedroom Light"},
        {"entity_id": "light.kitchen", "state": "off", "friendly_name": "Kitchen Light"},
    ]


def test_get_entity_returns_flat_state_with_attributes():
    out = payload(call("get_entity", {"entity_id": "light.kitchen"}))
    assert out["entity_id"] == "light.kitchen" and out["state"] == "off"
    assert out["attributes"] == {"friendly_name": "Kitchen Light"}
    assert "last_synced" in out and out["stale"] is False


def test_list_domains():
    assert payload(call("list_domains"))["domains"] == [
        {"domain": "light", "count": 2},
        {"domain": "sensor", "count": 1},
        {"domain": "switch", "count": 1},
    ]


def test_call_service_then_read_sees_the_change():
    out = payload(
        call("call_service", {"domain": "light", "service": "turn_on",
                              "service_data": {"entity_id": "light.kitchen"}})
    )
    assert out["changed_entities"] == ["light.kitchen"]
    assert payload(call("get_entity", {"entity_id": "light.kitchen"}))["state"] == "on"


def test_unknown_entity_is_a_tool_error():
    result = call("get_entity", {"entity_id": "light.nope"})
    assert result.is_error and "entity not found: light.nope" in text(result)


def test_missing_env_is_a_readable_tool_error(monkeypatch):
    monkeypatch.delenv("HA_URL")
    result = call("list_entities")
    assert result.is_error and "HA_URL and HA_TOKEN must be set" in text(result)


# registry tools (STU-12) ---------------------------------------------------------------

def test_registry_tools_are_exposed_and_writes_say_mutating():
    by_name = tools()
    new = {"list_areas", "list_labels", "get_device", "update_entity", "update_device",
           "repurpose_device", "create_label", "create_area"}
    assert new <= set(by_name)
    for name in ("update_entity", "update_device", "repurpose_device", "create_label", "create_area"):
        assert "Mutating" in (by_name[name].description or "")
        assert "dry_run" in by_name[name].input_schema["properties"]


def test_list_entities_by_area_and_get_entity_registry_info():
    out = payload(call("list_entities", {"area": "guest bedroom"}))
    assert [e["entity_id"] for e in out["entities"]] == ["switch.reading_lamp"]
    reg = payload(call("get_entity", {"entity_id": "switch.reading_lamp"}))["registry"]
    assert reg["device"] == {"id": PLUG, "name": "Reading Lamp"}
    assert reg["area_source"] == "device"


def test_list_areas_labels_and_get_device():
    assert [a["name"] for a in payload(call("list_areas"))["areas"]] == ["Guest bedroom", "Living Room"]
    assert [x["name"] for x in payload(call("list_labels"))["labels"]] == ["first_floor", "third_floor"]
    dev = payload(call("get_device", {"device": "switch.reading_lamp"}))
    assert dev["device"]["name"] == "Reading Lamp"


def test_repurpose_device_dry_run_then_apply(home):
    args = {"device": "Reading Lamp", "area": "Living Room", "name": "Aquarium Light",
            "add_labels": ["first_floor"], "remove_labels": ["third_floor"]}
    plan = payload(call("repurpose_device", {**args, "dry_run": True}))
    assert plan["dry_run"] is True and plan["plan"][0]["target"] == PLUG
    assert home.devices[PLUG]["name_by_user"] == "Reading Lamp"

    done = payload(call("repurpose_device", args))
    assert done["ok"] is True
    assert home.devices[PLUG]["area_id"] == "living_room"
    assert payload(call("get_device", {"device": "Aquarium Light"}))["device"]["labels"] == [
        {"label_id": "first_floor", "name": "first_floor"}
    ]


def test_update_entity_and_create_label():
    payload(call("create_label", {"name": "Aquarium"}))
    out = payload(call("update_entity", {"entity_id": "switch.reading_lamp", "add_labels": ["aquarium"]}))
    assert out["changes"] == {"labels": {"was": [], "is": ["aquarium"]}}


def test_empty_area_clears_and_omitted_fields_are_untouched(home):
    payload(call("update_device", {"device": PLUG, "area": ""}))
    assert home.devices[PLUG]["area_id"] is None
    assert home.devices[PLUG]["name_by_user"] == "Reading Lamp"


# STU-15: HA 2026.9 drift -------------------------------------------------------------

def test_update_area_tool_renames_and_sets_floor(home):
    tool = tools()["update_area"]
    assert "Mutating" in (tool.description or "") and "dry_run" in tool.input_schema["properties"]
    home.floors["main"] = {"floor_id": "main", "name": "Main", "level": 1}
    out = payload(call("update_area", {"area": "Guest bedroom", "name": "Guest Room", "floor": "Main"}))
    assert out["changes"] == {
        "name": {"was": "Guest bedroom", "is": "Guest Room"},
        "floor_id": {"was": None, "is": "main"},
    }
    assert home.areas["guest_bedroom"]["name"] == "Guest Room"


def test_update_area_name_clash_is_explained(home):
    result = call("update_area", {"area": "Guest bedroom", "name": "livingroom"})
    assert result.is_error and "already used by area 'Living Room'" in text(result)
    assert home.areas["guest_bedroom"]["name"] == "Guest bedroom"


def test_call_service_errors_include_home_assistants_reason():
    result = call("call_service", {"domain": "homeassistant", "service": "update_area",
                                   "service_data": {"area_id": "x", "name": "y"}})
    assert result.is_error and "Home Assistant has no service homeassistant.update_area" in text(result)


def test_call_service_return_response():
    out = payload(call("call_service", {"domain": "weather", "service": "get_forecasts",
                                        "service_data": {"entity_id": "weather.home"},
                                        "return_response": True}))
    assert out["response"] == {"weather.home": {"forecast": [{"condition": "sunny"}]}}


def test_return_response_inside_service_data_is_a_readable_error():
    result = call("call_service", {"domain": "weather", "service": "get_forecasts",
                                   "service_data": {"return_response": True}})
    assert result.is_error and "return_response is a call option, not service data" in text(result)


# STU-16/17/18 ---------------------------------------------------------------------------

AUTOMATION = {
    "alias": "Hallway wake-up",
    "triggers": [{"trigger": "state", "entity_id": "binary_sensor.occupancy", "to": "on"}],
    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}],
}


def test_automation_tools_round_trip(home):
    by_name = tools()
    for name in ("list_automations", "get_automation", "save_automation", "delete_automation"):
        assert name in by_name
    for name in ("save_automation", "delete_automation", "delete_area"):
        assert "Mutating" in (by_name[name].description or "")

    plan = payload(call("save_automation", {"config": AUTOMATION, "automation_id": "wake", "dry_run": True}))
    assert plan["action"] == "create" and "wake" not in home.automations
    payload(call("save_automation", {"config": AUTOMATION, "automation_id": "wake"}))
    assert payload(call("get_automation", {"automation_id": "wake"}))["config"]["alias"] == "Hallway wake-up"
    assert [a["automation_id"] for a in payload(call("list_automations"))["automations"]] == ["wake"]
    payload(call("delete_automation", {"automation_id": "wake"}))
    assert "wake" not in home.automations


def test_invalid_automation_is_a_tool_error(home):
    bad = {**AUTOMATION, "triggers": [{"trigger": "nonsense"}]}
    result = call("save_automation", {"config": bad, "automation_id": "wake"})
    assert result.is_error and "Invalid trigger 'nonsense'" in text(result)


def test_delete_area_refuses_then_forces(home):
    result = call("delete_area", {"area": "Guest bedroom"})
    assert result.is_error and "still has 1 device(s)" in text(result)
    payload(call("delete_area", {"area": "Guest bedroom", "force": True}))
    assert "guest_bedroom" not in home.areas


def test_call_service_reports_targets_and_async_note(home):
    home.async_entities.add("light.kitchen")
    out = payload(call("call_service", {"domain": "light", "service": "turn_on",
                                        "service_data": {"entity_id": "light.kitchen"}}))
    assert out["targets"] == ["light.kitchen"] and out["changed_entities"] == []
    assert "verify with get_entity" in out["note"]
