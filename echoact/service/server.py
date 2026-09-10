"""Running the service inside the application process (A.2, F-52, F-79).

The listener is a worker thread of the GUI process, not a child process.
A.2 fixes that: the GUI must survive a failed bind and stay fully usable,
which means it cannot reach the engine over the network path -- if it did, a
port conflict would take the desktop application down with the service, and
F-79 says the opposite must happen.  So the engine, the store, and the
service all live in one process and the service is the only part that can
fail to start.

Two consequences follow, and both are load-bearing.

* **The socket is bound here, on the caller's thread.**  ``uvicorn.Server.run``
  binds inside the thread it runs on and exits the process on failure, so a
  conflict would surface as a dead thread rather than as an answer.  Binding
  first turns a busy port into ``EchoActError(SERVICE_PORT_UNAVAILABLE)``
  raised from :meth:`ServiceRunner.start`, which ``Application.start_service``
  turns into F-79's actionable notice while the GUI keeps running.
* **The host is never a parameter.**  N-17 says binding a non-loopback
  address is not configurable, so ``policy.REST_HOST`` is the only value that
  reaches ``bind``.  The owner chooses the port and nothing else.
"""

from __future__ import annotations

import socket
import sys
import threading
from typing import Any

import uvicorn

from ..domain import RequestPath
from ..errors import Code, EchoActError
from ..policy import REST_HOST
from ..util.logging import get_logger
from .app import create_app
from .deps import ServiceContext

log = get_logger("service.server")

#: How long :meth:`ServiceRunner.start` waits for uvicorn to report itself
#: started.  The socket is already bound and listening by then, so this only
#: covers the server's own set-up; a machine slow enough to exceed it would
#: have failed the bind first.
START_TIMEOUT_S = 10.0

#: How long :meth:`ServiceRunner.stop` waits for the serving thread to end.
#: Long enough for an in-flight audio stream to finish its current block and
#: short enough that closing the window is not held up by a client that has
#: stopped reading.
STOP_TIMEOUT_S = 5.0

#: The paths whose jobs belong to this integration.  MCP is a REST client
#: (F-58), so its jobs arrive by the REST path and are cancelled with it;
#: turning REST off is exactly what makes MCP unavailable.
_INTEGRATION_PATHS = frozenset({RequestPath.REST, RequestPath.MCP})


class ServiceRunner:
    """The local REST service's lifetime.

    One instance per start.  ``stop`` is final: uvicorn's ``Server`` is not
    restartable and neither is a closed socket, so re-enabling the service
    after the owner turned it off builds a new runner -- which is also what
    4.1 means by re-enabling being an explicit action.
    """

    def __init__(self, application: Any, *, port: int | None = None) -> None:
        self.application = application
        self.context = ServiceContext(application, port=port)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._stopped = threading.Event()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def host(self) -> str:
        """N-17: the loopback address.  Read from policy, never from settings."""
        return REST_HOST

    @property
    def port(self) -> int:
        return self.context.port

    @property
    def running(self) -> bool:
        server, thread = self._server, self._thread
        return bool(
            server is not None
            and thread is not None
            and thread.is_alive()
            and server.started
            and not self.context.draining
        )

    # ------------------------------------------------------------------
    # Start (F-46, F-79)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind, then serve on a worker thread.

        Raises ``EchoActError(SERVICE_PORT_UNAVAILABLE)`` with the port in its
        detail when the address is taken.  F-79 forbids binding an alternative
        port silently and forbids terminating whatever holds this one, so the
        failure is reported and nothing else is attempted.
        """
        if self._thread is not None:
            raise EchoActError(Code.INTERNAL, "This service runner has already been started.")
        port = self.port
        self._socket = _bind(port)
        app = create_app(self.context)
        config = uvicorn.Config(
            app,
            host=REST_HOST,
            port=port,
            log_level="warning",
            access_log=False,
            # The parts are built by ``Application`` before the service
            # starts; there is nothing for a lifespan event to do, and a
            # protocol that expects one would only add a way to fail.
            lifespan="off",
            # Bounded so that closing the window is not held up by a client
            # that stopped reading mid-stream; F-77 confirms the exit with the
            # user and then has to actually make it.
            timeout_graceful_shutdown=int(STOP_TIMEOUT_S),
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = _no_signal_handlers  # type: ignore[method-assign]
        self._server = server
        self._thread = threading.Thread(
            target=self._serve, name="echoact-rest", daemon=True
        )
        self._thread.start()

        if not _await_started(server, self._thread, START_TIMEOUT_S):
            self.stop()
            raise EchoActError(
                Code.SERVICE_PORT_UNAVAILABLE,
                "The local service did not finish starting.",
                detail={"host": REST_HOST, "port": port},
            )
        log.info("local service listening on %s:%d", REST_HOST, port)

    def _serve(self) -> None:
        server, sock = self._server, self._socket
        if server is None or sock is None:
            return
        try:
            server.run(sockets=[sock])
        except BaseException as exc:  # noqa: BLE001 - F-79: never take the GUI with it
            log.warning("local service stopped: %s", type(exc).__name__)
        finally:
            self._stopped.set()

    # ------------------------------------------------------------------
    # Stop (F-52)
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """F-52's three steps, in the order the requirement gives them.

        Block new requests, cancel this integration's in-progress jobs, then
        shut down.  The order matters: cancelling first would leave a window
        in which a client could take the slot straight back, and shutting the
        listener first would drop the connection of the very caller whose job
        is about to be cancelled without ever telling it why.

        A GUI job is untouched.  F-52 says so outright, and it is why the
        cancellation is filtered by request path rather than applied to
        whatever happens to be running.
        """
        self.context.drain()
        self._cancel_integration_jobs()

        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._stopped.wait(STOP_TIMEOUT_S)
            thread.join(STOP_TIMEOUT_S)
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self.context.close()

    def _cancel_integration_jobs(self) -> None:
        """Cancel what this integration started, and nothing else.

        Failures are logged rather than raised: this runs on the way out, and
        F-52 wants the servers cleaned up on exit even when a job is already
        in a state that cannot be cancelled.
        """
        engine = self.application.engine
        current = engine.current()
        if current is None or current.request_path not in _INTEGRATION_PATHS:
            return
        try:
            engine.cancel(current.job_id)
        except EchoActError as exc:
            log.warning("could not cancel %s on shutdown: %s", current.job_id, exc.code.value)


def _bind(port: int) -> socket.socket:
    """Take the loopback address and port, or say why not.

    ``SO_REUSEADDR`` is set only away from Windows.  On Windows it does not
    mean "reuse a socket in TIME_WAIT"; it means "bind even though another
    process already holds this address", which would let EchoAct silently
    take a port from -- or share it with -- whatever is listening there.  On a
    service whose entire security model is "loopback only, authenticated", a
    second listener on the same port is the one thing that must fail loudly.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((REST_HOST, port))
        sock.listen(128)
        sock.set_inheritable(True)
    except OSError as exc:
        sock.close()
        # F-79 distinguishes an occupied port from insufficient access
        # permission, so the two are not folded into one message even though
        # they share a code: the detail carries which errno it was.
        raise EchoActError(
            Code.SERVICE_PORT_UNAVAILABLE,
            f"Port {port} on {REST_HOST} is not available ({exc.strerror or type(exc).__name__}).",
            detail={"host": REST_HOST, "port": port, "errno": exc.errno},
            cause=exc,
        ) from exc
    return sock


def _await_started(server: uvicorn.Server, thread: threading.Thread, timeout_s: float) -> bool:
    """Wait for uvicorn to report itself started, or for the thread to die.

    Polled against an event that is never set, which is simply a sleep that a
    reader can see is bounded.  ``uvicorn.Server`` offers no "started" event
    to wait on, and the alternative -- ``time.sleep`` in a loop with no
    liveness check -- would hang for the whole timeout when the serving
    thread has already died.
    """
    idle = threading.Event()
    step = 0.01
    waited = 0.0
    while waited < timeout_s:
        if server.started:
            return True
        if not thread.is_alive():
            return False
        idle.wait(step)
        waited += step
    return bool(server.started)


def _no_signal_handlers() -> None:
    """Uvicorn installs SIGINT and SIGTERM handlers, and only the main thread
    may.  The application owns its own shutdown (F-77), so the server is told
    to install none rather than being run somewhere it could."""
    return None
