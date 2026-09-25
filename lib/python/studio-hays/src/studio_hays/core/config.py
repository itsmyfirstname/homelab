"""Config resolution for studio-hays modules (D-009, doc 01).

Resolution order per key, first hit wins:

1. explicit kwargs passed to ``Client(**kwargs)``
2. process environment
3. ``.env`` file — ``$STUDIOHAYS_ENV_FILE`` if set, else ``./.env``
4. the key's declared default

Env naming (D-009): ``STUDIOHAYS_<SEGMENT>_<KEY>`` (e.g. ``STUDIOHAYS_HA_URL``).
Global keys carry no segment: ``STUDIOHAYS_DATA_DIR``, ``STUDIOHAYS_TIMEOUT``,
``STUDIOHAYS_ENV_FILE``.

Construction is cheap: resolving config reads env/``.env`` but opens no
socket and no database file.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

ENV_FILE_VAR = "STUDIOHAYS_ENV_FILE"
DATA_DIR_VAR = "STUDIOHAYS_DATA_DIR"
TIMEOUT_VAR = "STUDIOHAYS_TIMEOUT"
DEFAULT_TIMEOUT = 10.0
DEFAULT_DATA_DIR = "~/.studiohays"
DB_KEY = "db"  # virtual key: STUDIOHAYS_<SEGMENT>_DB


@dataclass(frozen=True)
class Key:
    """One configurable key of a module."""

    name: str  # kwarg name, e.g. "url"
    required: bool = False
    default: str | None = None
    type: Callable[[str], object] = str


@dataclass(frozen=True)
class ModuleSpec:
    """Declarative config surface of one module."""

    module: str  # package name, e.g. "home_assistant"
    env_segment: str  # env segment, e.g. "HA"
    keys: tuple[Key, ...] = field(default=())

    def env_var(self, key_name: str) -> str:
        return f"STUDIOHAYS_{self.env_segment}_{key_name.upper()}"


@dataclass(frozen=True)
class Config:
    """Resolved, immutable configuration for one client instance."""

    module: str
    values: Mapping[str, object]
    data_dir: Path
    db_path: Path
    timeout: float
    #: env vars of required keys that resolved to nothing; raised at first use
    missing: tuple[str, ...] = ()

    def get(self, name: str, default: object = None) -> object:
        return self.values.get(name, default)

    def fingerprint(self) -> tuple:
        """Hashable identity for the singleton registry (D-004)."""
        return (
            self.module,
            tuple(sorted((k, str(v)) for k, v in self.values.items())),
            str(self.data_dir),
            str(self.db_path),
            self.timeout,
        )


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE ``.env`` file (blanks, comments, quotes)."""
    values: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return values
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _env_file_path() -> str:
    return os.environ.get(ENV_FILE_VAR, "").strip() or "./.env"


def _lookup(env_var: str, dotenv: Mapping[str, str]) -> str | None:
    """Process env wins over .env; either must be non-blank to count."""
    value = os.environ.get(env_var, "").strip()
    if value:
        return value
    value = dotenv.get(env_var, "").strip()
    if value:
        return value
    return None


def resolve(spec: ModuleSpec, **kwargs: object) -> Config:
    """Resolve a module's config: kwargs > env > .env > defaults.

    Never raises for missing required keys (the lazy guarantee): they are
    recorded in ``Config.missing`` and raised by :func:`require` at first use.
    """
    dotenv = load_env_file(_env_file_path())
    data_dir = Path(
        _lookup(DATA_DIR_VAR, dotenv) or DEFAULT_DATA_DIR
    ).expanduser()

    values: dict[str, object] = {}
    missing: list[str] = []
    for key in spec.keys:
        env_var = spec.env_var(key.name)
        if key.name in kwargs:
            raw: object = kwargs[key.name]
        else:
            raw = _lookup(env_var, dotenv)
            if raw is None:
                raw = key.default
        if raw in (None, ""):
            if key.required:
                missing.append(env_var)
            continue
        values[key.name] = key.type(raw) if isinstance(raw, str) else raw

    db_raw = _lookup(spec.env_var(DB_KEY), dotenv)
    db_path = Path(db_raw).expanduser() if db_raw else data_dir / f"{spec.module}.db"
    timeout = float(_lookup(TIMEOUT_VAR, dotenv) or DEFAULT_TIMEOUT)
    return Config(
        module=spec.module,
        values=values,
        data_dir=data_dir,
        db_path=db_path,
        timeout=timeout,
        missing=tuple(missing),
    )


def require(config: Config, spec: ModuleSpec) -> None:
    """Raise :class:`ConfigError` naming every missing required key."""
    if not config.missing:
        return
    kwargs = ", ".join(
        f"{k.name}=..." for k in spec.keys if spec.env_var(k.name) in config.missing
    )
    it = "it" if len(config.missing) == 1 else "them"
    raise ConfigError(
        f"missing {', '.join(config.missing)} — set {it} or pass {kwargs}"
    )
