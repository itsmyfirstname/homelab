# studio-hays — prototype docs

These are the design docs for the prototype we're about to build. They are
**thought experiments, not a finished spec**: each doc states the problem, the
options considered, and the decision (or open question) that follows.

## The pitch

`studio-hays` is a library of independent Python modules, one per slice of the
homelab (`studio_hays.home_assistant`, `studio_hays.proxmox`, …). Every module
is a portable, stateful client you can import into a script, drive from a CLI,
or expose as an MCP server — with the *same code* doing all three.

```python
from studio_hays.home_assistant import Client as studiohays_ha

studiohays_ha = studiohays_ha()  # config falls back to env vars
studiohays_ha.get.entity("light.kitchen")
```

Each module is self-sustaining in four ways (the "guarantees", expanded in
[00](00-vision.md)):

1. **lazy** — `Client()` opens no sockets or files; the connection loads on first use
2. **CRUD** — reads and writes against the real infrastructure, not just a cache
3. **stateful** — a local SQLite state DB, Terraform-like: refresh, drift, plan
4. **portable singleton** — one object, usable from a REPL, a CLI, or an MCP server

## Read order

| Doc | What it settles |
| --- | --- |
| [00-vision.md](00-vision.md) | why we're doing this; the four guarantees; non-goals; success criteria |
| [01-architecture.md](01-architecture.md) | package layout, the core kernel, singleton semantics, lazy loading, config |
| [02-state-and-drift.md](02-state-and-drift.md) | the Terraform thought experiment: SQLite state, refresh / drift / plan / apply |
| [03-api-contract.md](03-api-contract.md) | the Client surface: namespaced verbs, operations registry, errors, sync-vs-async |
| [04-surfaces-cli-and-mcp.md](04-surfaces-cli-and-mcp.md) | the CLI command tree and the MCP adapter design |
| [05-roadmap-and-open-questions.md](05-roadmap-and-open-questions.md) | build order, full decision log, open questions |

## Decision log (short form)

| ID | Decision | Status |
| --- | --- | --- |
| D-001 | sync core (httpx sync client + stdlib `sqlite3`) | proposed |
| D-002 | import name is `studio_hays` (PEP 8), not `studiohays` | proposed |
| D-003 | API returns plain `dict`s (JSON-first) | proposed |
| D-004 | singleton = per-process registry keyed by config fingerprint | proposed |
| D-005 | state lives in `~/.studiohays/<module>.db` (override: `STUDIOHAYS_DATA_DIR`) | proposed |
| D-006 | each module declares one **operations registry**; CLI + MCP are generated from it | proposed |
| D-007 | state model = mirror layer + optional managed layer (per resource type) | proposed |
| D-008 | one `studio-hays` CLI dispatcher with per-module subcommands; JSON output by default | proposed |
| D-009 | env vars use the prefix `STUDIOHAYS_<MODULE>_<KEY>` | proposed |
| D-010 | HA refresh is a full atomic replace of the snapshot | proposed |
| D-011 | Python floor 3.12 | decided |

Flip "proposed" → "decided" as we review. Full log with rationale:
[05](05-roadmap-and-open-questions.md).
