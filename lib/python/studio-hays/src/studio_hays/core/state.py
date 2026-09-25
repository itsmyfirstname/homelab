"""StateStore: the local SQLite state of a module (doc 02).

Every module database has the same four tables:

- ``meta``     — module name, schema version, host, last_synced
- ``snapshot`` — mirror layer: verbatim remote objects, tombstoned deletes
- ``managed``  — optional layer: desired attributes for owned resources
- ``ops``      — audit log of every operation the client performed

Guarantees (doc 01):

- stdlib ``sqlite3``, WAL mode, ``PRAGMA foreign_keys=ON``
- one connection per process, guarded by a lock (single-user assumption)
- ``replace_snapshot`` is one atomic transaction (D-010)
- the DB is disposable: the store never assumes the file pre-exists
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = "1"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _field(obj: Mapping, path: str) -> object:
    """Resolve a dotted path like ``attributes.friendly_name``."""
    cur: object = obj
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot (
  id            TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  attributes    TEXT NOT NULL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  deleted_at    TEXT,
  PRIMARY KEY (resource_type, id)
);

CREATE TABLE IF NOT EXISTS managed (
  id            TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  desired       TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'ok',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (resource_type, id),
  FOREIGN KEY (resource_type, id) REFERENCES snapshot (resource_type, id)
);

CREATE TABLE IF NOT EXISTS ops (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  op            TEXT NOT NULL,
  resource_type TEXT,
  id            TEXT,
  args          TEXT,
  result        TEXT,
  ok            INTEGER NOT NULL
);
"""


class StateStore:
    """Owns one module's SQLite file. All methods are thread-safe."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self.set_meta("schema_version", SCHEMA_VERSION)

    # ------------------------------------------------------------------ meta

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def meta_all(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    @property
    def last_synced(self) -> str | None:
        return self.get_meta("last_synced")

    def is_stale(self, window_seconds: float) -> bool:
        """True when we have no data, or the last refresh is too old."""
        synced = self.last_synced
        if not synced:
            return True
        age = datetime.now(UTC) - datetime.fromisoformat(synced)
        return age.total_seconds() > window_seconds

    # ------------------------------------------------------------- snapshot

    def replace_snapshot(
        self,
        rows: Iterable[tuple[str, str, Mapping]],
        host: str | None = None,
    ) -> None:
        """Full atomic replace (D-010): one transaction, self-healing.

        ``rows`` is ``(resource_type, id, attributes)``. ``first_seen`` of
        surviving rows is preserved.
        """
        now = _now()
        new_keys = {(rt, rid) for rt, rid, _ in rows}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                old: dict[tuple[str, str], str] = {
                    (r["resource_type"], r["id"]): r["first_seen"]
                    for r in self._conn.execute(
                        "SELECT resource_type, id, first_seen FROM snapshot"
                    )
                }
                # Managed rows reference snapshot (FK): drop desires for
                # objects that vanished from the remote, keep the rest.
                kept_managed = [
                    (m["id"], m["resource_type"], m["desired"], m["status"],
                     m["created_at"], m["updated_at"])
                    for m in self._conn.execute(
                        "SELECT * FROM managed"
                    )
                    if (m["resource_type"], m["id"]) in new_keys
                ]
                # Order matters: managed references snapshot (FK, checked
                # per statement), so clear managed, clear snapshot, insert
                # snapshot, then re-insert the surviving managed rows.
                self._conn.execute("DELETE FROM managed")
                self._conn.execute("DELETE FROM snapshot")
                for resource_type, rid, attributes in rows:
                    self._conn.execute(
                        "INSERT INTO snapshot "
                        "(id, resource_type, attributes, first_seen, last_seen, deleted_at) "
                        "VALUES (?, ?, ?, ?, ?, NULL)",
                        (
                            rid,
                            resource_type,
                            json.dumps(attributes),
                            old.get((resource_type, rid), now),
                            now,
                        ),
                    )
                self._conn.executemany(
                    "INSERT INTO managed "
                    "(id, resource_type, desired, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    kept_managed,
                )
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('last_synced', ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (now,),
                )
                if host is not None:
                    self._conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('host', ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (host,),
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def get_snapshot(
        self,
        resource_type: str | None = None,
        rid: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict]:
        """Snapshot rows; tombstoned (deleted) rows are hidden by default."""
        sql = "SELECT * FROM snapshot"
        clauses: list[str] = []
        args: list[str] = []
        if resource_type is not None:
            clauses.append("resource_type = ?")
            args.append(resource_type)
        if rid is not None:
            clauses.append("id = ?")
            args.append(rid)
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY resource_type, id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def snapshot_ids(self, resource_type: str | None = None) -> set[str]:
        sql = "SELECT id FROM snapshot"
        args: list[str] = []
        if resource_type is not None:
            sql += " WHERE resource_type = ?"
            args.append(resource_type)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return {r["id"] for r in rows}

    def _row_to_snapshot(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "resource_type": row["resource_type"],
            "attributes": json.loads(row["attributes"]),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "deleted_at": row["deleted_at"],
        }

    # -------------------------------------------------------------- managed

    def upsert_managed(
        self,
        resource_type: str,
        rid: str,
        desired: Mapping,
        status: str = "ok",
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO managed "
                "(id, resource_type, desired, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (resource_type, id) DO UPDATE SET "
                "desired = excluded.desired, "
                "status = excluded.status, "
                "updated_at = excluded.updated_at",
                (rid, resource_type, json.dumps(desired), status, now, now),
            )
            self._conn.commit()

    def get_managed(self, resource_type: str | None = None) -> list[dict]:
        sql = "SELECT * FROM managed"
        args: list[str] = []
        if resource_type is not None:
            sql += " WHERE resource_type = ?"
            args.append(resource_type)
        sql += " ORDER BY resource_type, id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            {
                "id": r["id"],
                "resource_type": r["resource_type"],
                "desired": json.loads(r["desired"]),
                "status": r["status"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def set_managed_status(
        self, resource_type: str, rid: str, status: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE managed SET status = ?, updated_at = ? "
                "WHERE resource_type = ? AND id = ?",
                (status, _now(), resource_type, rid),
            )
            self._conn.commit()

    # --------------------------------------------------------- write-through

    def record_write(
        self,
        verb: str,
        resource_type: str,
        rid: str,
        *,
        obj: Mapping | None = None,
        desired: Mapping | None = None,
        args: Mapping | None = None,
        result: object = None,
    ) -> None:
        """Apply a successful remote write to local state + audit log, atomically.

        - ``create``: upsert snapshot ``obj``; record ``desired`` as managed
        - ``update``: upsert snapshot ``obj``; merge ``desired`` into the
          managed row if the resource is managed
        - ``delete``: tombstone the snapshot row; drop the managed row
        """
        if verb not in ("create", "update", "delete"):
            raise ValueError(f"record_write: unknown verb {verb!r}")
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if verb in ("create", "update"):
                    assert obj is not None
                    self._conn.execute(
                        "INSERT INTO snapshot "
                        "(id, resource_type, attributes, first_seen, last_seen, deleted_at) "
                        "VALUES (?, ?, ?, ?, ?, NULL) "
                        "ON CONFLICT (resource_type, id) DO UPDATE SET "
                        "attributes = excluded.attributes, "
                        "last_seen = excluded.last_seen, "
                        "deleted_at = NULL",
                        (rid, resource_type, json.dumps(obj), now, now),
                    )
                    self._conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('last_synced', ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (now,),
                    )
                if verb == "create" and desired is not None:
                    self._conn.execute(
                        "INSERT INTO managed "
                        "(id, resource_type, desired, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'ok', ?, ?) "
                        "ON CONFLICT (resource_type, id) DO UPDATE SET "
                        "desired = excluded.desired, status = 'ok', "
                        "updated_at = excluded.updated_at",
                        (rid, resource_type, json.dumps(desired), now, now),
                    )
                elif verb == "update" and desired:
                    row = self._conn.execute(
                        "SELECT desired FROM managed WHERE resource_type = ? AND id = ?",
                        (resource_type, rid),
                    ).fetchone()
                    if row is not None:
                        merged = {**json.loads(row["desired"]), **desired}
                        self._conn.execute(
                            "UPDATE managed SET desired = ?, status = 'ok', updated_at = ? "
                            "WHERE resource_type = ? AND id = ?",
                            (json.dumps(merged), now, resource_type, rid),
                        )
                elif verb == "delete":
                    self._conn.execute(
                        "DELETE FROM managed WHERE resource_type = ? AND id = ?",
                        (resource_type, rid),
                    )
                    self._conn.execute(
                        "UPDATE snapshot SET deleted_at = ?, last_seen = ? "
                        "WHERE resource_type = ? AND id = ?",
                        (now, now, resource_type, rid),
                    )
                self._insert_op(verb, resource_type, rid, args, result, True, now)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------ ops

    def _insert_op(
        self,
        op: str,
        resource_type: str | None,
        rid: str | None,
        args: Mapping | None,
        result: object,
        ok: bool,
        ts: str,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO ops (ts, op, resource_type, id, args, result, ok) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                op,
                resource_type,
                rid,
                json.dumps(args) if args is not None else None,
                json.dumps(result) if result is not None else None,
                int(ok),
            ),
        )
        return cur.lastrowid or 0

    def log_op(
        self,
        op: str,
        resource_type: str | None = None,
        rid: str | None = None,
        args: Mapping | None = None,
        result: object = None,
        ok: bool = True,
    ) -> int:
        with self._lock:
            seq = self._insert_op(op, resource_type, rid, args, result, ok, _now())
            self._conn.commit()
            return seq

    def history(self, n: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ops ORDER BY seq DESC LIMIT ?", (n,)
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "ts": r["ts"],
                "op": r["op"],
                "resource_type": r["resource_type"],
                "id": r["id"],
                "args": json.loads(r["args"]) if r["args"] else None,
                "result": json.loads(r["result"]) if r["result"] else None,
                "ok": bool(r["ok"]),
            }
            for r in rows
        ]

    # ----------------------------------------------------------------- drift

    def drift(
        self,
        new_rows: Iterable[tuple[str, str, Mapping]],
        identity_fields: Mapping[str, Iterable[str]] | None = None,
    ) -> dict:
        """Compare the stored snapshot against a fresh pull (doc 02).

        ``identity_fields`` maps resource type to dotted field paths that
        count as real drift; volatile fields are ignored. Without it, any
        attribute difference counts as ``changed`` (with no diff detail).
        The stored snapshot is not mutated.
        """
        new_rows = list(new_rows)
        old = {
            (r["resource_type"], r["id"]): r["attributes"]
            for r in self.get_snapshot(include_deleted=True)
            if r["deleted_at"] is None
        }
        new = {
            (resource_type, rid): attributes
            for resource_type, rid, attributes in new_rows
        }

        added = [
            {"type": rt, "id": rid}
            for (rt, rid) in sorted(new)
            if (rt, rid) not in old
        ]
        removed = [
            {"type": rt, "id": rid}
            for (rt, rid) in sorted(old)
            if (rt, rid) not in new
        ]
        changed: list[dict] = []
        for (rt, rid) in sorted(old.keys() & new.keys()):
            old_attrs, new_attrs = old[(rt, rid)], new[(rt, rid)]
            fields = list((identity_fields or {}).get(rt, []))
            if fields:
                diff = {}
                for path in fields:
                    was, is_now = _field(old_attrs, path), _field(new_attrs, path)
                    if was != is_now:
                        diff[path] = {"was": was, "is": is_now}
                if diff:
                    changed.append({"type": rt, "id": rid, "diff": diff})
            elif old_attrs != new_attrs:
                changed.append({"type": rt, "id": rid, "diff": {}})
        return {"added": added, "removed": removed, "changed": changed}

    # ------------------------------------------------------------------ misc

    def close(self) -> None:
        with self._lock:
            self._conn.close()
