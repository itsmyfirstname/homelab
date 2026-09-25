"""Home Assistant WebSocket API: one short-lived, authenticated session per
batch of commands (connect → auth → commands → close). Needed because the
entity/device/area/label/floor registries aren't reachable over REST.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

from ..core import NotFoundError, RemoteError

TOKEN_REJECTED = (
    "Home Assistant rejected the access token (WebSocket auth_invalid) — "
    "check STUDIOHAYS_HA_TOKEN or token="
)


def ws_url(http_url: str) -> str:
    scheme, _, rest = http_url.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest.rstrip('/')}/api/websocket"


class Session:
    """An authenticated connection; :meth:`call` runs one command."""

    def __init__(self, conn: ClientConnection, timeout: float) -> None:
        self._conn = conn
        self._timeout = timeout
        self._last_id = 0

    def call(self, kind: str, **payload: Any) -> Any:
        self._last_id += 1
        msg_id = self._last_id
        self._conn.send(json.dumps({"id": msg_id, "type": kind, **payload}))
        while True:
            msg = json.loads(self._conn.recv(timeout=self._timeout))
            if msg.get("id") == msg_id and msg.get("type") == "result":
                break
        if msg.get("success"):
            return msg.get("result")
        error = msg.get("error") or {}
        code, message = error.get("code", "unknown_error"), error.get("message", "")
        exc = NotFoundError if code == "not_found" else RemoteError
        raise exc(f"{kind} failed: {code}: {message}")


@contextmanager
def session(url: str, token: str, timeout: float) -> Iterator[Session]:
    stack = ExitStack()
    try:
        conn: ClientConnection = stack.enter_context(
            connect(url, open_timeout=timeout, max_size=None)
        )
    except (OSError, TimeoutError, WebSocketException) as exc:
        raise RemoteError(f"cannot reach Home Assistant WebSocket API at {url}: {exc}") from exc
    with stack:
        yield from _authenticated(conn, url, token, timeout)


def _authenticated(
    conn: ClientConnection, url: str, token: str, timeout: float
) -> Iterator[Session]:
    try:
        hello = json.loads(conn.recv(timeout=timeout))
        if hello.get("type") != "auth_required":
            raise RemoteError(f"unexpected WebSocket greeting from {url}: {hello.get('type')!r}")
        conn.send(json.dumps({"type": "auth", "access_token": token}))
        reply = json.loads(conn.recv(timeout=timeout))
        if reply.get("type") == "auth_invalid":
            raise RemoteError(TOKEN_REJECTED)
        if reply.get("type") != "auth_ok":
            raise RemoteError(f"unexpected WebSocket auth reply from {url}: {reply.get('type')!r}")
        yield Session(conn, timeout)
    except (ConnectionClosed, TimeoutError, OSError) as exc:
        raise RemoteError(f"Home Assistant WebSocket connection to {url} failed: {exc}") from exc
    except ValueError as exc:  # a non-JSON frame
        raise RemoteError(f"Home Assistant sent an invalid WebSocket message on {url}: {exc}") from exc
