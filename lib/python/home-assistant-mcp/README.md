# home-assistant-mcp

An [MCP](https://modelcontextprotocol.io) server that gives LLMs access to your Home Assistant
installation through a local SQLite cache. It is a thin tool layer over
[`studio_hays.home_assistant`](../studio-hays/), which owns the HTTP client, the cache and the sync
policy; this project keeps its `.env`, tool names and result shapes.

- **Source of truth:** Home Assistant. The SQLite file is a disposable cache — delete it and the
  next sync rebuilds it.
- **Sync:** on demand. A tool call refreshes the cache first when it is older than
  `2 x SYNC_INTERVAL` seconds (no background poller). Responses carry `last_synced` and a `stale`
  flag, which turns true when Home Assistant couldn't be reached and cached data was served.
- **Transport:** stdio JSON-RPC (run as a child process by your MCP client).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
uv sync
cp .env.example .env   # then fill in HA_URL and HA_TOKEN (long-lived access token)
```

`.env` is read from the project directory (and from the current directory as a fallback).
Process environment variables always win over `.env`.

| Variable       | Default                   | Notes                                  |
| -------------- | ------------------------- | -------------------------------------- |
| `HA_URL`       | — (required)              | e.g. `http://homeassistant.local:8123` |
| `HA_TOKEN`     | — (required)              | long-lived access token                |
| `SYNC_INTERVAL`| `5`                       | cache is refreshed when older than 2x this |
| `DB_PATH`      | `<project>/.data/home_assistant.db` | SQLite file (studio-hays schema) |

## Run

```sh
uv run home-assistant-mcp
```

Speaks MCP over stdin/stdout; all logging goes to stderr.

## Tools

### `list_domains`

Entity domains with counts, largest first — useful for orienting before drilling in.

```json
{"last_synced": "2026-09-13T03:25:34+00:00", "stale": false,
 "domains": [{"domain": "sensor", "count": 73}, {"domain": "light", "count": 16}]}
```

### `list_entities(domain?, search?, area?, label?, limit=100)`

Compact rows (`entity_id`, `state`, `friendly_name`) from the cache. `domain` filters by
domain prefix (e.g. `light`), `search` is a case-insensitive substring match on
`entity_id` or friendly name, `area` matches the entity's own area else its device's, and
`label` matches labels on the entity or its device (areas and labels by name or id); all
can be combined.

```json
{"last_synced": "...", "stale": false, "total": 16,
 "entities": [{"entity_id": "light.bedroom", "state": "off", "friendly_name": "Light Switch"}]}
```

### `get_entity(entity_id)`

Full state plus the complete `attributes` object for one entity, and a `registry` block: area
(with `area_source`: `entity` or `device`), floor, device, labels, name override — or
`{"registered": false}` for entities outside the entity registry.

### Registry reads: `list_areas`, `list_labels`, `get_device(device)`

Areas with floor and device/entity counts; labels with counts; one device (by name, id, or one
of its entity_ids) with its area, labels and entities.

### Registry writes (mutating; all take `dry_run`)

| Tool | Changes |
| --- | --- |
| `update_entity(entity_id, name?, area?, add_labels?, remove_labels?, new_entity_id?)` | one entity's registry entry |
| `update_device(device, name?, area?, add_labels?, remove_labels?)` | one device; entities inheriting its area move with it |
| `repurpose_device(device, area?, name?, add_labels?, remove_labels?, rename_entity_ids=false)` | device + the entity overrides/ids that mention its old name, step by step, stopping at the first failure |
| `update_area(area, name?, floor?)` | rename an area / assign its floor (`floor=""` clears) |
| `create_label(name)`, `create_area(name)` | a new label / area |
| `delete_area(area, force=false)` | remove an area; refuses while devices/entity overrides use it unless `force` |

Only the fields you pass change; `""` clears a name or area (inherit again). Results carry
`changes` (`was`/`is`) and `before`/`after`. Registry edits go over Home Assistant's WebSocket
API (the REST API can't edit registries).

Area and label names must be unique after Home Assistant's normalization (case and spaces
ignored); a clash is reported before anything is sent.

### Automations: `list_automations`, `get_automation(automation_id)`, `save_automation(config, automation_id?)`, `delete_automation(automation_id)`

Home Assistant 2026.9 has no automation list API, so `list_automations` reads the `automation.*`
entities (their `id` attribute is the `automation_id`). `save_automation` creates or replaces
through `/api/config/automation/config/{id}` after Home Assistant validates the config
(`dry_run=true` validates only); HA reloads automations itself. Save and delete are mutating.

### `call_service(domain, service, service_data?, return_response=false)`

Fires a service and returns `targets` (what you asked for) and `changed_entities` (what HA
reported changed before replying). Async integrations (Lutron, transitions) change state after the
reply, so an empty `changed_entities` isn't a failure — a `note` says to verify with `get_entity`.
Response-only services (e.g. `weather.get_forecasts`) need
`return_response=true` and return their data under `response`. Registry edits are not services
(Home Assistant 2026.9 removed `homeassistant.update_area`) — use the registry tools above.

Errors (bad token, unknown entity/area/label, ambiguous names, entities outside the registry)
come back as tool errors the model can read, including Home Assistant's own reason for a failed
request, e.g. `entity not found: light.kitchen`, `… HTTP 400 …: Service x.y not found.` or
`unknown area 'Attic' — known: Kitchen, Living Room, …`.

## opencode

In `~/.config/opencode/opencode.jsonc`:

```jsonc
"mcp": {
  "home-assistant": {
    "type": "local",
    "command": ["uv", "--project", "/home/mehays/projects/homelab/lib/python/home-assistant-mcp", "run", "home-assistant-mcp"],
    "cwd": "/home/mehays/projects/homelab/lib/python/home-assistant-mcp",
    "enabled": true,
    "timeout": 30000
  }
}
```

Restart opencode after editing config. The generous `timeout` covers the first request, which
waits for the initial sync.

## Design notes

- All Home Assistant logic lives in `studio_hays.home_assistant` (path dependency on
  `../studio-hays`, editable). `server.py` only maps this project's `.env` to that client and keeps
  the four tool signatures stable for existing MCP configs.
- The cache is studio-hays' state store: a full atomic replace per refresh (self-healing: renamed or
  removed entities disappear), plus an audit log of refreshes and service calls. It is disposable —
  delete the file and the next call rebuilds it.
- `call_service` doesn't write the cache; it invalidates it, so the next read refreshes.

## Test

```sh
uv run pytest
```
