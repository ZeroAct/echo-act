"""The parent side of the synthesis worker.

Nothing else in the application talks to the worker process.  That is the
point of the module: A.2 puts synthesis in a child so N-03 can enforce
limits on it and N-21 can measure it apart from the GUI, the database, and
the service, and a single owner of the pipe is what keeps the seq discipline
in ``protocol`` meaningful.

Three rules shape the code, all of them from requirements rather than taste.

*Stale replies are dropped.*  Every request carries an increasing ``seq``.
When a caller stops waiting -- a timeout, a cancelled job -- the pending
entry is removed, and a reply arriving afterwards matches nothing and is
discarded.  It is never handed to whatever request ran next, because the
audio it describes belongs to text the caller has already abandoned.

*A worker that dies unexpectedly makes this object unusable.*  The pending
request gets ``Code.WORKER_LOST`` and every later call raises it too, until
someone calls :meth:`kill` to tear the wreckage down deliberately.  A silent
restart would look like a recovery while having lost the loaded model and
every remaining segment of the job -- and whether to restart is the job
engine's decision under F-19, not this object's.

*Release is bounded.*  :meth:`kill` returns inside N-22's five seconds: ask
with ``Shutdown``, wait ``WORKER_TERMINATE_GRACE_S``, terminate, then close
the container, which on Windows kills anything still breathing because the
Job Object was created with ``KILL_ON_JOB_CLOSE``.  Each step is measured
against one deadline rather than being given its own timeout, so the sum
cannot drift past the promise.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Final

from ..domain import Budget
from ..errors import Code, EchoActError
from ..policy import (
    BOUNDED_WAIT_CEILING_S,
    BOUNDED_WAIT_DEFAULT_S,
    ENGINE_TOTAL_STEPS,
    WORKER_RELEASE_DEADLINE_S,
    WORKER_TERMINATE_GRACE_S,
)
from ..util.ids import monotonic
from ..util.logging import get_logger
from . import protocol
from .container import ContainerLimits, ResourceContainer, make_container

log = get_logger("engine.supervisor")

#: The worker, as a module so that the child inherits nothing but the
#: interpreter -- no script path to get wrong when the app is packaged.
WORKER_MODULE: Final = "echoact.engine.worker"

# Section 4 fixes no timeout for an individual engine request, so these
# borrow the two waits it does fix rather than inventing numbers here.  A
# request that blows its deadline is treated as a dead worker, which is the
# only honest reading: the protocol has one outstanding request at a time,
# so a worker that has not answered is a worker that cannot be asked
# anything else either.
START_TIMEOUT_S: Final = BOUNDED_WAIT_DEFAULT_S
LOAD_TIMEOUT_S: Final = BOUNDED_WAIT_CEILING_S
SYNTHESIZE_TIMEOUT_S: Final = BOUNDED_WAIT_CEILING_S
PING_TIMEOUT_S: Final = BOUNDED_WAIT_DEFAULT_S

#: F-87's allow-list, as the supervisor's default.  The check that matters
#: happens inside the worker, against the session it actually constructed --
#: this is only what the parent asks for, and a caller with a different
#: policy passes its own.
DEFAULT_ALLOWED_PROVIDERS: Final = ("CPUExecutionProvider",)

#: A worker error message is quoted back to the user but never logged and
#: never trusted to be short; N-20 keeps body text out of logs and this keeps
#: an engine traceback from smuggling any in.
_MAX_WORKER_MESSAGE = 200

_STDERR_KEEP = 20


class WorkerState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    #: Died without being asked to.  Unusable until :meth:`kill`.
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """What the worker reported after F-09's preparation succeeded."""

    model_id: str
    model_dir: str
    sample_rate: int
    voices: tuple[str, ...]
    providers: tuple[str, ...]
    load_seconds: float


@dataclass(frozen=True, slots=True)
class WorkerUsage:
    """F-22's figures for the generation job alone (N-21).

    ``source`` says where they came from: the kernel's own accounting for the
    container, or the worker's self-report.  They are not interchangeable --
    the container counts the whole worker tree whether or not the worker is
    answering -- so the distinction is reported rather than smoothed over.
    """

    rss_bytes: int
    cpu_percent: float
    peak_rss_bytes: int
    peak_commit_bytes: int
    limits: ContainerLimits | None
    source: str
    age_s: float


class _Waiter:
    """One in-flight request's mailbox."""

    __slots__ = ("event", "reply", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply: Any = None
        self.error: EchoActError | None = None

    def deliver(self, reply: Any) -> None:
        self.reply = reply
        self.event.set()

    def fail(self, error: EchoActError) -> None:
        self.error = error
        self.event.set()


class WorkerSupervisor:
    """Owns one worker process and its resource container.

    Warm reuse (F-17, F-18) lives here: the worker and its loaded model
    survive between jobs, and :meth:`would_need_load` answers whether the
    next job pays for a load.  Note what that method does *not* take -- voice,
    language, style, tempo.  Those are arguments to :meth:`synthesize`, so
    F-18's "changing only these keeps the model" is true by construction
    rather than by a comparison someone has to remember to write.  What does
    release the model is a different model id or a different budget (F-19):
    the budget is baked into the container at creation, so changing it means
    a new container and therefore a new worker.
    """

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        container_factory: Callable[[Budget], ResourceContainer] = make_container,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self._command: tuple[str, ...] = tuple(
            command if command is not None else (sys.executable, "-m", WORKER_MODULE)
        )
        self._container_factory = container_factory
        self._env_overrides = dict(env or {})
        self._cwd = str(cwd) if cwd is not None else None
        self._start_timeout_s = start_timeout_s

        # Held for the whole of one request/reply round trip: the protocol
        # allows exactly one outstanding request.  ``kill`` deliberately does
        # not take it, so a stuck synthesis can still be released in time.
        self._call_lock = threading.Lock()
        # Short critical sections only: process handles, pending table,
        # liveness.  Never held while waiting on a reply.
        self._state_lock = threading.RLock()

        self._counter = protocol.Counter()
        self._pending: dict[int, _Waiter] = {}
        self._state = WorkerState.STOPPED
        self._lost_error: EchoActError | None = None
        self._stopping = False

        self._proc: subprocess.Popen[bytes] | None = None
        self._container: ResourceContainer | None = None
        self._budget: Budget | None = None
        self._loaded: LoadedModel | None = None
        self._stdin: TextIOWrapper | None = None
        self._stdout: TextIOWrapper | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_KEEP)
        self._ready = threading.Event()
        self._worker_pid: int | None = None
        self._last_report: tuple[int, float, float] | None = None
        self._dropped_replies = 0

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> WorkerState:
        with self._state_lock:
            return self._state

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._state is WorkerState.RUNNING and self._proc is not None

    @property
    def is_usable(self) -> bool:
        """False after an unexpected death, until :meth:`kill` clears it."""
        return self.state is not WorkerState.LOST

    @property
    def loaded_model(self) -> LoadedModel | None:
        with self._state_lock:
            return self._loaded

    @property
    def budget(self) -> Budget | None:
        with self._state_lock:
            return self._budget

    @property
    def worker_pid(self) -> int | None:
        return self._worker_pid

    @property
    def limits(self) -> ContainerLimits | None:
        """What the container enforces, for F-22's display and N-03's
        distinction between an enforced ceiling and a monitored one."""
        c = self._container
        return c.limits if c is not None else None

    @property
    def dropped_replies(self) -> int:
        """Replies discarded because their request had been abandoned."""
        return self._dropped_replies

    @property
    def last_worker_report(self) -> tuple[int, float, float] | None:
        """The worker's own last ``(rss_bytes, cpu_percent, monotonic)``.

        Kept separate from :meth:`usage` because it is a different
        measurement: the worker reports what it can see of itself, while the
        container reports what the kernel accounts to the whole job.
        """
        return self._last_report

    @property
    def memory_exceeded(self) -> bool:
        """F-23, on a platform where the ceiling is polled rather than
        enforced: the container saw the budget exceeded and halted the job."""
        c = self._container
        return c is not None and c.memory_exceeded

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """The worker's last diagnostic lines, for a failure report."""
        return tuple(self._stderr_tail)

    def would_need_load(self, model_id: str, budget: Budget) -> bool:
        """F-17/F-18/F-19, as one question the job engine can ask up front.

        True means the next job pays a model load.  It is false only when the
        worker is alive, holds this exact model, and runs under this exact
        budget.
        """
        with self._state_lock:
            return not (
                self._state is WorkerState.RUNNING
                and self._budget == budget
                and self._loaded is not None
                and self._loaded.model_id == model_id
            )

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def load(
        self,
        model_id: str,
        model_dir: str | Path,
        budget: Budget,
        *,
        allowed_providers: Sequence[str] | None = None,
        timeout: float = LOAD_TIMEOUT_S,
    ) -> LoadedModel:
        """Prepare a model, starting or replacing the worker as F-19 requires.

        The parent resolves ``model_dir`` against the F-84 manifest before
        calling; the worker never searches for weights, so a bad path is a
        programming error here rather than a search there.
        """
        self._require_usable()
        directory = str(model_dir)
        with self._call_lock:
            with self._state_lock:
                running = self._state is WorkerState.RUNNING
                same_budget = self._budget == budget
                loaded = self._loaded
            if running and not same_budget:
                # F-19: a new resource limit releases the model.  The limit
                # lives in the container, and a container's limits are fixed
                # when it is created, so the worker goes with it.
                log.info("budget changed; replacing the worker to apply it")
                self.kill()
                running = False
                loaded = None
            if not running:
                self._spawn(budget)
                loaded = None
            elif loaded is not None and (loaded.model_id, loaded.model_dir) == (
                model_id,
                directory,
            ):
                # F-17: warm reuse.  The directory is compared as well as the
                # id, so a repaired or relocated model (F-65) is reloaded
                # rather than silently answered from the old weights.
                return loaded
            elif loaded is not None:
                self._send_unload()

            reply = self._call(
                protocol.Load(
                    model_id=model_id,
                    model_dir=directory,
                    intra_op_threads=budget.intra_op_threads,
                    inter_op_threads=budget.inter_op_threads,
                    allowed_providers=list(allowed_providers or DEFAULT_ALLOWED_PROVIDERS),
                ),
                timeout=timeout,
                kill_on_timeout=True,
            )
            if not isinstance(reply, protocol.Loaded):
                raise self._unexpected(reply)
            model = LoadedModel(
                model_id=reply.model_id,
                model_dir=directory,
                sample_rate=reply.sample_rate,
                voices=tuple(reply.voices),
                providers=tuple(reply.providers),
                load_seconds=reply.load_seconds,
            )
            with self._state_lock:
                self._loaded = model
            log.info(
                "model loaded model=%s rate=%d voices=%d in %.2fs",
                model.model_id,
                model.sample_rate,
                len(model.voices),
                model.load_seconds,
            )
            return model

    def synthesize(
        self,
        *,
        job_id: str,
        segment_index: int,
        text: str,
        lang: str,
        voice_id: str,
        speed: float,
        out_path: str | Path,
        total_steps: int = ENGINE_TOTAL_STEPS,
        timeout: float = SYNTHESIZE_TIMEOUT_S,
    ) -> protocol.Audio:
        """Render one segment and return the worker's ``Audio`` reply.

        The reply is the parent's evidence that the file at ``out_path`` is
        complete: the worker sends it only after the bytes are flushed and
        closed, so a killed worker leaves a file the parent already knows to
        discard rather than a truncated one it might use.
        """
        self._require_usable()
        with self._call_lock:
            with self._state_lock:
                if self._state is not WorkerState.RUNNING or self._loaded is None:
                    raise EchoActError(
                        Code.MODEL_NOT_READY,
                        "No model is loaded in the synthesis worker.",
                    )
            reply = self._call(
                protocol.Synthesize(
                    job_id=job_id,
                    segment_index=segment_index,
                    text=text,
                    lang=lang,
                    voice_id=voice_id,
                    speed=speed,
                    total_steps=total_steps,
                    out_path=str(out_path),
                ),
                timeout=timeout,
                kill_on_timeout=True,
            )
            if not isinstance(reply, protocol.Audio):
                raise self._unexpected(reply)
            return reply

    def unload(self, *, timeout: float = PING_TIMEOUT_S) -> None:
        """F-19's explicit release, keeping the worker warm.

        ``Unload`` has no reply of its own in the protocol, so the round trip
        is closed with a ``Ping``: stdin is ordered, and a ``Pong`` can only
        arrive after the unload ahead of it was handled.  Inventing an
        acknowledgement the protocol does not define would be worse -- the
        worker is written against the same file this is.
        """
        with self._call_lock:
            if not self.is_running:
                with self._state_lock:
                    self._loaded = None
                return
            self._send_unload()
            with self._state_lock:
                self._loaded = None
            self._call(protocol.Ping(), timeout=timeout, kill_on_timeout=False)

    def ping(self, *, timeout: float = PING_TIMEOUT_S) -> protocol.Pong:
        """Liveness, and a resource sample on the way back.

        It queues behind whatever request is in flight, because the protocol
        allows one at a time.  That is why F-22's display uses :meth:`usage`,
        which reads the container and never waits on the worker at all.
        """
        self._require_usable()
        with self._call_lock:
            reply = self._call(protocol.Ping(), timeout=timeout, kill_on_timeout=False)
            if not isinstance(reply, protocol.Pong):
                raise self._unexpected(reply)
            return reply

    # ------------------------------------------------------------------
    # Resource reporting
    # ------------------------------------------------------------------

    def usage(self) -> WorkerUsage | None:
        """The latest RSS and CPU for the generation job (F-22, N-21).

        Prefers the container's sample: on Windows that is Job Object
        accounting, which counts the worker and only the worker even while
        it is too busy to answer a ``Ping``.  The worker's own ``Stats`` is
        the fallback, and the result says which was used.
        """
        container = self._container
        sample = container.usage() if container is not None else None
        limits = container.limits if container is not None else None
        now = monotonic()
        if sample is not None:
            return WorkerUsage(
                rss_bytes=sample.rss_bytes,
                cpu_percent=sample.cpu_percent,
                peak_rss_bytes=sample.peak_rss_bytes,
                peak_commit_bytes=sample.peak_commit_bytes,
                limits=limits,
                source=sample.source,
                age_s=max(0.0, now - sample.sampled_at),
            )
        report = self._last_report
        if report is None:
            return None
        rss, cpu, at = report
        return WorkerUsage(
            rss_bytes=rss,
            cpu_percent=cpu,
            peak_rss_bytes=rss,
            peak_commit_bytes=0,
            limits=limits,
            source="worker report",
            age_s=max(0.0, now - at),
        )

    def sample_usage(self) -> WorkerUsage | None:
        """Force a fresh container sample, then report.  Used by the display
        refresh; the periodic sampling happens in the container regardless."""
        container = self._container
        if container is not None:
            container.sample()
        return self.usage()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def kill(self) -> float:
        """Release everything, within N-22's five seconds.  Returns elapsed.

        Safe at any instant, including mid-segment: the worker holds no
        durable state, and the parent named the output file before asking for
        it, so an interrupted write leaves a file the caller already knows to
        discard.
        """
        started = monotonic()
        deadline = started + WORKER_RELEASE_DEADLINE_S

        def remaining(reserve: float = 0.0) -> float:
            return max(0.0, deadline - monotonic() - reserve)

        with self._state_lock:
            proc = self._proc
            container = self._container
            stdin = self._stdin
            self._stopping = True
            self._loaded = None
            self._budget = None

        if proc is not None and proc.poll() is None and stdin is not None:
            try:
                protocol.write_message(stdin, protocol.Shutdown(seq=self._counter.next()))
            except (OSError, ValueError):
                pass
        self._close_stream(stdin)

        if proc is not None:
            # Reserve time for the terminate-then-close steps below, so a
            # worker that ignores Shutdown cannot eat the whole budget here.
            self._wait(proc, min(WORKER_TERMINATE_GRACE_S, remaining(reserve=1.5)))
            if proc.poll() is None:
                log.info("worker did not exit on request; terminating")
                try:
                    proc.terminate()
                except OSError:  # pragma: no cover - already gone
                    pass
                self._wait(proc, remaining(reserve=1.0))

        if container is not None:
            # The last resort, and the one that cannot be refused: closing a
            # Job Object created with KILL_ON_JOB_CLOSE kills every process
            # still inside it.
            container.close()
        if proc is not None and proc.poll() is None:
            self._wait(proc, remaining())

        # The reader is joined before its stream is closed: the child is gone
        # by now, so it is sitting on an EOF, and closing a pipe underneath a
        # blocked read is how a teardown turns into a hang.
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=remaining())
        stderr_reader = self._stderr_reader
        if stderr_reader is not None and stderr_reader is not threading.current_thread():
            stderr_reader.join(timeout=min(0.5, remaining()))
        self._teardown_streams()

        with self._state_lock:
            self._proc = None
            self._container = None
            self._reader = None
            self._stderr_reader = None
            self._worker_pid = None
            self._state = WorkerState.STOPPED
            self._lost_error = None
            self._stopping = False
            self._ready.clear()

        self._fail_pending(
            EchoActError(
                Code.SHUTTING_DOWN,
                "The synthesis worker was stopped before this request finished.",
            )
        )
        elapsed = monotonic() - started
        log.info("worker released in %.2fs", elapsed)
        return elapsed

    def __enter__(self) -> WorkerSupervisor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.kill()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_usable(self) -> None:
        with self._state_lock:
            if self._state is WorkerState.LOST:
                raise self._lost_error or EchoActError(Code.WORKER_LOST)

    def _spawn(self, budget: Budget) -> None:
        container = self._container_factory(budget)
        self._ready.clear()
        self._stderr_tail.clear()
        try:
            proc = subprocess.Popen(
                list(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
                env=self._child_env(),
                close_fds=True,
                **container.popen_kwargs(),
            )
        except OSError as exc:
            container.close()
            raise EchoActError(
                Code.INTERNAL, "The synthesis worker could not be started.", cause=exc
            ) from exc

        try:
            container.adopt(proc.pid)
            container.start_child(proc.pid)
        except Exception as exc:
            # N-03 is a guarantee, not a preference: a worker that could not
            # be placed under its limits does not get to run.
            _force_kill(proc)
            container.close()
            raise EchoActError(
                Code.INTERNAL,
                "The resource container could not be applied to the synthesis worker.",
                cause=exc,
            ) from exc

        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        # newline="\n" on the way out: the protocol frames on a single \n and
        # the platform's line ending has no business in it.
        stdin = TextIOWrapper(proc.stdin, encoding="utf-8", newline="\n", write_through=True)
        stdout = TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")
        stderr = TextIOWrapper(proc.stderr, encoding="utf-8", errors="replace")

        with self._state_lock:
            self._proc = proc
            self._container = container
            self._budget = budget
            self._stdin = stdin
            self._stdout = stdout
            self._worker_pid = proc.pid
            self._state = WorkerState.RUNNING
            self._lost_error = None
            self._stopping = False
            self._loaded = None

        self._reader = threading.Thread(
            target=self._read_loop, args=(proc, stdout), name="echoact-worker-reader", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, args=(stderr,), name="echoact-worker-stderr", daemon=True
        )
        self._stderr_reader.start()

        if not self._ready.wait(self._start_timeout_s):
            self.kill()
            raise EchoActError(
                Code.WORKER_LOST,
                "The synthesis worker did not start within its deadline.",
            )
        with self._state_lock:
            if self._state is WorkerState.LOST:
                err = self._lost_error or EchoActError(Code.WORKER_LOST)
            else:
                err = None
        if err is not None:
            self.kill()
            raise err
        log.info("worker started pid=%s limits=%s", proc.pid, container.limits.facility)

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # The protocol is UTF-8 JSON and the text is Korean; the console
        # encoding this process happens to have must not reach the pipe.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self._env_overrides)
        return env

    def _send_unload(self) -> None:
        stdin = self._stdin
        if stdin is None:
            return
        try:
            protocol.write_message(stdin, protocol.Unload(seq=self._counter.next()))
        except (OSError, ValueError):
            pass

    def _call(self, msg: Any, *, timeout: float, kill_on_timeout: bool) -> Any:
        seq = self._counter.next()
        msg.seq = seq
        waiter = _Waiter()
        with self._state_lock:
            if self._state is WorkerState.LOST:
                raise self._lost_error or EchoActError(Code.WORKER_LOST)
            if self._state is not WorkerState.RUNNING or self._stdin is None:
                raise EchoActError(
                    Code.WORKER_LOST, "The synthesis worker is not running."
                )
            self._pending[seq] = waiter
            stdin = self._stdin

        try:
            protocol.write_message(stdin, msg)
        except (OSError, ValueError) as exc:
            self._abandon(seq)
            raise EchoActError(
                Code.WORKER_LOST, "The synthesis worker's input closed.", cause=exc
            ) from exc

        if not waiter.event.wait(timeout):
            # The request is abandoned here, which is what makes a late reply
            # unmatchable: the seq is gone from the table before the worker
            # can answer it.
            self._abandon(seq)
            if kill_on_timeout:
                self.kill()
            raise EchoActError(
                Code.WORKER_LOST,
                f"The synthesis worker did not answer within {timeout:g} seconds.",
            )
        if waiter.error is not None:
            raise waiter.error
        return waiter.reply

    def _abandon(self, seq: int) -> None:
        with self._state_lock:
            self._pending.pop(seq, None)

    def _unexpected(self, reply: Any) -> EchoActError:
        return EchoActError(
            Code.INTERNAL,
            "The synthesis worker answered with an unexpected message.",
            detail={"type": getattr(reply, "type", "?")},
        )

    # -- reader ------------------------------------------------------------

    def _read_loop(self, proc: subprocess.Popen[bytes], stdout: TextIOWrapper) -> None:
        try:
            for raw in stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = protocol.decode(line)
                except Exception:
                    # A line we cannot parse is a protocol error, never
                    # something to guess at.  It is counted and skipped; the
                    # request it belonged to will time out on its own.
                    log.warning("unparseable line from the worker (%d chars)", len(line))
                    continue
                try:
                    self._dispatch(msg)
                except Exception:  # pragma: no cover - dispatch must not die
                    log.exception("worker message dispatch failed")
        except (OSError, ValueError):
            pass
        finally:
            self._on_eof(proc)

    def _dispatch(self, msg: Any) -> None:
        if isinstance(msg, protocol.Ready):
            self._worker_pid = msg.pid
            self._ready.set()
            return
        if isinstance(msg, protocol.Stats):
            self._last_report = (msg.rss_bytes, msg.cpu_percent, monotonic())
            return
        if isinstance(msg, protocol.Pong):
            prev = self._last_report
            cpu = prev[1] if prev else 0.0
            self._last_report = (msg.rss_bytes, cpu, monotonic())
        if isinstance(msg, protocol.Error) and msg.fatal:
            # A fatal error is about the worker, not only about the request
            # that provoked it, so everyone waiting hears about it.
            error = _error_from(msg)
            with self._state_lock:
                self._state = WorkerState.LOST
                self._lost_error = error
            self._fail_pending(error)
            return

        with self._state_lock:
            waiter = self._pending.pop(msg.seq, None)
        if waiter is None:
            # Stale: its request was cancelled or timed out.  Dropping it is
            # the protocol's rule -- matching it to the current request would
            # attach audio to text nobody asked about any more.
            self._dropped_replies += 1
            log.debug("dropped a stale %s reply seq=%d", msg.type, msg.seq)
            return
        if isinstance(msg, protocol.Error):
            waiter.fail(_error_from(msg))
        else:
            waiter.deliver(msg)

    def _drain_stderr(self, stderr: TextIOWrapper) -> None:
        """Read the worker's diagnostics so a full pipe can never block it.

        The lines are kept for a failure report but not logged at any normal
        level: N-20 puts the burden on both sides, and the parent cannot
        verify what the child wrote.
        """
        try:
            for raw in stderr:
                line = raw.rstrip()
                if line:
                    self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass

    def _on_eof(self, proc: subprocess.Popen[bytes]) -> None:
        """The worker's stdout closed, which means the worker is gone.

        A death the container caused by halting an over-budget worker is
        reported as OUT_OF_MEMORY rather than as a mystery: F-23 requires
        the reason, and on a platform where the ceiling is polled the
        container is the only party that knows it.  Where the kernel
        enforces the ceiling the worker survives its failed allocation and
        sends the error itself, so this path does not apply.
        """
        exit_code = proc.poll()
        container = self._container
        if container is not None and container.memory_exceeded:
            error = EchoActError(
                Code.OUT_OF_MEMORY,
                detail={"exit_code": exit_code, "memory_bytes": container.budget.memory_bytes},
            )
        else:
            error = EchoActError(
                Code.WORKER_LOST,
                "The synthesis worker stopped unexpectedly.",
                detail={"exit_code": exit_code},
            )
        with self._state_lock:
            if self._proc is not proc:
                return
            # ``_stopping`` distinguishes a death we asked for from one we
            # did not.  Only the second makes the supervisor unusable.
            if self._stopping:
                self._state = WorkerState.STOPPED
                self._ready.set()
                return
            self._state = WorkerState.LOST
            self._lost_error = error
            self._loaded = None
            self._ready.set()
        log.error("worker lost exit_code=%s", exit_code)
        self._fail_pending(error)

    def _fail_pending(self, error: EchoActError) -> None:
        with self._state_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for w in waiters:
            w.fail(error)

    # -- stream and process helpers ---------------------------------------

    @staticmethod
    def _wait(proc: subprocess.Popen[bytes], timeout: float) -> None:
        if timeout <= 0:
            return
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _close_stream(stream: Any) -> None:
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    def _teardown_streams(self) -> None:
        with self._state_lock:
            stdin, stdout = self._stdin, self._stdout
            self._stdin = self._stdout = None
        self._close_stream(stdin)
        self._close_stream(stdout)


def _force_kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.kill()
    except OSError:  # pragma: no cover - already gone
        pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def _error_from(msg: protocol.Error) -> EchoActError:
    """Translate the worker's small vocabulary of codes.

    An unrecognised code becomes GENERATION_FAILED rather than INTERNAL: the
    worker only ever fails a synthesis request, and F-57 asks that the user
    be told something true about what failed.
    """
    try:
        code = Code(msg.code)
    except ValueError:
        code = Code.GENERATION_FAILED
    detail: dict[str, Any] = {}
    if msg.job_id:
        detail["job_id"] = msg.job_id
    if msg.segment_index is not None:
        detail["segment_index"] = msg.segment_index
    return EchoActError(code, msg.message[:_MAX_WORKER_MESSAGE] or None, detail=detail)


__all__ = [
    "DEFAULT_ALLOWED_PROVIDERS",
    "LOAD_TIMEOUT_S",
    "PING_TIMEOUT_S",
    "START_TIMEOUT_S",
    "SYNTHESIZE_TIMEOUT_S",
    "LoadedModel",
    "WorkerState",
    "WorkerSupervisor",
    "WorkerUsage",
]
