# studio-hays

A library of independent Python modules, one per slice of the homelab —
importable from a script, drivable from a CLI, exposable as an MCP server.
Each module is a lazy, CRUD-capable, SQLite-stateful, portable singleton
client for its piece of infrastructure.

```python
from studio_hays.home_assistant import Client as ha

ha = ha()                      # config from env: STUDIOHAYS_HA_URL / STUDIOHAYS_HA_TOKEN
ha.get.entity("light.kitchen")
```

```sh
studio-hays ha list entities --domain light
studio-hays proxmox create vm --name test-vm --cores 2 --memory 4096
studio-hays mcp --modules home_assistant,proxmox
```

## Prototype docs

The design lives in [docs/](docs/README.md) — start with
[00-vision.md](docs/00-vision.md). Decisions and open questions are tracked
in [05](docs/05-roadmap-and-open-questions.md).

## Status

**M1 done** — core kernel + `_fixture` module + contract suite (11/11 green):

```sh
uv run studio-hays fixture --list-ops
uv run studio-hays fixture create_widget name=hello
uv run studio-hays fixture state_drift
```

**M2 code done** — `home_assistant` module (mirror mode), registry-generated
CLI and MCP server:

```sh
export STUDIOHAYS_HA_URL=http://homeassistant.local:8123 STUDIOHAYS_HA_TOKEN=…
uv run studio-hays ha list domains
uv run studio-hays ha list entities --domain light
uv run studio-hays ha call light turn_on --data '{"entity_id": "light.kitchen"}'
uv run studio-hays mcp --modules home_assistant     # stdio MCP server
```

Next: cut `home-assistant-mcp`'s opencode config over, then **M3** — `proxmox`.
Build order and success criteria:
[docs/05-roadmap-and-open-questions.md](docs/05-roadmap-and-open-questions.md).

## Develop

```sh
uv sync
uv run studio-hays --help
```
