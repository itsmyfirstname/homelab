"""BaseClient: singleton registry + lazy lifecycle (D-004, D-010, doc 01).

Lifecycle costs (doc 01):

- ``import``        — class definitions, zero side effects
- ``Client(**kw)``  — env/``.env`` read only; no socket, no file
- first op /        — opens transport + SQLite (``_ensure``)
  ``connect()``

``close()`` drops the transport but keeps the instance registered, so a
long-lived MCP process survives network blips without re-instantiating.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar, Self, TypeVar, cast

from .config import Config, ModuleSpec, require, resolve
from .errors import StudioHaysError
from .state import StateStore

T = TypeVar("T")


class StateNamespace:
    """The ``client.state`` verbs every module gets, built on ``_pull()``.

    Modules may subclass to add resource-specific verbs (e.g. ``show``).
    """

    def __init__(self, client: "BaseClient") -> None:
        self._c = client

    def refresh(self) -> dict:
        c = self._c
        rows = c._audited("refresh", None, None, None, c._pull)
        c.store.set_meta("module", c.module)
        c.store.replace_snapshot(rows, host=c._host())
        counts: dict[str, int] = {}
        for resource_type, _, _ in rows:
            counts[resource_type] = counts.get(resource_type, 0) + 1
        c.store.log_op("refresh", result={"count": len(rows), "counts": counts})
        return {"last_synced": c.store.last_synced, "count": len(rows), "counts": counts}

    def drift(self) -> dict:
        c = self._c
        rows = c._audited("drift", None, None, None, c._pull)
        report = c.store.drift(rows, c.identity_fields)
        c.store.replace_snapshot(rows, host=c._host())
        c.store.log_op("drift", result=report)
        return {
            "module": c.module,
            "host": c._host(),
            "last_synced": c.store.last_synced,
            "count": (
                len(report["added"]) + len(report["removed"]) + len(report["changed"])
            ),
            **report,
        }

    def reset(self) -> dict:
        c = self._c
        c.close()
        base = str(c.config.db_path)
        for suffix in ("", "-wal", "-shm"):
            Path(base + suffix).unlink(missing_ok=True)
        return {"reset": base}

    def history(self, n: int = 20) -> dict:
        return {"ops": self._c.store.history(n)}


class BaseClient:
    """Base class for all module clients.

    Subclasses must set :attr:`spec` and implement :meth:`_open_transport`
    and :meth:`_pull`. Write verbs go through :meth:`_created`,
    :meth:`_updated`, :meth:`_deleted` and :meth:`_called`, which own the
    write-through + audit-log guarantee.
    """

    module: ClassVar[str] = "base"
    spec: ClassVar[ModuleSpec]
    #: dotted fields that count as real drift, per resource type (doc 02)
    identity_fields: ClassVar[dict[str, list[str]]] = {}
    #: seconds after which state-backed reads report ``stale: true``;
    #: overridable per instance by a host that knows its refresh cadence
    staleness_window: float = 30.0

    _registry: ClassVar[dict[tuple[type, tuple], "BaseClient"]] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()
    _sh_config: Config

    # ------------------------------------------------------------ singleton

    def __new__(cls, **kwargs: object) -> Self:
        cfg = resolve(cls.spec, **kwargs)
        key = (cls, cfg.fingerprint())
        with cls._registry_lock:
            existing = cls._registry.get(key)
            if existing is not None:
                return cast(Self, existing)
            self = super().__new__(cls)
            cls._registry[key] = self
            self._sh_config = cfg
            return self

    def __init__(self, **kwargs: object) -> None:
        # Re-runs when __new__ returned a cached instance: guard with flag.
        if getattr(self, "_sh_initialized", False):
            return
        self.config: Config = getattr(self, "_sh_config", resolve(self.spec, **kwargs))
        self._sh_initialized = True
        self._http: object = None
        self._state: StateStore | None = None

    @classmethod
    def reset(cls) -> None:
        """Forget all registered instances (used between tests)."""
        with cls._registry_lock:
            cls._registry.clear()

    # -------------------------------------------------------------- lazy

    def _ensure(self) -> None:
        """Open transport + state on first use. Idempotent."""
        if self._http is None and self._state is None:
            require(self.config, self.spec)
            self._state = StateStore(self.config.db_path)
            self._http = self._open_transport()
            self._on_connect()

    def _open_transport(self) -> object:
        """Module hook: create the remote transport (e.g. httpx.Client)."""
        raise NotImplementedError

    def _on_connect(self) -> None:
        """Module hook: run once after transport + state are open."""

    def connect(self) -> None:
        """Eagerly pay the first-use cost (CLIs call this for fast failure)."""
        self._ensure()

    def close(self) -> None:
        transport, self._http = self._http, None
        state, self._state = self._state, None
        if state is not None:
            state.close()
        close_transport = getattr(transport, "close", None)
        if callable(close_transport):
            close_transport()

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------- access

    @property
    def store(self) -> StateStore:
        """The raw StateStore. Modules expose the `state` *namespace*; this
        is the internal accessor (doc 03)."""
        self._ensure()
        assert self._state is not None
        return self._state

    @property
    def transport(self) -> object:
        self._ensure()
        return self._http

    @property
    def state(self) -> StateNamespace:
        return StateNamespace(self)

    # ------------------------------------------------------- module hooks

    def _pull(self) -> list[tuple[str, str, Mapping]]:
        """Module hook: every remote object as ``(resource_type, id, attrs)``."""
        raise NotImplementedError

    def _id_of(self, resource: str, obj: Mapping) -> str:
        """Module hook: the id of a remote object returned by a create."""
        return str(obj["id"])

    def _host(self) -> str | None:
        url = self.config.get("url")
        return str(url) if url is not None else None

    # ------------------------------------------------------- write-through

    def _audited(
        self,
        verb: str,
        resource: str | None,
        rid: str | None,
        args: Mapping | None,
        send: Callable[[], T],
    ) -> T:
        """Run ``send``; audit-log remote failures (``ok=False``) and re-raise."""
        store = self.store
        try:
            return send()
        except StudioHaysError as exc:
            store.log_op(verb, resource, rid, args=args, result=str(exc), ok=False)
            raise

    def _created(
        self,
        resource: str,
        desired: Mapping,
        send: Callable[[], dict],
        *,
        manage: bool = True,
    ) -> dict:
        """``send`` returns the normalized created object. It becomes managed
        unless ``manage=False`` (mirror-mode resources, D-007)."""
        obj = self._audited("create", resource, None, desired, send)
        self.store.record_write(
            "create", resource, self._id_of(resource, obj),
            obj=obj, desired=desired if manage else None, args=desired, result=obj,
        )
        return obj

    def _updated(
        self, resource: str, rid: str, fields: Mapping, send: Callable[[], dict]
    ) -> dict:
        obj = self._audited("update", resource, rid, fields, send)
        self.store.record_write(
            "update", resource, rid, obj=obj, desired=fields, args=fields, result=obj
        )
        return obj

    def _deleted(self, resource: str, rid: str, send: Callable[[], T]) -> T:
        result = self._audited("delete", resource, rid, None, send)
        self.store.record_write("delete", resource, rid, result=result)
        return result

    def _called(self, service: str, args: Mapping, send: Callable[[], T]) -> T:
        """A remote call with no state effect beyond the audit log."""
        logged = {"service": service, **args}
        result = self._audited("call", None, None, logged, send)
        self.store.log_op("call", args=logged, result=result)
        return result

    def _envelope(self, **payload: object) -> dict:
        """State-backed read envelope (doc 03): last_synced + stale + payload."""
        return {
            "last_synced": self.store.last_synced,
            "stale": self.store.is_stale(self.staleness_window),
            **payload,
        }
