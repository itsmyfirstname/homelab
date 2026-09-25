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

### `list_entities(domain?, search?, limit=100)`

Compact rows (`entity_id`, `state`, `friendly_name`) from the cache. `domain` filters by
domain prefix (e.g. `light`), `search` is a case-insensitive substring match on
`entity_id` or friendly name; both can be combined.

```json
{"last_synced": "...", "stale": false, "total": 16,
 "entities": [{"entity_id": "light.bedroom", "state": "off", "friendly_name": "Light Switch"}]}
```

### `get_entity(entity_id)`

Full state plus the complete `attributes` object for one entity.

Errors (bad token, unknown entity, no data yet) come back as tool errors the model can read,
e.g. `entity not found: light.kitchen` or `no data yet - initial sync failed: Home Assistant rejected HA_TOKEN (HTTP 401)`.

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
