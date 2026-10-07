# 03 — API contract

The Client object is the whole API. This doc fixes its shape so that every
module feels identical and surfaces (CLI/MCP) can be generated from it.

## The Client object

```python
from studio_hays.home_assistant import Client as studiohays_ha

studiohays_ha = studiohays_ha(**kwargs)   # kwargs optional → env vars → defaults
studiohays_ha.get.entity("light.kitchen")
```

- construction is cheap and side-effect free (doc 01, lazy guarantee)
- repeated `Client()` with the same config is the same object (singleton)
- `connect()`, `close()`, `with`-statement, `state.*` are on every client
- everything is **sync** (D-001, rationale at the end of this doc)

## The six standard namespaces

| Namespace | Network? | Purpose |
| --- | --- | --- |
| `client.get` | no (state only) | fetch one or many resources from the local state |
| `client.query` | no | free-form search/filter against the local state |
| `client.create` | yes | create a resource on the remote (write-through) |
| `client.update` | yes | modify a resource on the remote (write-through) |
| `client.delete` | yes | remove a resource on the remote (write-through) |
| `client.state` | sometimes | refresh / drift / plan / apply / show / reset / history |

Modules may add **documented extra namespaces** (HA has `client.call` for
fire-and-forget service calls like `light.turn_on`; PVE will have `client.call`
for start/stop/restart — status is volatile, not CRUD). Extras must be
registered in `OPERATIONS` like anything else.

### Naming conventions

- resources are named by the module's vocabulary: `entity`, `vm`, `node`, …
- singular for one, plural for many: `get.entity(id)`, `get.entities(...)`
- `create` / `update` / `delete` take the singular: `create.vm(...)`,
  `delete.vm(id)`
- `state` verbs are verbs: `state.refresh()`, `state.drift()`
- parameter names are snake_case and boring: `domain`, `search`, `limit`,
  `fresh`

### Standard parameters

| Param | Where | Meaning |
| --- | --- | --- |
| `fresh: bool = False` | `get.*` | refresh from remote first, then read |
| `limit: int = 100` | `get.*` (plural), `query` | cap rows |
| `search: str` | `query` | case-insensitive substring match on id + human name |
| module-specific filters | `get.*` (plural) | e.g. `domain="light"` for HA, `node="pve1"` for PVE |

## Return values (D-003)

**Plain `dict`s, JSON-serializable, always.** Rationale:

- the same value can be printed by the CLI, serialized by MCP, and
  `json.dumps`-ed by a script with zero conversion
- `dict` keeps v0 dependency-free (no pydantic/dataclasses in the API path);
  validation is an open question (Q-008)

Conventions:

- singular `get` returns the resource dict, or raises `NotFoundError`
- plural `get` / `query` return a list of compact row dicts
- every state-backed read is wrapped in an envelope:

```json
{"last_synced": "2026-09-13T15:04:12+00:00", "stale": false, "entities": [...]}
```

  `stale` is true when `now - last_synced > 2 × refresh expectation` (modules
  declare a staleness window; HA: 15 s, since typical refresh is ~5 s).
- write ops return the resource dict as the remote reported it; registry
  edits (HA `update.*` / `create.*`) wrap it as `{dry_run, changes, before,
  after}` so a `dry_run` and a real write have the same shape (M2.1).
- `state.drift()` returns the drift report (doc 02) verbatim.

## Worked example: `home_assistant`

```python
ha = Client()

ha.get.domains()                        # {"last_synced":…,"stale":false,
                                        #  "domains":[{"domain":"sensor","count":73},…]}
ha.get.entities(domain="light", limit=50)
ha.get.entity("light.kitchen")          # full state + attributes, from state
ha.get.entity("light.kitchen", fresh=True)

ha.query("kitchen", domain="light")     # local search, no network

ha.get.entities(area="Kitchen", label="outdoor")
ha.get.areas(); ha.get.labels()
ha.get.device("switch.reading_lamp")    # by name, id, or one of its entities

ha.update.entity("sensor.plug_power", name="Plug Power", area="", dry_run=True)
ha.update.device("Reading Lamp", area="Living Room", add_labels=["first_floor"])
ha.update.repurpose_device("Reading Lamp", area="Living Room", name="Aquarium Light",
                           add_labels=["first_floor"], remove_labels=["third_floor"])
ha.create.label("Aquarium")
ha.create.floor("Third Floor", level=2)
ha.update.floor("Main", name="First Floor")
ha.update.area("hallwat", name="Main Hallway", floor="Main")
ha.delete.area("Old Room", dry_run=True)      # impact; force=True if still in use

ha.get.automations()                     # from automation.* entities
ha.get.automation("hallway_nightlight_wakeup")
ha.update.automation({"alias": "…", "triggers": […], "actions": […]}, dry_run=True)
ha.delete.automation("hallway_nightlight_wakeup")

ha.get.scripts()                         # from script.* entities
ha.get.script("routine_bedtime")
ha.update.script({"alias": "…", "sequence": […]}, dry_run=True)
ha.delete.script("routine_bedtime")

ha.call.service("light", "turn_on", entity_id="light.kitchen")
ha.call.service("switch", "toggle", entity_id="switch.garage")
ha.call.set_state("binary_sensor.occupancy", "on", dry_run=True)  # simulate; does not command

ha.state.refresh()
ha.state.drift()                        # added/removed/changed report
ha.state.show(resource_type="entity", id="light.kitchen")
ha.state.reset()
ha.state.history(10)
```

HA is **mirror mode** in v0. `update` / `create` edit HA's registries (names,
areas, floors, labels) over the WebSocket API and write the result through to
the mirror (M2.1, STU-6/15/13); `delete.area` removes an area (STU-18).

**Automations and scripts are the exception to "`get` is state only"** (STU-16):
HA 2026.9 keeps their config only behind its REST config view, so
`get.automation(id)` / `get.script(id)` read it live (the envelope's
`last_synced`/`stale` describe the entity mirror, not the config).
`update.automation(config)` / `update.script(config)` are upserts — HA's
config API has no separate create — and report `action: create|replace`;
CLI verbs are `save` (`ha save automation` / `ha save script`) and MCP tools
`save_automation` / `save_script`. Configs aren't mirrored, only audited
(`create` / `update` / `delete` ops rows).

## Worked example: `proxmox`

```python
pve = Client()

pve.get.nodes()
pve.get.node("pve1")
pve.get.vms(node="pve1")
pve.get.vm(101)

pve.create.vm(
    name="test-vm", node="pve1",
    cores=2, memory=4096,               # MiB
    os="cloud-init", disk_gb=32,
)                                       # → managed row with desired attrs

pve.update.vm(101, memory=8192)
pve.delete.vm(101)

pve.call.vm_start(101)                  # volatile action, not CRUD
pve.call.vm_stop(101)

pve.state.refresh()
pve.state.drift()                       # identity fields: name, cores, memory, …
pve.state.plan()                        # desired (managed) vs live
pve.state.apply()                       # push planned changes
```

PVE is **mirror + managed**: every `create.vm` records a `managed` row, so
`plan`/`apply` work. VMs created in the web UI are visible in `snapshot`
after refresh but not in `managed` (they can be "adopted" later — v1).

## Errors

Hierarchy (doc 01): `StudioHaysError` → `ConfigError`, `RemoteError`,
`NotFoundError`, `DriftError`, `StateError`.

Contract:

- errors carry context: the URL/id, the env var name, the HTTP status
- `get` on a resource never seen before → `NotFoundError("entity not found:
  light.kitchen — run state.refresh() if it exists on the remote")`
- `create`/`update`/`delete` on an unreachable remote → `RemoteError`;
  state is untouched (write-through, remote-first)
- strict ops may raise `DriftError` (e.g. `apply(strict=True)` when plan is
  non-empty and unexpected); v0 defaults to non-strict

## Sync vs async (D-001)

**Decision (proposed): the core and all module clients are sync** —
`httpx.Client` + stdlib `sqlite3`.

Thought experiment, three candidates:

| Option | Pros | Cons |
| --- | --- | --- |
| **A. sync core** ✅ | the pitch example (`c.get.entity(…)`) is just Python; REPL/script/notebook friendly; `sqlite3` is stdlib; MCP's FastMCP runs sync tool functions in a worker thread, so no async needed for MCP | an async-only host app must hop to a thread (rare in this codebase) |
| B. async core | matches the old home-assistant-mcp code (httpx.AsyncClient/aiosqlite) | every script needs `asyncio.run`; `sqlite3` async means aiosqlite dep; REPL pain |
| C. async core + sync facade | best of both | two code paths to maintain; the facade is where bugs live; v0 doesn't need it |

The existing HA code is async, but the port to sync is mechanical
(`AsyncClient` → `Client`, drop `await`s, aiosqlite → sqlite3). Pick A;
revisit only if a real async-only host appears.

## The operations registry (D-006)

Every public op is declared once, in `studio_hays/<module>/operations.py`:

```python
@dataclass(frozen=True)
class Operation:
    name: str          # "get_entity"
    verb: str          # "get" | "query" | "create" | "update" | "delete" | "state" | "call"
    resource: str      # "entity" | "vm" | "" (for state verbs)
    doc: str           # one line; becomes CLI help AND MCP tool description
    params: dict[str, Param]   # name → type/required/default/description
    handler: Callable  # (client, kwargs) → JSON-serializable
```

Consumers:

- **CLI** — `studio-hays ha get entity …` resolves `(verb, resource, name)`
  in the registry → argparse generated from `params` (doc 04).
- **MCP** — each operation becomes a tool `ha_get_entity` with `doc` as the
  description and `params` as the input schema (doc 04).
- **contract tests** — assert every namespaced method on `Client` has a
  matching `Operation` (parity: if you can call it in Python, you can call
  it from CLI and MCP).

v0 builds handlers explicitly (thin wrappers over the namespaced methods);
full code-generation from the registry is a v1 idea. The registry is still
the single source of truth for *what exists and what it means*.
