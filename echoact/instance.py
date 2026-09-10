"""One instance per user account, per F-85.

A second launch must surface the window that already exists rather than
start a second engine, a second database session, or a second REST
listener -- and above all it must never create a second generation slot,
which F-47 makes an app-wide singleton.

The mechanism is a lock file holding the first instance's activation port
and a random token, plus a loopback listener that accepts exactly one
message.  Two details are deliberate:

* The listener is not the REST service and shares nothing with it.  It
  binds an ephemeral port, speaks one word, and does nothing else, so a
  bind failure here cannot affect F-79's separate story about the REST
  port -- and the REST service being turned off does not stop a second
  launch from finding the first window.
* A stale lock file is taken over rather than treated as a running app.
  A forced termination leaves one behind, and refusing to start after a
  crash would be worse than the problem the lock solves.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .paths import ensure_tree, lock_path
from .util.logging import get_logger

log = get_logger("instance")

_ACTIVATE = "echoact-activate"
_CONNECT_TIMEOUT_S = 1.5


@dataclass(frozen=True, slots=True)
class LockInfo:
    pid: int
    port: int
    token: str


class AlreadyRunning(Exception):
    """Raised by :func:`acquire` when another instance answered."""


class InstanceLock:
    """Held for the life of the application process."""

    def __init__(self, on_activate: Callable[[], None] | None = None) -> None:
        self._on_activate = on_activate
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._token = secrets.token_urlsafe(24)
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        return self._server.getsockname()[1] if self._server else 0

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self._server.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, name="echoact-instance", daemon=True)
        self._thread.start()
        _write_lock(LockInfo(pid=os.getpid(), port=self.port, token=self._token))

    def release(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        try:
            path = lock_path()
            info = _read_lock()
            # Only remove our own lock: a lock written by a later instance
            # after we were killed is not ours to delete.
            if info is None or info.pid == os.getpid():
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set() and self._server is not None:
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(1.0)
                    payload = conn.recv(256).decode("utf-8", "replace").strip()
                except OSError:
                    continue
                want = f"{_ACTIVATE} {self._token}"
                if payload != want:
                    # Not our second launch.  Say nothing: an unauthenticated
                    # caller learns neither that this is EchoAct nor why it
                    # was refused.
                    continue
                try:
                    conn.sendall(b"ok\n")
                except OSError:
                    pass
                if self._on_activate:
                    try:
                        self._on_activate()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("activation handler failed: %s", type(exc).__name__)


def _write_lock(info: LockInfo) -> None:
    ensure_tree()
    path = lock_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"pid": info.pid, "port": info.port, "token": info.token}),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _read_lock() -> LockInfo | None:
    try:
        data = json.loads(lock_path().read_text(encoding="utf-8"))
        return LockInfo(pid=int(data["pid"]), port=int(data["port"]), token=str(data["token"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def notify_existing() -> bool:
    """Ask a running instance to show itself.  True if one answered.

    A lock file whose listener does not answer is stale -- the usual cause
    is the forced termination F-45 already has to recover from -- so the
    caller starts normally and overwrites it.
    """
    info = _read_lock()
    if info is None or info.port <= 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", info.port), _CONNECT_TIMEOUT_S) as s:
            s.settimeout(_CONNECT_TIMEOUT_S)
            s.sendall(f"{_ACTIVATE} {info.token}".encode())
            return s.recv(16).strip() == b"ok"
    except OSError:
        return False


def acquire(on_activate: Callable[[], None] | None = None) -> InstanceLock:
    """Become the single instance, or raise :class:`AlreadyRunning`."""
    if notify_existing():
        raise AlreadyRunning
    lock = InstanceLock(on_activate)
    lock.start()
    return lock
