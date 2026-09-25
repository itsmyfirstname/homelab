# 00 — Vision

## Elevator pitch

`studio-hays` is a library of independent Python modules, one per slice of my
homelab. `studio_hays.home_assistant` is a client for Home Assistant;
`studio_hays.proxmox` is a client for Proxmox; and so on. Each module is a
self-sustaining, portable client object that can be:

- **imported** — in a script, a REPL, a notebook, anything that can `import`
- **executed** — from a `studio-hays` CLI
- **exposed** — as an MCP server for LLM clients (opencode, Claude, …)

All three are thin surfaces over the *same* client code.

```python
from studio_hays.home_assistant import Client as studiohays_ha

studiohays_ha = studiohays_ha(**kwargs)   # kwargs optional; defaults come from env vars
studiohays_ha.get.entity("light.kitchen")
```

## The problem

Today homelab automation is a pile of one-offs:

- a dedicated MCP server project just to give an LLM read access to Home
  Assistant,
- curl one-liners and throwaway scripts for Proxmox,
- the same env-var config, HTTP client, error messages, and caching logic
  re-invented in every one of them,
- state living nowhere: nothing tracks "what I last saw / what I created", so
  drift is invisible.

Each one-off is shaped like a *server* (or a script), not a *library*, so they
can't share code. The pitch is to invert that: **library first**, surfaces
second.

## What we build

One Python distribution, `studio-hays`, whose subpackages are per-slice
clients of the homelab:

```
studio_hays/
  home_assistant/   # Home Assistant
  proxmox/          # Proxmox VE
  ...               # one subpackage per infrastructure slice (roadmap: 05)
```

"Each module is self-sustaining" is a **contract**, not a vibe. A module earns
the name only if it satisfies all four guarantees below. The core kernel
(doc 01) enforces most of it mechanically; a contract test suite (doc 03)
checks the rest in CI.

## The four guarantees

### 1. Lazy

`Client()` construction opens nothing: no sockets, no files, no env-var
validation that can raise. The HTTP transport and the SQLite file are created
on the first real operation (or by an explicit `client.connect()`).

Consequences:

- `import studio_hays.home_assistant` is side-effect free.
- A typo'd env var surfaces *at first use* with a good message ("missing
  STUDIOHAYS_HA_URL — set it or pass url=…"), not at import time.
- The CLI can call `connect()` eagerly so users get fast, clear failures.

### 2. CRUD

The client can read from and write to the *real* infrastructure — not just
serve a cache. Reads default to the local state DB (fast, no network); every
write goes to the remote API **and** writes back through the state DB
(write-through). So the DB and the remote can never silently diverge because
of *our own* operations.

### 3. Stateful

Each module keeps a local SQLite database — default
`~/.studiohays/<module>.db`, one file per module — operating like
`terraform.tfstate` (thought experiment in doc 02):

- a **mirror** of remote reality (the snapshot),
- optionally a **managed** layer: resources this client created/declares, with
  desired attributes (Terraform's "planned state"),
- an **audit log** of every operation (Terraform's history).

The DB is **disposable**: delete the file, run `client.state.refresh()`, and
it rebuilds. The remote is always the source of truth.

### 4. Portable singleton

The client is a plain Python object, no framework. Repeated `Client()` calls
in one process return the same instance, keyed by configuration (doc 01).
That makes the same object safe to use from a long-lived MCP server process,
a one-shot CLI process, or an interactive REPL — and safe to pass around
between functions in a script.

## Non-goals (v0)

- **Not a Terraform replacement.** No HCL, no module graph, no cross-machine
  state locking. Single user, one machine, one state dir.
- **Not a framework.** No plugins, no decorators, no metaclass magic beyond
  the singleton registry.
- **Not a daemon.** No background polling inside the library. A surface may
  run its own refresh loop (e.g. the MCP server), but the library only knows
  about on-demand refresh.
- **Not a GUI / dashboard.**
- **Not a universal API surface.** Each module models the resources we care
  about, not every endpoint the remote happens to expose.

## Relationship to existing code

`home-assistant-mcp` (sibling project) is a one-off of the kind this library
replaces: an HA-only MCP server with its own config, client, and SQLite sync.
The plan is to **refactor it to use `studio_hays.home_assistant`** — its
tool list becomes the module's operations registry, and its MCP server
becomes one of the module's surfaces (doc 04). After that, the sibling
project can retire.

## Success criteria

The prototype is a success when all of these are true:

1. In a bare REPL, with only `STUDIOHAYS_HA_URL` / `STUDIOHAYS_HA_TOKEN` set:
   `from studio_hays.home_assistant import Client; c = Client();
   c.get.entity("light.kitchen")` returns a dict.
2. `Client() is Client()` within one process.
3. `studio-hays ha list entities --domain light` prints JSON that is
   byte-for-byte the same data the Python API returns.
4. An MCP client (e.g. opencode) sees `ha_list_entities`, `ha_get_entity`,
   `ha_refresh`, `ha_drift` and gets the same answers as the CLI.
5. Delete `~/.studiohays/home_assistant.db`; next `state.refresh` rebuilds it.
6. `studio-hays proxmox create vm --name test-vm …` appears in the PVE web UI
   and in the `managed` table; `state.plan` afterwards reports no drift.
7. The core contract test suite passes for both modules.
