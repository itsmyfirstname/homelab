"""home_assistant module, tested at its public seams (M2).

HTTP is faked at httpx's transport (``Client._transport``), so the module's
real request building and error mapping run against a fake Home Assistant.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from studio_hays.core import ConfigError, NotFoundError, RemoteError
from studio_hays.home_assistant import Client

URL = "http://ha.test:8123"
TOKEN = "test-token"


class FakeHA:
    """Just enough of the HA REST API: /api/states, /api/services."""

    def __init__(self, states: list[dict] | None = None) -> None:
        self.states: dict[str, dict] = {s["entity_id"]: s for s in states or []}
        self.calls: list[tuple[str, str, object]] = []
        self.down = False
        self.status_override: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.status_override is not None:
            return httpx.Response(self.status_override)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401)
        path = request.url.path
        if request.method == "GET" and path == "/api/states":
            return httpx.Response(200, json=list(self.states.values()))
        if request.method == "GET" and path.startswith("/api/states/"):
            state = self.states.get(path.removeprefix("/api/states/"))
            return httpx.Response(200, json=state) if state else httpx.Response(404)
        if request.method == "POST" and path.startswith("/api/services/"):
            domain, _, service = path.removeprefix("/api/services/").partition("/")
            if domain != "light" or service not in ("turn_on", "turn_off"):
                return httpx.Response(400)
            ids = (body or {}).get("entity_id")
            changed = []
            for eid in [ids] if isinstance(ids, str) else ids or []:
                if eid in self.states:
                    self.states[eid]["state"] = "on" if service == "turn_on" else "off"
                    changed.append(self.states[eid])
            return httpx.Response(200, json=changed)
        return httpx.Response(404)


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
    Client._transport = None
    yield
    Client._transport = None
    Client.reset()


@pytest.fixture
def fake(monkeypatch) -> FakeHA:
    """A configured client environment talking to a seeded fake HA."""
    monkeypatch.setenv("STUDIOHAYS_HA_URL", URL)
    monkeypatch.setenv("STUDIOHAYS_HA_TOKEN", TOKEN)
    ha = FakeHA([dict(e, attributes=dict(e["attributes"])) for e in HOUSE])
    Client._transport = httpx.MockTransport(ha.handler)
    return ha


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
    assert code == 0 and out["count"] == 4
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


def tool_json(result) -> object:
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

def test_host_project_can_pass_url_token_and_db_as_kwargs(tmp_path, monkeypatch):
    ha = FakeHA([dict(e, attributes=dict(e["attributes"])) for e in HOUSE])
    Client._transport = httpx.MockTransport(ha.handler)
    db = tmp_path / "host" / "cache.db"
    c = Client(url=URL, token=TOKEN, db=str(db))
    assert c.config.db_path == db
    assert c.get.entity("light.kitchen")["entity"]["state"] == "off"
    assert db.exists()

