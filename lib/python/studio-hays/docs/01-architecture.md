# 01 — Architecture

## Big picture

```
              surfaces (all optional, all thin adapters)
  ┌────────────────┐  ┌────────────────┐  ┌──────────────────────┐
  │ script / REPL  │  │ studio-hays CLI│  │ MCP server (stdio)   │
  │   import       │  │                │  │ opencode / Claude …  │
  └───────┬────────┘  └───────┬────────┘  └──────────┬───────────┘
          │                   │                      │
          ▼                   ▼                      ▼
  ┌────────────────────────────────────────────────────────────────┐
  │  module Client   (studio_hays.home_assistant / .proxmox / …)  │
  │  namespaced verbs: get / query / create / update / delete /    │
  │  state  (+ module-specific extras like `call`)                │
  │  OPERATIONS registry — single source of truth for surfaces    │
  └──────┬─────────────────────────────────────┬───────────────────┘
         │                                     │
         ▼                                     ▼
  ┌──────────────────┐              ┌─────────────────────────┐
  │ core kernel      │              │ remote API              │
  │ config · lazy    │              │ (httpx, one client per  │
  │ singleton        │              │  module, per module)    │
  │ StateStore       │              └─────────────────────────┘
  └──────┬───────────┘
         ▼
  ┌────────────────────────────┐
  │ ~/.studiohays/             │
  │   home_assistant.db        │
  │   proxmox.db               │
  └────────────────────────────┘
```

Rules of the picture:

- **Surfaces never talk to the remote or the DB directly.** They call Client
  methods. That's what keeps CLI output, MCP tool results, and Python returns
  identical (D-006, D-008).
- **Modules never import each other.** A module imports `studio_hays.core`
  and its own files. `home_assistant` knowing about `proxmox` is a bug.
- **The core kernel knows nothing about any infrastructure.** No "entity",
  no "VM", no "node" — only config, lifecycle, and generic state tables.

## Package layout

```
src/studio_hays/
  __init__.py              # version only; importing studio_hays is side-effect free
  core/
    __init__.py
    config.py              # env/.env/kwargs resolution, STUDIOHAYS_* prefixes
    base.py                # BaseClient: singleton registry, lazy connect/close
    state.py               # StateStore: sqlite3, schema, refresh/drift helpers
    errors.py              # exception hierarchy
    testing.py             # FakeRemote + contract test suite
  home_assistant/
    __init__.py            # from .client import Client   (the public line)
    client.py              # Client(BaseClient) + API wrapper (httpx)
    operations.py          # OPERATIONS registry (names, docs, params, handlers)
    mcp.py                 # create_server() -> FastMCP   (surface)
  proxmox/
    __init__.py            # from .client import Client
    client.py
    operations.py
    mcp.py
  cli.py                   # studio-hays dispatcher (argparse) — the CLI surface
```

Each module subpackage is deliberately self-contained: if you copied
`studio_hays/proxmox/` plus `studio_hays/core/` into another repo, it would
work. That's the "independent module" part of the pitch made concrete.

## The core kernel

`studio_hays.core` is small, stdlib + httpx only, and contains **no**
infrastructure knowledge.

### `config.py` — config resolution

Resolution order (first wins), per key:

1. explicit kwargs to `Client(**kwargs)`
2. process environment
3. `.env` file — discovered in this order: `$STUDIOHAYS_ENV_FILE`, then `./.env`
4. module defaults (e.g. timeout 10 s, data dir `~/.studiohays`)

Env naming (D-009): `STUDIOHAYS_<MODULE>_<KEY>`, e.g.
`STUDIOHAYS_HA_URL`, `STUDIOHAYS_HA_TOKEN`, `STUDIOHAYS_PROXMOX_URL`. Global
keys have no module segment: `STUDIOHAYS_DATA_DIR`, `STUDIOHAYS_ENV_FILE`,
`STUDIOHAYS_TIMEOUT`.

`config.py` returns a frozen `Config` dataclass per module, including:

- connection params (url, auth…)
- `db_path: Path` — resolved from `STUDIOHAYS_<MODULE>_DB` or
  `STUDIOHAYS_DATA_DIR/<module>.db` or `~/.studiohays/<module>.db` (D-005)
- `fingerprint()` — a hashable tuple of *all* config values. Two `Client()`s
  with different fingerprints are different instances (see singleton below).

### `base.py` — BaseClient: singleton + lazy lifecycle

```python
class BaseClient:
    module: ClassVar[str]                       # e.g. "home_assistant"
    _registry: ClassVar[dict[tuple, BaseClient]] = {}
    _lock = threading.Lock()

    def __new__(cls, **kwargs):
        cfg = resolve_config(cls.module, kwargs)
        with cls._lock:
            if cfg.fingerprint() in cls._registry:
                return cls._registry[cfg.fingerprint()]
            self = super().__new__(cls)
            cls._registry[cfg.fingerprint()] = self
            return self

    def __init__(self, **kwargs):
        if getattr(self, "_initialized", False):
            return                              # __init__ re-runs on the cached instance
        self.config = resolve_config(self.module, kwargs)
        self._http = None                       # httpx.Client, lazy
        self._state = None                      # StateStore, lazy
        self._initialized = True

    def _ensure(self):                          # called at the top of every op
        if self._http is None:
            self._http = self._open_transport() # module implements
            self._state = StateStore(self.config.db_path)

    def connect(self) -> None:                  # eager, for CLIs: fail fast
        self._ensure()

    def close(self) -> None:                    # drops transport; instance stays registered
        ...

    @classmethod
    def reset(cls) -> None:                     # for tests: forget all instances
        ...
```

Key properties:

- construction is O(env-read); first op pays the socket/file cost
- `with Client() as c:` works (`__enter__` → `connect()`, `__exit__` → `close()`)
- `close()` does **not** unregister; the next op lazily reconnects. This is
  what makes the singleton safe in long-lived MCP processes (a client can
  survive a network blip without the host app re-instantiating anything).

### `state.py` — StateStore

Owns the SQLite file (doc 02 has the full schema and semantics). Guarantees:

- stdlib `sqlite3`, WAL mode, `PRAGMA foreign_keys=ON`
- one connection per process, guarded by a lock (single-user assumption)
- every module DB has the same tables: `meta`, `snapshot`, `managed`, `ops`
- `refresh(rows)` — atomic full/merge replace of the snapshot in one
  transaction (D-010)
- `drift(last_snapshot, new_rows, identity_fields)` — pure function, returns
  `{added, removed, changed}` (doc 02)

### `errors.py`

```
StudioHaysError
├── ConfigError          # missing/invalid config (raised at first use, not init)
├── RemoteError          # network/API failure (includes status code, URL)
├── NotFoundError        # resource not on remote and not in state
├── DriftError           # raised only when a strict op demands zero drift
└── StateError           # schema mismatch, corrupt db, locked db
```

Message style: one line, actionable, names the env var or id:
`Home Assistant rejected HA_TOKEN (HTTP 401)`, `no data yet — run state.refresh`.

### `testing.py`

- `FakeRemote` — a minimal in-memory "remote" (dict store + call log) that a
  module's transport can be pointed at, so CRUD round-trips are testable with
  no network.
- the **contract suite** — parametrized tests every module Client must pass:
  lazy construction, singleton identity, env-var config, CRUD write-through,
  refresh rebuilds state, drift detection. CI runs it against each module.

## Singleton semantics (D-004)

Rules, and the thought experiments behind them:

| Scenario | Behavior |
| --- | --- |
| `Client()` twice, same config | `c1 is c2` — literally the same object |
| `Client(url="lab")` then `Client(url="prod")` | two instances; fingerprints differ (different data dirs too) |
| `Client()` after `close()` | same instance; next op lazily reconnects |
| `Client.reset()` | clears the registry (used between tests) |
| CLI process vs MCP process | separate registries (per-process); they coordinate through the SQLite file, not the registry |
| conflicting kwargs on a second call | v0: first constructor wins (its fingerprint was matched); note as a possible `ConfigError` in v1 |

Why a registry instead of a module-level global: a module global would make
"two HA instances in one process" (lab + prod in a comparison script)
impossible, and would make tests leak into each other.

## Lazy loading — what each phase costs

| Phase | Cost |
| --- | --- |
| `import studio_hays.home_assistant` | class definitions; no env read, no files |
| `Client(**kwargs)` | read env/`.env`, build `Config`; no socket, no file |
| first op (or `connect()`) | open httpx client, open/create SQLite file, run `PRAGMA`s |
| ops | HTTP + SQL as needed |

The CLI calls `connect()` immediately after construction, so a missing env
var or unreachable host is reported before doing real work. Library users get
true laziness.

## Config reference (HA module, for concreteness)

| Variable | Meaning | Default |
| --- | --- | --- |
| `STUDIOHAYS_HA_URL` | HA base URL, e.g. `http://homeassistant.local:8123` | — (required) |
| `STUDIOHAYS_HA_TOKEN` | long-lived access token | — (required) |
| `STUDIOHAYS_HA_DB` | explicit DB path | `STUDIOHAYS_DATA_DIR/home_assistant.db` |
| `STUDIOHAYS_DATA_DIR` | global state dir | `~/.studiohays` |
| `STUDIOHAYS_TIMEOUT` | HTTP timeout, seconds | `10` |
| `STUDIOHAYS_ENV_FILE` | explicit `.env` path | `./.env` fallback |

Proxmox (sketch, final in M3): `STUDIOHAYS_PROXMOX_URL`,
`STUDIOHAYS_PROXMOX_USER` (e.g. `root@PVE`), `STUDIOHAYS_PROXMOX_TOKEN`,
`STUDIOHAYS_PROXMOX_TOKEN_SECRET`, `STUDIOHAYS_PROXMOX_INSECURE` (self-signed
TLS). PVE auth is a session ticket, not a bearer token — the core stays
auth-agnostic; each module implements its own transport (open question Q-005).

## Portability rules (the checklist a new module must pass)

1. `import studio_hays.<module>` has zero side effects (no env read, no I/O).
2. No imports from sibling modules; only `studio_hays.core` + own files.
3. Third-party imports (httpx, mcp SDK, and `websockets` for modules whose
   remote needs a WebSocket API, e.g. HA registries) are the only non-stdlib deps.
4. `Client()` is constructible with **no arguments** when env is configured.
5. All state I/O goes through `StateStore`; no raw sqlite3 in module code.
6. Every public method is registered in `OPERATIONS` (or is a documented
   module-specific extra) — so CLI and MCP can be generated from the registry.
7. `FakeRemote`-based CRUD round-trip passes the contract suite.
