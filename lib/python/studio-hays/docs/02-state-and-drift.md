# 02 — State & drift: the Terraform thought experiment

Guarantee #3 says each module is stateful via a local SQLite DB,
"operating similar to terraform". This doc is the thought experiment about
what that actually means.

## What we borrow from Terraform, what we don't

| Terraform concept | Do we borrow it? | How |
| --- | --- | --- |
| state file (`.tfstate`) | **yes** | one SQLite file per module, `~/.studiohays/<module>.db` |
| source of truth is remote | **yes** | the DB is a cache/mirror + bookkeeping, never the truth |
| `refresh` (re-read reality) | **yes** | `client.state.refresh()` |
| drift detection | **yes** | `client.state.drift()` — snapshot vs fresh pull |
| plan / apply (desired vs real) | **partially** | `state.plan()` / `state.apply()` exist **only** for resource types in "managed" mode (below) |
| `tfstate` is disposable | **yes** | delete the file → `state.refresh()` rebuilds |
| operation history | **yes** | `ops` table, `state.history()` |
| HCL desired-state files | **no** | desired state is in code/kwargs, not files (v0) |
| providers as plugins | **no** | each module is a hand-written subpackage |
| state locking (multi-machine) | **no** | single user, one machine; WAL + single-writer assumption |
| module graph / dependencies | **no** | modules are independent by rule |

## Two modes: mirror and managed (D-007)

The key insight: "stateful like Terraform" means two very different things
for different infrastructure, so the core provides **two layers** and each
module opts in per resource type.

**Mirror mode** — the DB is a *snapshot of remote reality*.
Reads hit the snapshot; `refresh()` re-pulls; drift = "what changed since the
last refresh". There is no desired state. This fits read-heavy infra where we
mostly *observe*: Home Assistant (entities and their states), node lists,
logs.

**Managed mode** — mirror **plus** a `managed` layer: resources this client
created or the user declared, with *desired* attributes. `plan()` diffs
desired vs live; `apply()` pushes desired to the remote. This fits infra we
*own and mutate*: Proxmox VMs/containers.

```
                 ┌─────────────────────────────────────────────┐
                 │  ~/.studiohays/<module>.db                  │
                 │                                             │
 remote ──refresh──►  snapshot   (mirror layer, always on)     │
                 │  managed      (desired state, opt-in)       │
                 │  ops          (audit log, always on)        │
                 │  meta         (schema v, host, last_synced) │
                 └─────────────────────────────────────────────┘
```

| | Home Assistant (v0) | Proxmox (v0) |
| --- | --- | --- |
| entities / nodes / VMs | mirror | mirror |
| entities we "adopt" | managed (optional, later) | managed (VMs created via the client) |

## Identity vs volatile fields — the drift-noise problem

Thought experiment: HA `states` change *constantly* (a sensor ticks every few
seconds). If drift compared whole JSON blobs, `state.drift()` would be
perpetually red and meaningless.

Solution: each module declares, per resource type, which fields are
**identity** (changes = real drift) vs **volatile** (changes = normal life):

```python
# home_assistant
IDENTITY = {"entity": ["entity_id", "attributes.friendly_name"]}
# proxmox
IDENTITY = {"vm": ["name", "cores", "memory", "disk_size", "node"]}
```

`drift()` compares identity fields only; `changed` entries include a compact
diff. Volatile fields are still stored (they're what reads return) — they just
don't trigger drift. Modules can expose `drift(volatile=True)` to see *all*
changes (that's basically "what changed since last refresh", useful for HA).

## Database schema

Same four tables in every module DB. SQLite, WAL, one writer.

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- module metadata
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- rows: module='home_assistant', schema_version='1',
--       host='http://homeassistant.local:8123', last_synced='…'

-- MIRROR layer: what the remote currently looks like
CREATE TABLE snapshot (
  id            TEXT NOT NULL,          -- remote's own id: entity_id, VMID…
  resource_type TEXT NOT NULL,          -- 'entity' | 'vm' | 'node' | …
  attributes    TEXT NOT NULL,          -- JSON: the full object as returned by remote
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  deleted_at    TEXT,                   -- tombstone; row kept until purge
  PRIMARY KEY (resource_type, id)
);

-- MANAGED layer (opt-in): what we want the remote to look like
CREATE TABLE managed (
  id            TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  desired       TEXT NOT NULL,          -- JSON: desired identity attributes
  status        TEXT NOT NULL DEFAULT 'ok',   -- ok | pending | tainted | drifted
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (resource_type, id),
  FOREIGN KEY (resource_type, id) REFERENCES snapshot(resource_type, id)
);

-- AUDIT: every op the client performed (Terraform's history)
CREATE TABLE ops (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  op            TEXT NOT NULL,          -- create | update | delete | refresh | apply | …
  resource_type TEXT,
  id            TEXT,
  args          TEXT,                   -- JSON
  result        TEXT,                   -- JSON, or the error message
  ok            INTEGER NOT NULL
);
```

Notes:

- `attributes` is the **verbatim** remote JSON (plus nothing). Parsing happens
  at the API boundary, not in state. This keeps refresh dumb and self-healing.
- Tombstones (`deleted_at`) let `drift()` report *removed* resources and let
  reads return "deleted at …" for a short grace period. A periodic purge (or
  `state.reset()`) clears them.
- `meta.last_synced` powers the `stale` flag on every read (doc 03).

## The state verbs

All under `client.state` — the sixth namespace (doc 03).

| Verb | Mirror mode | Managed mode | Pre/postcondition |
| --- | --- | --- | --- |
| `refresh()` | pull remote, atomically replace `snapshot`, set `last_synced` | same, plus re-evaluate `managed.status` | remote reachable; post: snapshot == reality |
| `drift()` | refresh + compare old snapshot vs new, on identity fields | same | returns `{added, removed, changed}`; does not mutate state |
| `plan()` | n/a | desired vs live (post-refresh); returns per-resource diffs | only for `managed` rows |
| `apply()` | n/a | push each planned change to remote, update `desired`, clear `drifted` | post: plan is empty (or rows `tainted`) |
| `show()` | dump snapshot (filterable by type/id) | + managed rows + statuses | local only, no network |
| `reset()` | delete the DB file | same | next op rebuilds; post: empty state |
| `history(n=20)` | last n `ops` rows | same | local only |

`drift()` output shape (stable JSON — CLI and MCP print it as-is):

```json
{
  "module": "home_assistant",
  "host": "http://homeassistant.local:8123",
  "last_synced": "2026-09-13T15:04:12+00:00",
  "added":   [{"type": "entity", "id": "light.new_room"}],
  "removed": [{"type": "entity", "id": "light.old_room"}],
  "changed": [
    {"type": "entity", "id": "sensor.temperature",
     "diff": {"attributes.friendly_name": {"was": "Hallway", "is": "Hall"}}}
  ]
}
```

## CRUD write-through contract

Every `create` / `update` / `delete` follows the same sequence:

```
1. call the remote API
2. if it succeeds:
     upsert snapshot row (from the remote's response, verbatim)
     upsert managed row (managed mode: record desired attrs)
     insert ops row (ok=1)
3. if it fails:
     insert ops row (ok=0, result=error)
     leave snapshot/managed untouched
     (managed mode: if remote is uncertain, mark row 'tainted' —
      e.g. timeout after request sent. Next plan() surfaces it.)
```

- Step 2 happens in one SQLite transaction (atomic).
- **Writes go remote-first.** If the remote says yes but our DB write fails,
  we'd rather log a `StateError` than report a lie; the next `refresh()`
  self-heals the snapshot, and `ops` shows the gap.
- `create` in managed mode always records `desired` — that's how a VM we made
  becomes plan-able later.
- `delete` tombstones the snapshot row and removes (or marks) the managed row.

## Thought experiments: failure modes

**HA is down during refresh.** Snapshot stays as-is; `meta.last_synced` is
now old, so every read carries `stale: true` (doc 03). No op raises just
because the world is stale — only ops that *need* the remote raise
`RemoteError`. Reads from state keep working.

**Crash between remote success and DB write (the write-through gap).**
The `ops` log shows `ok=0` with no matching snapshot change. Next
`refresh()` reconciles the snapshot; managed rows that were `pending`
become `ok` if the resource exists with the desired identity fields, else
`tainted`. No manual state surgery needed.

**Two clients refresh at once (CLI + MCP server).**
WAL lets readers proceed; refresh uses `BEGIN IMMEDIATE` so two writers
serialize. Single-user homelab: last refresh wins, both are full pulls, so
convergence is automatic. (Cross-process file locking is Q-002-ish; not v0.)

**An entity is renamed on the remote.**
Refresh sees the old id missing, the new id new → drift reports
`removed: old`, `added: new`. Self-healing: we don't try to guess
renames (Terraform doesn't either, without `import`).

**The DB file is deleted.**
`meta` is gone, so the next refresh treats it as a fresh DB: full rebuild,
`first_seen` resets. That's the disposability guarantee.

**Remote returns a malformed payload.**
`attributes` stores the raw JSON as received (verbatim rule); validation is
the API layer's job (doc 03, Q-008). A bad payload fails the op with
`RemoteError`, state untouched.

## Data dir & lifecycle (D-005)

- Default: `~/.studiohays/<module>.db` — per-user, shared across CLI, MCP,
  and scripts, independent of where the code lives.
- Overrides: `STUDIOHAYS_DATA_DIR` (whole dir), `STUDIOHAYS_<MODULE>_DB`
  (single file).
- Parent dir is created on first use; the DB file is git-ignored wherever it
  lives (in a repo checkout, `.data/` or similar, per project).
- `state.reset()` = delete file + clear in-process `StateStore`. The module
  must never assume the DB pre-exists.
