"""Contract suite (doc 01, `core.contract_suite`) against the fixture client.

These same checks run against every future module (home_assistant in M2,
proxmox in M3): the fixture is the stand-in that proves the core kernel
and the module shape hold together.
"""

from __future__ import annotations

import json

import pytest

import studio_hays._fixture as fixture_module
import studio_hays.home_assistant as ha_module
from studio_hays._fixture import OPERATIONS, Client
from studio_hays._fixture.client import FixtureRemote
from studio_hays.core import NotFoundError, RemoteError

NAMESPACES = ("get", "query", "create", "update", "delete", "call", "state")

#: every module the registry-level contract runs against
MODULE_CASES = pytest.mark.parametrize(
    "module", [fixture_module, ha_module], ids=["fixture", "home_assistant"]
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh singleton + isolated data dir + controlled env, per test."""
    monkeypatch.setenv("STUDIOHAYS_DATA_DIR", str(tmp_path / "data"))
    # keep an ambient ./.env from leaking into resolution
    monkeypatch.setenv("STUDIOHAYS_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.delenv("STUDIOHAYS_FIX_URL", raising=False)
    monkeypatch.delenv("STUDIOHAYS_FIX_TOKEN", raising=False)
    Client.reset()
    Client._fake = None
    yield
    Client._fake = None
    Client.reset()


# 1. lazy construction ---------------------------------------------------

def test_lazy_construction():
    c = Client()
    assert not c.config.db_path.exists()  # construction opened nothing
    env = c.get.widgets()                 # first use pays the cost
    assert c.config.db_path.exists()
    assert env["widgets"] == []
    assert env["stale"] is True           # never synced yet


# 2. singleton identity ----------------------------------------------------

def test_singleton_identity():
    assert Client() is Client()
    assert Client(token="other-token") is not Client()


# 3. env config -------------------------------------------------------------

def test_env_config(monkeypatch):
    monkeypatch.setenv("STUDIOHAYS_FIX_TOKEN", "env-token")
    monkeypatch.setenv("STUDIOHAYS_FIX_URL", "http://env.example")
    c = Client()
    assert c.config.get("token") == "env-token"
    assert c.config.get("url") == "http://env.example"
    assert c.config.db_path.name == "fixture.db"


# 4. kwargs override env ------------------------------------------------------

def test_kwargs_override_env(monkeypatch):
    monkeypatch.setenv("STUDIOHAYS_FIX_TOKEN", "env-token")
    assert Client(token="explicit").config.get("token") == "explicit"


# 5. CRUD write-through ---------------------------------------------------------

def test_crud_write_through():
    fake = FixtureRemote(seed=[{"id": "w1", "name": "seeded"}])
    Client._fake = fake
    c = Client()

    created = c.create.widget("alpha")
    assert created["id"] == "w2"
    assert fake.store["w2"] == created  # remote and state agree

    got = c.get.widget(created["id"])
    assert got["widget"]["name"] == "alpha"
    assert got["stale"] is False

    updated = c.update.widget(created["id"], name="beta")
    assert updated["name"] == "beta"
    assert fake.store["w2"]["name"] == "beta"

    deleted = c.delete.widget(created["id"])
    assert deleted["id"] == "w2"
    assert "w2" not in fake.store

    with pytest.raises(NotFoundError):
        c.get.widget("w2")

    assert c.call.ping() == {"pong": True}


def test_failed_write_is_audited_and_leaves_state_untouched():
    fake = FixtureRemote(seed=[{"id": "w1", "name": "one"}])
    Client._fake = fake
    c = Client()
    c.state.refresh()
    before = c.state.show()

    fake.fail_next = LookupError("boom")
    with pytest.raises(RemoteError):
        c.update.widget("w1", name="nope")
    fake.fail_next = LookupError("boom")
    with pytest.raises(RemoteError):
        c.call.ping()

    assert c.state.show()["widgets"] == before["widgets"]
    failed = [op for op in c.state.history()["ops"] if not op["ok"]]
    assert [(op["op"], op["id"]) for op in failed] == [("call", None), ("update", "w1")]


def test_update_merges_into_managed_desired():
    Client._fake = FixtureRemote()
    c = Client()
    created = c.create.widget("alpha")
    c.update.widget(created["id"], name="beta", color="red")
    (managed,) = c.state.show()["managed"]
    assert managed["desired"] == {"name": "beta", "color": "red"}
    ops = [op["op"] for op in c.state.history()["ops"]]
    assert ops[:2] == ["update", "create"]


def test_update_resets_managed_status():
    Client._fake = FixtureRemote()
    c = Client()
    created = c.create.widget("alpha")
    c.store.set_managed_status("widget", created["id"], "drifted")
    c.update.widget(created["id"], name="beta")
    assert c.state.show()["managed"][0]["status"] == "ok"


def test_record_write_is_atomic():
    Client._fake = FixtureRemote()
    c = Client()
    with pytest.raises(TypeError):
        # unserializable result fails after the snapshot row is written
        c.store.record_write(
            "create", "widget", "w1",
            obj={"id": "w1"}, desired={"name": "x"}, result=object(),
        )
    show = c.state.show()
    assert show["widgets"] == [] and show["managed"] == []
    assert c.state.history()["ops"] == []


# 6. refresh rebuilds ----------------------------------------------------------

def test_refresh_rebuilds():
    Client._fake = FixtureRemote(seed=[{"id": "w1", "name": "one"}])
    c = Client()
    res = c.state.refresh()
    assert res["count"] == 1
    env = c.get.widgets()
    assert env["stale"] is False
    assert [w["name"] for w in env["widgets"]] == ["one"]
    # a fresh state store sees the same data (self-healing mirror)
    Client._fake = FixtureRemote(seed=[{"id": "w1", "name": "one"}])
    Client.reset()
    c2 = Client()
    c2.state.reset()  # throw away the old DB
    assert [w["name"] for w in c2.get.widgets()["widgets"]] == []
    c2.state.refresh()
    assert [w["name"] for w in c2.get.widgets()["widgets"]] == ["one"]


def test_refresh_and_managed_layer():
    """Full replace must survive the managed layer (FK) and prune orphans."""
    Client._fake = FixtureRemote(seed=[{"id": "w1", "name": "one"}])
    c = Client()
    c.state.refresh()
    created = c.create.widget("alpha")  # adds a managed row for w2
    c.state.refresh()                   # full replace over live managed rows
    show = c.state.show()
    assert {m["id"] for m in show["managed"]} == {created["id"]}
    # a managed row whose object vanishes from the remote gets pruned
    fake = Client._fake
    assert fake is not None
    fake.store["w99"] = {"id": "w99", "name": "ghost"}
    c.state.refresh()
    c.store.upsert_managed("widget", "w99", {"name": "ghost"})
    del fake.store["w99"]
    c.state.refresh()
    assert {m["id"] for m in c.state.show()["managed"]} == {created["id"]}


# 7. drift detection -------------------------------------------------------------

def test_drift_detection():
    fake = FixtureRemote(seed=[{"id": "w1", "name": "one"}])
    Client._fake = fake
    c = Client()
    c.state.refresh()

    # changes made behind the client's back
    fake.store["w1"]["name"] = "renamed"
    fake.store["w3"] = {"id": "w3", "name": "three"}

    report = c.state.drift()
    assert report["removed"] == []
    assert {d["id"] for d in report["added"]} == {"w3"}
    changed = {d["id"]: d for d in report["changed"]}
    assert set(changed) == {"w1"}
    assert changed["w1"]["diff"]["name"] == {"was": "one", "is": "renamed"}
    assert report["count"] == 2

    # after the drift run reconciles the snapshot, we are clean
    assert c.state.drift()["count"] == 0


# 8. registry parity ---------------------------------------------------------------

@MODULE_CASES
def test_registry_parity(module):
    c = module.Client()
    names = {op.name for op in module.OPERATIONS}
    for op in module.OPERATIONS:
        ns_name, sep, method = op.name.partition("_")
        assert sep and ns_name and method, f"bad op name: {op.name}"
        ns = getattr(c, ns_name, None)
        assert ns is not None, f"op {op.name}: no namespace {ns_name!r}"
        assert callable(getattr(ns, method, None)), (
            f"op {op.name}: {ns_name}.{method} is not callable"
        )
    # reverse: every namespace method has an operation
    for ns_name in NAMESPACES:
        ns = getattr(c, ns_name, None)
        for m in dir(ns) if ns is not None else ():
            if m.startswith("_") or not callable(getattr(ns, m)):
                continue
            assert f"{ns_name}_{m}" in names, (
                f"namespace method {ns_name}.{m} has no operation"
            )


# operation schemas (MCP inputSchema source) ----------------------------------------

@MODULE_CASES
def test_operations_schema(module):
    tool_names = [op.tool_name for op in module.OPERATIONS]
    assert len(tool_names) == len(set(tool_names)), "duplicate MCP tool names"
    for op in module.OPERATIONS:
        schema = op.to_schema("x")
        assert schema["name"] == f"x_{op.tool_name}"
        assert schema["description"]
        props = schema["inputSchema"]["properties"]
        required = schema["inputSchema"]["required"]
        assert isinstance(props, dict)
        for p in op.params:
            assert p.name in props, f"{op.name}: param {p.name} missing from schema"
            if p.required:
                assert p.name in required


# CLI smoke ------------------------------------------------------------------------

def test_cli_smoke(capsys):
    from studio_hays import cli

    Client._fake = FixtureRemote(seed=[{"id": "w1", "name": "one"}])

    assert cli.main(["fixture", "call_ping"]) == 0
    assert json.loads(capsys.readouterr().out) == {"pong": True}

    assert cli.main(["fixture", "create_widget", "name=two"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "w2"

    assert cli.main(["fixture", "--list-ops"]) == 0
    assert "create_widget" in capsys.readouterr().out
