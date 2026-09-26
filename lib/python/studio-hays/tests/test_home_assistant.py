"""home_assistant module, tested at its public seams (M2, M2.1).

``FakeHomeAssistant`` fakes REST at httpx's transport and serves the
WebSocket API from a real local server, so the module's request building,
WS protocol handling and error mapping all run.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from studio_hays.core import ConfigError, NotFoundError, RemoteError, StudioHaysError
from studio_hays.home_assistant import Client
from studio_hays.home_assistant.testing import FakeHomeAssistant

URL = "http://ha.test:8123"
TOKEN = "test-token"


def entity(entity_id: str, state: str, name: str | None = None, **attrs: object) -> dict:
    attributes = {"friendly_name": name, **attrs} if name else dict(attrs)
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attributes,
        "last_changed": "2026-09-24T00:00:00+00:00",
        "last_updated": "2026-09-24T00:00:00+00:00",
    }


HOUSE = [
    entity("light.kitchen", "off", "Kitchen Light", supported_features=391),
    entity("light.bedroom", "on", "Bedroom Light"),
    entity("sensor.hall_temp", "21.5", "Hallway Temperature"),
    entity("switch.garage", "off", "Garage Door"),
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIOHAYS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STUDIOHAYS_ENV_FILE", str(tmp_path / "no.env"))
    for var in ("STUDIOHAYS_HA_URL", "STUDIOHAYS_HA_TOKEN", "STUDIOHAYS_HA_DB"):
        monkeypatch.delenv(var, raising=False)
    Client.reset()
    yield
    Client.reset()


@pytest.fixture
def fake(monkeypatch):
    """A configured client environment talking to a seeded fake HA."""
    monkeypatch.setenv("STUDIOHAYS_HA_URL", URL)
    monkeypatch.setenv("STUDIOHAYS_HA_TOKEN", TOKEN)
    with FakeHomeAssistant(HOUSE, token=TOKEN) as ha:
        yield ha


# lazy config ------------------------------------------------------------------

def test_missing_config_raises_at_first_use_not_construction():
    c = Client()  # no env: must not raise
    with pytest.raises(ConfigError, match="STUDIOHAYS_HA_URL"):
        c.get.entities()


# reads ------------------------------------------------------------------------

def test_get_entity_on_fresh_db_syncs_then_returns_full_state(fake):
    env = Client().get.entity("light.kitchen")
    assert env["stale"] is False
    assert env["entity"]["state"] == "off"
    assert env["entity"]["attributes"] == {
        "friendly_name": "Kitchen Light",
        "supported_features": 391,
    }


def test_get_entities_returns_compact_rows_filtered_by_domain_and_search(fake):
    ha = Client()
    lights = ha.get.entities(domain="light")
    assert lights["total"] == 2
    assert lights["entities"] == [
        {"entity_id": "light.bedroom", "state": "on", "friendly_name": "Bedroom Light"},
        {"entity_id": "light.kitchen", "state": "off", "friendly_name": "Kitchen Light"},
    ]
    # search matches entity_id or friendly name, case-insensitively
    assert [e["entity_id"] for e in ha.get.entities(search="HALLWAY")["entities"]] == [
        "sensor.hall_temp"
    ]
    capped = ha.get.entities(limit=1)
    assert capped["total"] == 4 and len(capped["entities"]) == 1


def test_get_domains_counts_largest_first(fake):
    assert Client().get.domains()["domains"] == [
        {"domain": "light", "count": 2},
        {"domain": "sensor", "count": 1},
        {"domain": "switch", "count": 1},
    ]


def test_query_searches_local_state(fake):
    ha = Client()
    assert [e["entity_id"] for e in ha.query("kitchen")["entities"]] == ["light.kitchen"]
    assert ha.query("garage", domain="light")["entities"] == []


def test_unknown_entity_raises_not_found(fake):
    with pytest.raises(NotFoundError, match="light.nope"):
        Client().get.entity("light.nope")


def test_reads_refresh_only_when_stale_and_fall_back_to_cache_when_ha_is_down(
    fake, monkeypatch
):
    ha = Client()
    ha.get.entities()
    fake.states["light.kitchen"]["state"] = "on"  # changed behind our back

    # within the staleness window: served from state, no network
    pulls = len(fake.calls)
    assert ha.get.entity("light.kitchen")["entity"]["state"] == "off"
    assert len(fake.calls) == pulls

    # stale: the read refreshes first
    monkeypatch.setattr(Client, "staleness_window", 0.0)
    assert ha.get.entity("light.kitchen")["entity"]["state"] == "on"

    # stale and HA down: cached data, flagged stale
    fake.down = True
    env = ha.get.entity("light.kitchen")
    assert env["entity"]["state"] == "on" and env["stale"] is True


def test_first_read_with_ha_unreachable_raises_remote_error(fake):
    fake.down = True
    with pytest.raises(RemoteError, match="cannot reach Home Assistant at http://ha.test:8123"):
        Client().get.entities()


def test_rejected_token_names_the_env_var(fake, monkeypatch):
    monkeypatch.setenv("STUDIOHAYS_HA_TOKEN", "wrong")
    with pytest.raises(RemoteError, match=r"rejected the access token \(HTTP 401\).*STUDIOHAYS_HA_TOKEN"):
        Client().get.entities()


# service calls -------------------------------------------------------------------

def test_call_service_hits_ha_and_next_read_reflects_it(fake):
    ha = Client()
    assert ha.get.entity("light.kitchen")["entity"]["state"] == "off"

    result = ha.call.service("light", "turn_on", entity_id="light.kitchen", brightness_pct=50)
    assert result == {"service": "light.turn_on", "changed_entities": ["light.kitchen"]}
    assert fake.calls[-1] == (
        "POST",
        "/api/services/light/turn_on",
        {"entity_id": "light.kitchen", "brightness_pct": 50},
    )
    # still inside the staleness window, but the call invalidated state
    assert ha.get.entity("light.kitchen")["entity"]["state"] == "on"
    (last_call,) = [op for op in ha.state.history()["ops"] if op["op"] == "call"]
    assert last_call["ok"] and last_call["args"]["service"] == "light.turn_on"


def test_failed_service_call_raises_and_is_audited(fake):
    ha = Client()
    with pytest.raises(RemoteError, match="HTTP 400"):
        ha.call.service("light", "explode", entity_id="light.kitchen")
    assert [op["ok"] for op in ha.state.history()["ops"] if op["op"] == "call"] == [False]


# state verbs ----------------------------------------------------------------------

def test_drift_reports_identity_changes_not_volatile_ones(fake):
    ha = Client()
    ha.state.refresh()
    fake.states["sensor.hall_temp"]["state"] = "22.0"  # volatile: not drift
    fake.states["light.kitchen"]["attributes"]["friendly_name"] = "Galley Light"
    fake.states["light.porch"] = entity("light.porch", "off", "Porch")
    del fake.states["switch.garage"]

    report = ha.state.drift()
    assert report["module"] == "home_assistant" and report["host"] == URL
    assert report["added"] == [{"type": "entity", "id": "light.porch"}]
    assert report["removed"] == [{"type": "entity", "id": "switch.garage"}]
    assert report["changed"] == [
        {
            "type": "entity",
            "id": "light.kitchen",
            "diff": {"attributes.friendly_name": {"was": "Kitchen Light", "is": "Galley Light"}},
        }
    ]
    assert report["count"] == 3


def test_show_filters_the_snapshot_by_id(fake):
    ha = Client()
    ha.state.refresh()
    shown = ha.state.show(id="light.kitchen")
    assert [e["entity_id"] for e in shown["entities"]] == ["light.kitchen"]
    assert len(ha.state.show()["entities"]) == 4


def test_deleted_db_rebuilds_on_next_read(fake):
    ha = Client()
    ha.state.refresh()
    db = ha.config.db_path
    ha.state.reset()
    assert not db.exists()
    assert ha.get.entity("light.bedroom")["entity"]["state"] == "on"
    assert db.exists()


# CLI surface -------------------------------------------------------------------------

def run_cli(capsys, *argv: str) -> tuple[int, Any]:
    from studio_hays import cli

    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_cli_list_entities_prints_the_python_api_result(fake, capsys):
    code, out = run_cli(capsys, "ha", "list", "entities", "--domain", "light")
    assert code == 0
    assert out == Client().get.entities(domain="light")


def test_cli_get_entity_positional_and_not_found_exit_code(fake, capsys):
    code, out = run_cli(capsys, "ha", "get", "entity", "light.kitchen", "--fresh")
    assert code == 0 and out["entity"]["entity_id"] == "light.kitchen"
    code, out = run_cli(capsys, "ha", "get", "entity", "light.nope")
    assert code == 2 and "light.nope" in out["error"]


def test_cli_call_service_with_json_data(fake, capsys):
    code, out = run_cli(
        capsys, "ha", "call", "light", "turn_on", "--data", '{"entity_id": "light.kitchen"}'
    )
    assert code == 0
    assert out == {"service": "light.turn_on", "changed_entities": ["light.kitchen"]}


def test_cli_state_verbs_and_legacy_op_names(fake, capsys):
    code, out = run_cli(capsys, "ha", "state", "refresh")
    assert code == 0 and out["counts"]["entity"] == 4
    code, out = run_cli(capsys, "ha", "state", "history", "1")
    assert code == 0 and [op["op"] for op in out["ops"]] == ["refresh"]
    code, out = run_cli(capsys, "ha", "get_entity", "entity_id=light.bedroom")
    assert code == 0 and out["entity"]["state"] == "on"


def test_cli_missing_config_is_a_one_line_json_error(capsys):
    code, out = run_cli(capsys, "ha", "list", "domains")
    assert code == 1 and "STUDIOHAYS_HA_URL" in out["error"]


# MCP surface --------------------------------------------------------------------------

def mcp_session(coro_fn):
    """Run ``coro_fn(client)`` against the HA MCP server, in-process."""
    import asyncio

    from mcp import Client as MCPClient

    from studio_hays.home_assistant.mcp import create_server

    async def main():
        async with MCPClient(create_server()) as session:
            return await coro_fn(session)

    return asyncio.run(main())


def tool_json(result) -> Any:
    (content,) = result.content
    return json.loads(content.text)


def test_mcp_tools_are_generated_from_the_registry(fake):
    async def tools(session):
        return {t.name: t for t in (await session.list_tools()).tools}

    by_name = mcp_session(tools)
    # doc 00 success criterion 4
    assert {"ha_list_entities", "ha_get_entity", "ha_refresh", "ha_drift"} <= set(by_name)
    assert "ha_call_service" in by_name and "Mutating" in by_name["ha_call_service"].description
    schema = by_name["ha_get_entity"].input_schema
    assert schema["required"] == ["entity_id"]
    assert set(schema["properties"]) == {"entity_id", "fresh"}


def test_mcp_tool_call_returns_the_python_api_result(fake):
    async def call(session):
        return await session.call_tool("ha_list_entities", {"domain": "light"})

    result = mcp_session(call)
    assert not result.is_error
    assert tool_json(result) == Client().get.entities(domain="light")


def test_mcp_errors_come_back_as_tool_errors(fake):
    async def call(session):
        return await session.call_tool("ha_get_entity", {"entity_id": "light.nope"})

    result = mcp_session(call)
    assert result.is_error
    assert "entity not found: light.nope" in result.content[0].text


@pytest.mark.parametrize(
    "argv", [["ha", "mcp"], ["mcp", "--modules", "home_assistant"]], ids=["per-module", "aggregate"]
)
def test_cli_serves_mcp_over_stdio(tmp_path, argv):
    import asyncio
    import os
    import sys

    from mcp import Client as MCPClient
    from mcp import StdioServerParameters

    env = dict(
        os.environ,
        STUDIOHAYS_HA_URL="http://unused.invalid:8123",
        STUDIOHAYS_HA_TOKEN="t",
        STUDIOHAYS_DATA_DIR=str(tmp_path),
        STUDIOHAYS_ENV_FILE=str(tmp_path / "no.env"),
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "studio_hays.cli", *argv], env=env
    )

    async def main():
        async with MCPClient(params) as session:
            return {t.name for t in (await session.list_tools()).tools}

    assert "ha_get_entity" in asyncio.run(main())


def test_cli_mcp_with_missing_config_fails_fast(capsys):
    code, out = run_cli(capsys, "mcp", "--modules", "home_assistant")
    assert code == 1 and "STUDIOHAYS_HA_URL" in out["error"]


def test_read_after_call_reports_stale_when_the_refresh_fails(fake):
    ha = Client()
    ha.get.entities()
    ha.call.service("light", "turn_on", entity_id="light.kitchen")
    fake.down = True
    env = ha.get.entity("light.kitchen")
    assert env["stale"] is True


def test_cli_invalid_json_data_is_a_json_error_not_a_usage_exit(fake, capsys):
    code, out = run_cli(capsys, "ha", "call", "light", "turn_on", "--data", "{nope")
    assert code == 1 and "--data" in out["error"]


# embedding: a host project passes its own config ------------------------------------

def test_host_project_can_pass_url_token_and_db_as_kwargs(tmp_path):
    db = tmp_path / "host" / "cache.db"
    with FakeHomeAssistant(HOUSE, token=TOKEN):
        c = Client(url=URL, token=TOKEN, db=str(db))
        assert c.config.db_path == db
        assert c.get.entity("light.kitchen")["entity"]["state"] == "off"
    assert db.exists()



# M2.1: registries (STU-7 transport, STU-8 mirror) ------------------------------------

PLUG = "dev-plug"
AREAS = [
    {"area_id": "guest_bedroom", "name": "Guest bedroom", "floor_id": "upstairs"},
    {"area_id": "living_room", "name": "Living Room", "floor_id": "main"},
    {"area_id": "kitchen", "name": "Kitchen", "floor_id": "main"},
]
LABELS = [
    {"label_id": "first_floor", "name": "first_floor"},
    {"label_id": "third_floor", "name": "third_floor"},
    {"label_id": "outdoor", "name": "Outdoor"},
]
FLOORS = [{"floor_id": "main", "name": "Main", "level": 0}, {"floor_id": "upstairs", "name": "Upstairs", "level": 1}]
DEVICES = [
    {"id": PLUG, "name": "Smart Plug Mini", "name_by_user": "Reading Lamp",
     "area_id": "guest_bedroom", "labels": ["third_floor"]},
    {"id": "dev-kitchen", "name": "Kitchen Bulb", "area_id": "kitchen"},
]
STATES = [
    entity("switch.reading_lamp", "off", "Reading Lamp"),
    entity("sensor.reading_lamp_power", "0.0", "Reading Lamp Power"),
    entity("button.reading_lamp_identify", "unknown", "Reading Lamp Identify"),
    entity("light.kitchen", "off", "Kitchen Light"),
    entity("sensor.outside_temp", "12.0", "Outside Temperature"),
    entity("sun.sun", "above_horizon", "Sun"),
]
ENTRIES = [
    {"entity_id": "switch.reading_lamp", "device_id": PLUG, "has_entity_name": True, "original_name": None},
    {"entity_id": "sensor.reading_lamp_power", "device_id": PLUG, "has_entity_name": True, "original_name": "Power"},
    {"entity_id": "button.reading_lamp_identify", "device_id": PLUG, "has_entity_name": True,
     "original_name": "Identify", "name": "Reading Lamp Identify"},
    {"entity_id": "light.kitchen", "device_id": "dev-kitchen", "has_entity_name": True, "original_name": None},
    {"entity_id": "sensor.outside_temp", "area_id": "kitchen", "labels": ["outdoor"]},
]


@pytest.fixture
def home(monkeypatch):
    """The Reading Lamp scenario: a plug upstairs in the guest bedroom."""
    monkeypatch.setenv("STUDIOHAYS_HA_URL", URL)
    monkeypatch.setenv("STUDIOHAYS_HA_TOKEN", TOKEN)
    with FakeHomeAssistant(
        STATES, token=TOKEN, devices=DEVICES, areas=AREAS, labels=LABELS,
        floors=FLOORS, entries=ENTRIES, unregistered=["sun.sun"],
    ) as ha:
        yield ha


def test_refresh_mirrors_registries_and_drift_sees_ui_side_changes(home):
    ha = Client()
    ha.state.refresh()
    assert [c for c, _ in home.commands] == [
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
        "config/label_registry/list",
        "config/floor_registry/list",
    ]
    # someone moves and relabels the plug, and renames an area, in the HA UI
    home.devices[PLUG].update(area_id="living_room", labels=["first_floor"])
    home.areas["kitchen"]["name"] = "Galley"
    home.entries["sensor.outside_temp"]["labels"] = []

    changed = {(d["type"], d["id"]): d["diff"] for d in Client().state.drift()["changed"]}
    assert changed[("device", PLUG)] == {
        "area_id": {"was": "guest_bedroom", "is": "living_room"},
        "labels": {"was": ["third_floor"], "is": ["first_floor"]},
    }
    assert changed[("area", "kitchen")] == {"name": {"was": "Kitchen", "is": "Galley"}}
    assert changed[("registry_entry", "reg-sensor.outside_temp")] == {
        "labels": {"was": ["outdoor"], "is": []}
    }


def test_websocket_auth_failure_names_the_token(monkeypatch):
    monkeypatch.setenv("STUDIOHAYS_HA_URL", URL)
    monkeypatch.setenv("STUDIOHAYS_HA_TOKEN", TOKEN)
    with FakeHomeAssistant(STATES, token=TOKEN, ws_token="other"):
        with pytest.raises(RemoteError, match=r"rejected the access token \(WebSocket auth_invalid\)"):
            Client().state.refresh()


def test_websocket_unreachable_is_a_remote_error(home):
    Client._ws_url = "ws://127.0.0.1:9/api/websocket"  # nothing listens on the discard port
    with pytest.raises(RemoteError, match="cannot reach Home Assistant WebSocket API"):
        Client().state.refresh()


def test_websocket_command_error_carries_ha_code_and_message(home):
    home.fail_next["config/label_registry/list"] = ("unknown_error", "registry exploded")
    with pytest.raises(
        RemoteError, match=r"config/label_registry/list failed: unknown_error: registry exploded"
    ):
        Client().state.refresh()


# M2.1: registry-aware reads (STU-9) ----------------------------------------------------

def ids(env: dict) -> list[str]:
    return [e["entity_id"] for e in env["entities"]]


def test_entities_filter_by_effective_area_name_or_id(home):
    ha = Client()
    assert ids(ha.get.entities(area="guest BEDROOM")) == [
        "button.reading_lamp_identify", "sensor.reading_lamp_power", "switch.reading_lamp"
    ]
    # light.kitchen inherits from its device; sensor.outside_temp overrides
    assert ids(ha.get.entities(area="kitchen")) == ["light.kitchen", "sensor.outside_temp"]


def test_entities_filter_by_label_on_entity_or_its_device(home):
    ha = Client()
    assert ids(ha.get.entities(label="outdoor")) == ["sensor.outside_temp"]
    assert ids(ha.get.entities(label="Third_Floor", domain="switch")) == ["switch.reading_lamp"]


def test_unknown_area_lists_the_known_ones(home):
    with pytest.raises(NotFoundError, match=r"unknown area 'Attic' — known: Guest bedroom, Kitchen, Living Room"):
        Client().get.entities(area="Attic")


def test_get_entity_adds_registry_info(home):
    reg = Client().get.entity("sensor.reading_lamp_power")["registry"]
    assert reg == {
        "registered": True,
        "registry_id": "reg-sensor.reading_lamp_power",
        "name": None,
        "area": {"area_id": "guest_bedroom", "name": "Guest bedroom"},
        "area_source": "device",
        "floor": {"floor_id": "upstairs", "name": "Upstairs"},
        "device": {"id": PLUG, "name": "Reading Lamp"},
        "labels": [],
        "device_labels": [{"label_id": "third_floor", "name": "third_floor"}],
    }
    assert Client().get.entity("sun.sun")["registry"] == {"registered": False}


def test_list_areas_and_labels_with_counts(home):
    ha = Client()
    areas = {a["area_id"]: a for a in ha.get.areas()["areas"]}
    assert areas["guest_bedroom"] == {
        "area_id": "guest_bedroom", "name": "Guest bedroom",
        "floor": "Upstairs", "device_count": 1, "entity_count": 3,
    }
    assert areas["living_room"]["entity_count"] == 0
    labels = {x["label_id"]: x for x in ha.get.labels()["labels"]}
    assert labels["third_floor"] == {
        "label_id": "third_floor", "name": "third_floor", "device_count": 1, "entity_count": 0
    }
    assert labels["outdoor"]["entity_count"] == 1


def test_get_device_by_name_id_or_one_of_its_entities(home):
    ha = Client()
    by_entity = ha.get.device("switch.reading_lamp")
    assert ha.get.device("reading lamp")["device"] == by_entity["device"]
    assert ha.get.device(PLUG)["device"] == by_entity["device"]
    device = by_entity["device"]
    assert device["name"] == "Reading Lamp" and device["area"]["name"] == "Guest bedroom"
    assert device["labels"] == [{"label_id": "third_floor", "name": "third_floor"}]
    assert ids(by_entity) == [
        "button.reading_lamp_identify", "sensor.reading_lamp_power", "switch.reading_lamp"
    ]
    assert by_entity["entities"][0]["name_override"] == "Reading Lamp Identify"


def test_ambiguous_device_name_lists_candidates(home):
    home.devices["dev-2"] = {"id": "dev-2", "name": "Reading Lamp", "name_by_user": None,
                             "area_id": None, "labels": []}
    with pytest.raises(StudioHaysError, match=r"ambiguous device .Reading Lamp. — matches: dev-2 \(Reading Lamp\), dev-plug \(Reading Lamp\)"):
        Client().get.device("Reading Lamp")


# M2.1: registry writes (STU-10) --------------------------------------------------------

def updates(home: FakeHomeAssistant) -> list[tuple[str, dict]]:
    return [(k, p) for k, p in home.commands if not k.endswith("/list")]


def test_update_entity_renames_moves_and_labels_with_before_after(home):
    ha = Client()
    result = ha.update.entity(
        "sensor.reading_lamp_power", name="Plug Power", area="kitchen", add_labels=["Outdoor"]
    )
    assert result["dry_run"] is False
    assert result["changes"] == {
        "name": {"was": None, "is": "Plug Power"},
        "area_id": {"was": None, "is": "kitchen"},
        "labels": {"was": [], "is": ["outdoor"]},
    }
    assert result["before"]["name"] is None and result["after"]["name"] == "Plug Power"
    assert updates(home) == [(
        "config/entity_registry/update",
        {"entity_id": "sensor.reading_lamp_power", "name": "Plug Power",
         "area_id": "kitchen", "labels": ["outdoor"]},
    )]
    reg = ha.get.entity("sensor.reading_lamp_power")["registry"]
    assert reg["area"]["name"] == "Kitchen" and reg["area_source"] == "entity"
    assert reg["labels"] == [{"label_id": "outdoor", "name": "Outdoor"}]
    (op,) = [o for o in ha.state.history()["ops"] if o["op"] == "update"]
    assert op["ok"] and op["resource_type"] == "registry_entry"


def test_dry_run_plans_without_touching_ha(home):
    ha = Client()
    result = ha.update.entity("sensor.outside_temp", remove_labels=["outdoor"], dry_run=True)
    assert result["dry_run"] is True
    assert result["changes"] == {"labels": {"was": ["outdoor"], "is": []}}
    assert result["after"]["labels"] == []
    assert updates(home) == []
    assert home.entries["sensor.outside_temp"]["labels"] == ["outdoor"]


def test_empty_string_clears_area_and_name_override(home):
    ha = Client()
    ha.update.entity("sensor.outside_temp", area="")
    ha.update.entity("button.reading_lamp_identify", name="")
    assert home.entries["sensor.outside_temp"]["area_id"] is None
    assert home.entries["button.reading_lamp_identify"]["name"] is None
    assert ha.get.entity("sensor.outside_temp")["registry"]["area"] is None


def test_change_entity_id(home):
    ha = Client()
    ha.update.entity("switch.reading_lamp", new_entity_id="switch.aquarium_light")
    assert ha.get.entity("switch.aquarium_light")["registry"]["device"]["id"] == PLUG
    with pytest.raises(NotFoundError):
        ha.get.entity("switch.reading_lamp")


def test_change_entity_id_must_keep_the_domain(home):
    with pytest.raises(StudioHaysError, match="must keep the domain 'switch'"):
        Client().update.entity("switch.reading_lamp", new_entity_id="light.aquarium")
    assert updates(home) == []


def test_unregistered_entity_cannot_be_edited(home):
    with pytest.raises(
        StudioHaysError, match="sun.sun is not in the entity registry — can't be renamed, labelled or moved"
    ):
        Client().update.entity("sun.sun", name="Sol")


def test_unknown_label_is_rejected_before_calling_ha(home):
    with pytest.raises(NotFoundError, match=r"unknown label 'aquarium' — known: first_floor, Outdoor, third_floor"):
        Client().update.entity("switch.reading_lamp", add_labels=["aquarium"])
    assert updates(home) == []


def test_update_device_moves_its_inheriting_entities(home):
    ha = Client()
    result = ha.update.device(
        "Reading Lamp", area="Living Room", add_labels=["first_floor"], remove_labels=["third_floor"]
    )
    assert result["changes"] == {
        "area_id": {"was": "guest_bedroom", "is": "living_room"},
        "labels": {"was": ["third_floor"], "is": ["first_floor"]},
    }
    assert updates(home) == [(
        "config/device_registry/update",
        {"device_id": PLUG, "area_id": "living_room", "labels": ["first_floor"]},
    )]
    assert ids(ha.get.entities(area="living room")) == [
        "button.reading_lamp_identify", "sensor.reading_lamp_power", "switch.reading_lamp"
    ]


def test_create_label_and_area(home):
    ha = Client()
    assert ha.create.label("Aquarium")["after"]["label_id"] == "aquarium"
    assert ha.create.area("Attic")["after"]["area_id"] == "attic"
    assert "Aquarium" in [x["name"] for x in ha.get.labels()["labels"]]
    assert "Attic" in [a["name"] for a in ha.get.areas()["areas"]]
    with pytest.raises(StudioHaysError, match="name 'aquarium' is already used by label 'Aquarium'"):
        ha.create.label("aquarium")


def test_failed_registry_write_is_audited_and_state_untouched(home):
    ha = Client()
    before = ha.get.entity("switch.reading_lamp")["registry"]
    home.fail_next["config/entity_registry/update"] = ("home_assistant_error", "nope")
    with pytest.raises(RemoteError, match="config/entity_registry/update failed: home_assistant_error: nope"):
        ha.update.entity("switch.reading_lamp", name="Aquarium Light")
    assert ha.get.entity("switch.reading_lamp")["registry"] == before
    assert [o["ok"] for o in ha.state.history()["ops"] if o["op"] == "update"] == [False]


def test_cli_update_device_dry_run_with_comma_separated_labels(home, capsys):
    code, out = run_cli(
        capsys, "ha", "update", "device", "Reading Lamp", "--area", "Living Room",
        "--add-labels", "first_floor", "--remove-labels", "third_floor", "--dry-run",
    )
    assert code == 0 and out["dry_run"] is True
    assert out["changes"]["labels"] == {"was": ["third_floor"], "is": ["first_floor"]}
    assert updates(home) == []


def test_mcp_update_entity_takes_label_arrays(home):
    async def call(session):
        return await session.call_tool(
            "ha_update_entity",
            {"entity_id": "switch.reading_lamp", "add_labels": ["Outdoor"], "dry_run": True},
        )

    out = tool_json(mcp_session(call))
    assert out["changes"] == {"labels": {"was": [], "is": ["outdoor"]}}


# M2.1: repurpose_device (STU-11) --------------------------------------------------------

REPURPOSE: dict[str, Any] = dict(
    area="Living Room", name="Aquarium Light",
    add_labels=["first_floor"], remove_labels=["third_floor"],
)


def test_repurpose_dry_run_plans_every_change_and_lists_references(home):
    home.references["switch.reading_lamp"] = {"automation": ["automation.lamp_at_dusk"], "script": []}
    ha = Client()
    result = ha.update.repurpose_device("switch.reading_lamp", rename_entity_ids=True, dry_run=True, **REPURPOSE)

    assert result["dry_run"] is True and result["device_id"] == PLUG
    plan = [(s["action"], s["target"], s["changes"]) for s in result["plan"]]
    assert plan == [
        ("update_device", PLUG, {
            "name_by_user": {"was": "Reading Lamp", "is": "Aquarium Light"},
            "area_id": {"was": "guest_bedroom", "is": "living_room"},
            "labels": {"was": ["third_floor"], "is": ["first_floor"]},
        }),
        ("update_entity", "button.reading_lamp_identify", {
            "name": {"was": "Reading Lamp Identify", "is": "Aquarium Light Identify"},
            "new_entity_id": {"was": "button.reading_lamp_identify", "is": "button.aquarium_light_identify"},
        }),
        ("update_entity", "sensor.reading_lamp_power", {
            "new_entity_id": {"was": "sensor.reading_lamp_power", "is": "sensor.aquarium_light_power"},
        }),
        ("update_entity", "switch.reading_lamp", {
            "new_entity_id": {"was": "switch.reading_lamp", "is": "switch.aquarium_light"},
        }),
    ]
    assert result["references"] == {"switch.reading_lamp": {"automation": ["automation.lamp_at_dusk"]}}
    assert updates(home) == [
        ("search/related", {"item_type": "entity", "item_id": eid})
        for eid in ("button.reading_lamp_identify", "sensor.reading_lamp_power", "switch.reading_lamp")
    ]


def test_repurpose_applies_the_plan(home):
    ha = Client()
    result = ha.update.repurpose_device("Reading Lamp", **REPURPOSE)
    assert result["ok"] is True and result["not_applied"] == []
    assert len(result["applied"]) == 2  # device + the one name override; entity_ids kept

    after = ha.get.device("Aquarium Light")
    assert after["device"]["area"]["name"] == "Living Room"
    assert after["device"]["labels"] == [{"label_id": "first_floor", "name": "first_floor"}]
    assert ids(after) == ["button.reading_lamp_identify", "sensor.reading_lamp_power", "switch.reading_lamp"]
    assert after["entities"][0]["name_override"] == "Aquarium Light Identify"
    assert ha.get.entity("switch.reading_lamp")["entity"]["attributes"]["friendly_name"] == "Aquarium Light"


def test_repurpose_stops_at_the_first_failure_and_reports(home):
    home.fail_next["config/entity_registry/update"] = ("home_assistant_error", "busy")
    ha = Client()
    result = ha.update.repurpose_device(PLUG, rename_entity_ids=True, **REPURPOSE)
    assert result["ok"] is False
    assert "config/entity_registry/update failed: home_assistant_error: busy" in result["error"]
    assert [s["target"] for s in result["applied"]] == [PLUG]
    assert [s["target"] for s in result["not_applied"]] == [
        "button.reading_lamp_identify", "sensor.reading_lamp_power", "switch.reading_lamp"
    ]
    assert home.devices[PLUG]["name_by_user"] == "Aquarium Light"
    assert "switch.reading_lamp" in home.entries


def test_repurpose_entity_without_device_is_an_error(home):
    with pytest.raises(NotFoundError, match="entity sensor.outside_temp has no device"):
        Client().update.repurpose_device("sensor.outside_temp", name="x", dry_run=True)


# review fixes (M2.1) ---------------------------------------------------------------------

def test_get_entity_shows_labels_inherited_from_its_device(home):
    reg = Client().get.entity("switch.reading_lamp")["registry"]
    assert reg["labels"] == []
    assert reg["device_labels"] == [{"label_id": "third_floor", "name": "third_floor"}]


def test_registry_write_invalidates_state_for_other_processes(home):
    Client().update.entity("switch.reading_lamp", new_entity_id="switch.aquarium_light")
    Client.reset()  # a fresh process: no in-memory dirty flag
    assert Client().get.entity("switch.aquarium_light")["entity"]["state"] == "off"


def test_repurpose_rejects_an_empty_name(home):
    with pytest.raises(StudioHaysError, match="repurpose_device needs a new name"):
        Client().update.repurpose_device(PLUG, name="", dry_run=True)


def test_repurpose_entity_id_rewrite_is_token_bounded_and_name_is_literal(home):
    home.entries["sensor.reading_lamp_power"]["name"] = "Reading Lamp Power"
    home.entries["sensor.reading_lampshade"] = {
        **home.entries["sensor.reading_lamp_power"],
        "id": "reg-shade", "entity_id": "sensor.reading_lampshade", "name": None,
    }
    plan = Client().update.repurpose_device(
        PLUG, name=r"Tank \1 Light", rename_entity_ids=True, dry_run=True
    )["plan"]
    by_target = {s["target"]: s["changes"] for s in plan}
    assert by_target["sensor.reading_lamp_power"]["name"]["is"] == r"Tank \1 Light Power"
    assert "sensor.reading_lampshade" not in by_target  # "reading_lamp" isn't a whole token there


def test_ambiguous_names_show_ids_and_names(home):
    home.devices["dev-2"] = {"id": "dev-2", "name": "Reading Lamp", "name_by_user": None,
                             "area_id": None, "labels": []}
    with pytest.raises(StudioHaysError, match=r"dev-2 \(Reading Lamp\), dev-plug \(Reading Lamp\)"):
        Client().get.device("reading lamp")


# STU-15: HA 2026.9 drift ---------------------------------------------------------------

def test_service_errors_carry_home_assistants_reason(fake):
    with pytest.raises(RemoteError, match=r"HTTP 400 for POST /api/services/homeassistant/update_area: "
                                          r"Service homeassistant.update_area not found"):
        Client().call.service("homeassistant", "update_area", area_id="hallwat", name="Hallway")


def test_response_only_service_needs_return_response(fake):
    ha = Client()
    with pytest.raises(RemoteError, match="requires responses but caller did not ask for responses"):
        ha.call.service("weather", "get_forecasts", entity_id="weather.home", type="daily")
    result = ha.call.service(
        "weather", "get_forecasts", return_response=True, entity_id="weather.home", type="daily"
    )
    assert result == {
        "service": "weather.get_forecasts",
        "changed_entities": [],
        "response": {"weather.home": {"forecast": [{"condition": "sunny"}]}},
    }
    assert fake.calls[-1] == (
        "POST", "/api/services/weather/get_forecasts", {"entity_id": "weather.home", "type": "daily"}
    )


def test_update_area_renames_and_assigns_a_floor(home):
    ha = Client()
    result = ha.update.area("Guest bedroom", name="Guest Room", floor="main")
    assert result["changes"] == {
        "name": {"was": "Guest bedroom", "is": "Guest Room"},
        "floor_id": {"was": "upstairs", "is": "main"},
    }
    assert updates(home) == [(
        "config/area_registry/update",
        {"area_id": "guest_bedroom", "name": "Guest Room", "floor_id": "main"},
    )]
    areas = {a["area_id"]: a for a in ha.get.areas()["areas"]}
    assert areas["guest_bedroom"]["name"] == "Guest Room" and areas["guest_bedroom"]["floor"] == "Main"


def test_update_area_dry_run_and_clearing_the_floor(home):
    ha = Client()
    result = ha.update.area("kitchen", floor="", dry_run=True)
    assert result["changes"] == {"floor_id": {"was": "main", "is": None}}
    assert updates(home) == []
    ha.update.area("kitchen", floor="")
    assert home.areas["kitchen"]["floor_id"] is None


@pytest.mark.parametrize("taken", ["Living Room", "living room", "LivingRoom"])
def test_update_area_rejects_a_name_another_area_normalizes_to(home, taken):
    with pytest.raises(StudioHaysError, match="already used by area 'Living Room'"):
        Client().update.area("kitchen", name=taken)
    assert updates(home) == []


def test_update_area_unknown_floor_lists_known_floors(home):
    with pytest.raises(NotFoundError, match=r"unknown floor 'Attic' — known: Main, Upstairs"):
        Client().update.area("kitchen", floor="Attic")


def test_create_area_uses_the_same_normalized_uniqueness(home):
    with pytest.raises(StudioHaysError, match="already used by area 'Living Room'"):
        Client().create.area("livingroom")


def test_cli_update_area_dry_run(home, capsys):
    code, out = run_cli(capsys, "ha", "update", "area", "Guest bedroom", "--floor", "Main", "--dry-run")
    assert code == 0 and out["changes"] == {"floor_id": {"was": "upstairs", "is": "main"}}


def test_update_area_rejects_blank_names_naming_the_area(home):
    with pytest.raises(StudioHaysError, match="area kitchen: the name can't be blank"):
        Client().update.area("kitchen", name="   ")


def test_normalization_matches_home_assistant(home):
    ha = Client()
    # HA 2026.9 only casefolds and drops spaces: punctuation keeps names distinct
    assert ha.update.area("kitchen", name="Living-Room", dry_run=True)["changes"]
    with pytest.raises(StudioHaysError, match="pick a distinct name"):
        ha.update.area("kitchen", name="LIVING  room")


def test_cli_call_with_return_response(fake, capsys):
    code, out = run_cli(
        capsys, "ha", "call", "weather", "get_forecasts",
        "--data", '{"entity_id": "weather.home"}', "--return-response",
    )
    assert code == 0 and out["response"] == {"weather.home": {"forecast": [{"condition": "sunny"}]}}


def test_return_response_inside_service_data_is_a_readable_error(fake, capsys):
    code, out = run_cli(capsys, "ha", "call", "weather", "get_forecasts",
                        "--data", '{"return_response": true}')
    assert code == 1 and "return_response is a call option, not service data" in out["error"]
