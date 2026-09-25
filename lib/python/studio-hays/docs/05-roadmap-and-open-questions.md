# 05 — Roadmap, decisions, open questions

## Build order

| Milestone | Scope | Done when |
| --- | --- | --- |
| **M0 — docs** | this set; decisions flipped to "decided" | you've read and flipped the log |
| **M1 — core kernel** ✅ | `core/{config,base,state,errors,testing}.py`; contract suite runs against a toy `_fixture` module | toy module passes: lazy, singleton, env config, CRUD write-through, refresh-rebuild, drift — **done** (11/11 tests, CLI smoke green) |
| **M2 — home_assistant** | mirror mode; refactor `home-assistant-mcp`'s behavior onto the module: `get.domains/entities/entity`, `query`, `call.service`, `state.*`; CLI + per-module MCP server | success criteria 1–5 of doc 00; the sibling project's opencode config points at `studio-hays` |
| **M3 — proxmox** | mirror + managed; `get.nodes/vm(s)`, `create/update/delete.vm`, `call.vm_*`, `state.*` incl. plan/apply | success criterion 6; `ops` history shows a real create; plan empty after apply |
| **M4 — polish** | aggregate MCP server, `--table` views, docs per module, packaging extras (`[mcp]`), CI (contract suite per module) | `studio-hays mcp --modules home_assistant,proxmox` serves both; clean `uv build` |

Deliberate sequencing: M2 is the *read-heavy/mirror* test of the design;
M3 is the *write-heavy/managed* test. If M3's plan/apply feels wrong, we fix
the core before a third module multiplies the pain.

### M1 implementation notes (settled while building)

- **Write-through refreshes the sync stamp**: a successful create/update
  also moves `last_synced`, so reads right after a write are not `stale`.
- **Tombstones are hidden by default**: `get_snapshot()` excludes
  `deleted_at IS NOT NULL` rows (opt in with `include_deleted`); a deleted
  object reads as *not found* until the next refresh.
- **Full replace prunes the managed layer**: `replace_snapshot` keeps
  managed rows only for objects that still exist remotely, and orders the
  statements as clear-managed → clear-snapshot → insert-snapshot →
  re-insert-managed (SQLite checks FKs per statement).
- **Drift report shape**: `{added, removed, changed}` where each `changed`
  entry carries a `diff` of identity fields as `{was, is}`; module-level
  `state.drift()` adds `count`.

### M2 mapping (home-assistant-mcp → module)

| today (sibling project) | becomes |
| --- | --- |
| `config.py` (Settings, env) | `core.config` + `home_assistant` defaults |
| `client.py` (httpx.AsyncClient) | `home_assistant/client.py` (sync httpx) |
| `db.py` + `sync.py` (aiosqlite, full replace) | `core.state.StateStore.refresh()` (D-010) |
| `server.py` (FastMCP tools) | `home_assistant/operations.py` + `mcp.py` |
| `.env` handling | `core.config` `.env` discovery |

## Decision log (full)

| ID | Decision | Rationale (short) | Status |
| --- | --- | --- | --- |
| D-001 | sync core (httpx sync + stdlib sqlite3) | the pitch example is plain Python; REPL/script-friendly; FastMCP runs sync tools in a worker thread; porting the old async HA code is mechanical. Async-only host would justify revisiting. | proposed |
| D-002 | import name `studio_hays` (not `studiohays`) | PEP 8; the uv scaffold already uses it; the pitch's `studiohays.` is the marketing name, `studio_hays.` is the import path | proposed |
| D-003 | plain dicts, JSON-first returns | one value shape flows through Python/CLI/MCP unchanged; no validation deps in v0 (Q-008) | proposed |
| D-004 | singleton = per-process registry keyed by config fingerprint | `Client() is Client()`; multiple hosts per process possible; `reset()` for tests; no module-global leakage | proposed |
| D-005 | state in `~/.studiohays/<module>.db`, overridable | per-user, shared across surfaces, independent of code location; disposable | proposed |
| D-006 | one operations registry per module; CLI + MCP generated from it | single source of truth; parity is structural, not aspirational; contract tests police it | proposed |
| D-007 | mirror layer always on; managed layer opt-in per resource type | HA is observed, PVE is owned; one core schema serves both | proposed |
| D-008 | single `studio-hays` dispatcher, module subcommands, JSON default | one thing to install/discover; JSON pipes to jq; `--table` for humans | proposed |
| D-009 | `STUDIOHAYS_<MODULE>_<KEY>` env prefix | no collisions across modules; global keys un-prefixed | proposed |
| D-010 | HA refresh = full atomic replace (DELETE+INSERT in one txn) | proven by the existing project; self-healing (renames/removals disappear); single-user write pattern fits | proposed |
| D-011 | Python floor 3.12 | server portability; the lib should run anywhere the homelab boxes run (closes Q-001) | decided |

## Open questions

| ID | Question | Options | Lean |
| --- | --- | --- | --- |
| Q-001 | ~~Python floor: 3.14 vs 3.12?~~ **Decided: 3.12** (server portability) | — | closed |
| Q-002 | Background polling inside the lib (a `watch`/daemon mode)? | on-demand only / per-surface loops / core watcher | on-demand in the lib; MCP server may own a refresh loop in v1 |
| Q-003 | Managed mode in v0 or v2? | v0 (PVE create records desired) / v2 (mirror+CRUD first) | v0 — M3's whole point is testing plan/apply early |
| Q-004 | Per-module MCP servers, or aggregate only? | both / aggregate only | both (cheap, generated) |
| Q-005 | Core `Auth` abstraction? | none (per-module transport) / tiny core interface | none in v0; HA bearer + PVE session-ticket are different enough to model per-module |
| Q-006 | Which module after proxmox? | tailscale / pfsense / unifi / syncthing / zfs / … | pick by usage frequency; the contract suite makes #3 cheap |
| Q-007 | `.env` discovery rules | `STUDIOHAYS_ENV_FILE` → `./.env` → stop / also search up the tree | stop at `./.env` (predictable for a library) |
| Q-008 | Validate remote payloads (pydantic/dataclasses)? | trust dicts in v0 / validate at API boundary | trust dicts in v0; `attributes` stays verbatim (doc 02) |
| Q-009 | CLI framework: argparse vs typer? | stdlib argparse / typer extra | argparse for v0 (zero deps); typer is a `[cli]` extra later |
| Q-010 | HA `call.service` and its state interaction | no state effect / optional write-through | no state effect in v0 (fire-and-forget; drift catches consequences) |
| Q-011 | Cross-process locking for refresh (CLI + MCP concurrent)? | `BEGIN IMMEDIATE` only / file lock | v0: `BEGIN IMMEDIATE`; revisit if corruption is ever observed |

## Later module candidates (fill in as we go)

Each is just: a `client.py` (transport + API), an `operations.py`
(registry), `IDENTITY` fields, and a mode (mirror/managed). The contract
suite does the quality control.

- `studio_hays.tailscale` — tailnet status, peers, dns (mirror)
- `studio_hays.pfsense` / firewall — interfaces, aliases, NAT (mirror + managed)
- `studio_hays.unifi` — controllers, APs, clients (mirror)
- `studio_hays.syncthing` — devices, folders, sync status (mirror)
- `studio_hays.zfs` — pools, datasets (mirror + managed)
- `studio_hays.docker` — containers, images (mirror + managed) — *if* the lab
  keeps running docker alongside PVE

## Definition of "prototype done"

- M0–M4 complete
- decision log all "decided"
- two real modules passing the contract suite
- one real LLM session (opencode) doing a homelab task through the MCP
  surface without a human noticing any of the plumbing
