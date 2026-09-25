"""The MCP tool surface opencode relies on, now backed by studio-hays.

HTTP is faked at studio-hays' transport hook, so the real client, cache and
tool wiring all run.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp import Client as MCPClient
from studio_hays.home_assistant import Client

from home_assistant_mcp.server import mcp

URL = "http://ha.test:8123"
TOKEN = "test-token"


def _state(entity_id: str, state: str, name: str) -> dict:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {"friendly_name": name},
        "last_changed": "2026-09-24T00:00:00+00:00",
        "last_updated": "2026-09-24T00:00:00+00:00",
    }


class FakeHA:
    def __init__(self) -> None:
        self.states = {
            s["entity_id"]: s
            for s in (
                _state("light.kitchen", "off", "Kitchen Light"),
                _state("light.bedroom", "on", "Bedroom Light"),
                _state("sensor.hall_temp", "21.5", "Hallway Temperature"),
            )
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401)
        path = request.url.path
        if request.method == "GET" and path == "/api/states":
            return httpx.Response(200, json=list(self.states.values()))
        if request.method == "POST" and path == "/api/services/light/turn_on":
            eid = json.loads(request.content)["entity_id"]
            self.states[eid]["state"] = "on"
            return httpx.Response(200, json=[self.states[eid]])
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HA_URL", URL)
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setenv("STUDIOHAYS_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("STUDIOHAYS_DATA_DIR", str(tmp_path / "data"))
    Client.reset()
    Client._transport = httpx.MockTransport(FakeHA().handler)
    yield
    Client._transport = None
    Client.reset()


def call(name: str, args: dict | None = None):
    async def main():
        async with MCPClient(mcp) as session:
            return await session.call_tool(name, args or {})

    return asyncio.run(main())


def payload(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_tool_surface_is_unchanged():
    async def main():
        async with MCPClient(mcp) as session:
            return {t.name for t in (await session.list_tools()).tools}

    assert asyncio.run(main()) == {"list_entities", "get_entity", "list_domains", "call_service"}


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
    assert result.is_error and "entity not found: light.nope" in result.content[0].text


def test_missing_env_is_a_readable_tool_error(monkeypatch):
    monkeypatch.delenv("HA_URL")
    result = call("list_entities")
    assert result.is_error and "HA_URL and HA_TOKEN must be set" in result.content[0].text
