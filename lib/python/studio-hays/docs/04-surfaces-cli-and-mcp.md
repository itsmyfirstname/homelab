# 04 — Surfaces: CLI and MCP

Both surfaces are **thin adapters over the operations registry** (D-006).
Nothing surface-specific may touch the remote or the DB. The parity goal:
anything you can do in Python, you can do from the CLI and MCP, with the
same data.

## Design principles

1. **JSON first.** CLI output and MCP results are the same dicts the Python
   API returns (doc 03). Humans get an optional table view; machines get
   stable JSON that pipes into `jq`.
2. **Generated, not hand-written.** Argument parsing (CLI) and tool schemas
   (MCP) come from `Operation.params`. One declaration, three surfaces.
3. **Fast failure.** The CLI calls `connect()` eagerly: missing env or an
   unreachable host is reported before doing work, with the env var named.
4. **Exit codes carry meaning** so shell scripts and CI can branch.

## CLI (D-008)

One entry point, `studio-hays`, with a module subcommand per module:

```
studio-hays <module> <verb> <resource> [options]

modules:
  ha         home_assistant      (short name for scripts)
  proxmox    proxmox

global flags:
  --json          print JSON (the default)
  --table         print a human table
  --fresh         refresh from remote before reading
  --data-dir D    override STUDIOHAYS_DATA_DIR
  --env-file F    override STUDIOHAYS_ENV_FILE
  -v, -vv         warning / debug logging (to stderr)
  --version
```

### Command tree (HA)

```
studio-hays ha list domains
studio-hays ha list entities [--domain D] [--search S] [--area A] [--label L] [--limit N]
studio-hays ha list areas | labels
studio-hays ha get entity <entity_id>
studio-hays ha get device <name|id|entity_id>
studio-hays ha update entity <entity_id> [--name N] [--area A] [--add-labels a,b]
                         [--remove-labels c] [--new-entity-id ID] [--dry-run]
studio-hays ha update device <device> [--name N] [--area A] [--add-labels …] [--dry-run]
studio-hays ha repurpose <device> [--area A] [--name N] [--add-labels …]
                         [--remove-labels …] [--rename-entity-ids] [--dry-run]
studio-hays ha create label|area <name> [--dry-run]
studio-hays ha query <text> [--domain D]
studio-hays ha call <domain> <service> [--data '{"k":"v"}']
studio-hays ha state refresh
studio-hays ha state drift
studio-hays ha state show [--type entity] [--id ID]
studio-hays ha state reset
studio-hays ha state history [N]
studio-hays ha mcp                      # per-module MCP server on stdio
```

### Command tree (Proxmox, sketch)

```
studio-hays proxmox list nodes
studio-hays proxmox list vms [--node N]
studio-hays proxmox get vm <vmid>
studio-hays proxmox create vm --name NAME --node NODE --cores N --memory MB
                         [--os cloud-init] [--disk GB]
studio-hays proxmox update vm <vmid> [--name …] [--cores N] [--memory MB]
studio-hays proxmox delete vm <vmid>
studio-hays proxmox call vm-start <vmid> | vm-stop | vm-restart
studio-hays proxmox state refresh | drift | plan | apply | show | history
```

Note the mapping: CLI `list`/`get` ↔ Python `get.entities`/`get.entity` —
the CLI uses `list <plural>` / `get <singular>` so commands read naturally;
the registry's `verb`/`resource`/`name` fields are the translation table.

### Output

```
$ studio-hays ha get entity light.kitchen
{
  "last_synced": "2026-09-13T15:04:12+00:00",
  "stale": false,
  "entity": {
    "entity_id": "light.kitchen",
    "state": "off",
    "attributes": {"friendly_name": "Kitchen Light", "supported_features": 391}
  }
}

$ studio-hays ha list entities --domain light --table
ENTITY_ID        STATE  FRIENDLY_NAME
light.kitchen    off    Kitchen Light
light.bedroom    on     Bedroom Light
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | ok (or: drift check ran and found drift — see below) |
| 1 | error (config, remote, state) — JSON error object on stdout, details on stderr |
| 2 | not found |
| 3 | drift detected (`state drift` with `--strict`; v1: also `apply --check`) |

Errors print a one-line JSON object to stdout (`{"error": "Home Assistant
rejected HA_TOKEN (HTTP 401)"}`) so `jq` still works, with a human trace on
stderr under `-v`.

## MCP

Two server shapes, both stdio JSON-RPC, both generated from the registry:

**Per-module server** — `studio_hays/home_assistant/mcp.py`:

```python
def create_server(client: Client | None = None) -> FastMCP:
    client = client or Client()
    mcp = FastMCP(f"studio-hays.{client.module}")
    for op in OPERATIONS:
        mcp.tool(name=f"ha_{op.name}", description=op.doc)(wrap(op, client))
    return mcp
```

Run: `studio-hays ha mcp` (spawns the single-module server on stdio).

**Aggregate server** — one process, many modules:

```
studio-hays mcp --modules home_assistant,proxmox
```

Tools are prefixed per module (`ha_*`, `proxmox_*`); the registry makes this
trivial because every op already knows its module and name. (Q-004: whether
the per-module form is even needed once the aggregate exists — lean: keep
both, they're cheap.)

### Tool conventions

- tool name: `<module_prefix>_<op.tool_name>` (`Operation.tool`, else the op name) — `ha_list_entities`,
  `ha_get_entity`, `ha_drift`, `proxmox_create_vm`
- description: `Operation.doc` (one line, human/LLM readable)
- input schema: generated from `Operation.params` (name, type, required,
  default, description)
- result: the operation's dict, JSON-serialized — the *same* envelope as the
  CLI, including `last_synced` / `stale`
- errors: tool errors with the same one-line message the CLI prints
- logging: stderr only (stdio is the protocol)
- the server calls `connect()` at startup; a bad env var fails the MCP
  handshake with a readable message, not a silent empty toolset

### Example client config (opencode)

```jsonc
"mcp": {
  "studio-hays": {
    "type": "local",
    "command": ["uv", "--project", "/home/mehays/projects/homelab/lib/python/studio-hays",
                "run", "studio-hays", "mcp", "--modules", "home_assistant"],
    "cwd": "/home/mehays/projects/homelab/lib/python/studio-hays",
    "enabled": true,
    "timeout": 30000
  }
}
```

(Generous timeout: the first tool call may pay the initial refresh.)

## Parity matrix

| Cap | Python | CLI | MCP |
| --- | --- | --- | --- |
| all `get/query` reads | ✅ | ✅ | ✅ |
| all `create/update/delete` writes | ✅ | ✅ | ✅ (flagged "mutating" in tool docs) |
| `call` extras (HA services, PVE start/stop) | ✅ | ✅ | ✅ |
| `state.*` verbs | ✅ | ✅ | ✅ |
| `fresh=True` | ✅ | `--fresh` | param on read tools |
| long-running watch/poll | manual loop | v1 (`--watch`?) | server-side refresh loop (v1) |
| `Client.reset()` | ✅ | n/a (process exit) | n/a |

## REPL / script usage (the pitch, concretely)

```python
# quick check in a REPL
from studio_hays.home_assistant import Client as ha
ha().get.entities(domain="light")

# a script
from studio_hays.proxmox import Client as pve
c = pve()
for vm in c.get.vms():
    if vm["status"] == "stopped":
        print(vm["vmid"], vm["name"])

# two instances, one process (lab + prod)
lab  = pve(url="https://pve.lab:8006", token_user="root@PVE", token_id="lib", token_secret="…")
prod = pve()   # env config
```
