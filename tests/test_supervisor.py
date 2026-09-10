"""The parent side of the worker (F-17 to F-19, N-21, N-22, protocol seq).

Almost everything here runs against a fake worker written into ``tmp_path``
that speaks the real ``echoact.engine.protocol``.  That is deliberate: the
behaviours these tests are about -- a stale reply being dropped, a crash
becoming WORKER_LOST, release inside five seconds while a segment is still
rendering -- are properties of the supervisor and of process lifetime, and
tying them to a 385 MB model would make them slow, flaky, and unrunnable on
a machine without the weights.  One test at the end drives the real worker
and is marked ``engine``.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

import echoact
from echoact import paths
from echoact.domain import Budget
from echoact.engine.container import Enforcement, NullContainer, ResourceContainer, make_container
from echoact.engine.supervisor import (
    DEFAULT_ALLOWED_PROVIDERS,
    WorkerState,
    WorkerSupervisor,
)
from echoact.errors import Code, EchoActError
from echoact.policy import WORKER_RELEASE_DEADLINE_S

REPO_ROOT = Path(echoact.__file__).resolve().parent.parent
MODEL_DIR = Path.home() / ".cache" / "supertonic3"
REAL_WORKER = Path(echoact.__file__).resolve().parent / "engine" / "worker.py"

BUDGET = Budget(cpu_percent=20, memory_bytes=2 * (1 << 30), intra_op_threads=2)
OTHER_BUDGET = Budget(cpu_percent=40, memory_bytes=3 * (1 << 30), intra_op_threads=2)

requires_engine = pytest.mark.skipif(
    not (MODEL_DIR / "onnx").is_dir() or not REAL_WORKER.exists(),
    reason="the Supertonic 3 weights or the worker module are absent",
)

#: A worker that answers the protocol without owning a model.  Flags in
#: ``argv`` select the misbehaviour a test needs; every request it handles is
#: appended to ``ECHOACT_FAKE_LOG`` so a test can count loads.
FAKE_WORKER = '''
import os
import sys
import time

from echoact.engine import protocol

FLAGS = set(sys.argv[1:])
LOG = os.environ.get("ECHOACT_FAKE_LOG")
OUT = sys.stdout


def note(what):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(what + "\\n")


def number(prefix, default=0.0):
    for f in FLAGS:
        if f.startswith(prefix):
            return float(f[len(prefix):])
    return default


if "--no-ready" not in FLAGS:
    protocol.write_message(OUT, protocol.Ready(pid=os.getpid()))
    note("ready")
if "--die-after-ready" in FLAGS:
    sys.exit(3)
if "--stats" in FLAGS:
    protocol.write_message(OUT, protocol.Stats(rss_bytes=123456, cpu_percent=4.5))

pings = 0
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = protocol.decode(raw)
    kind = msg.type
    note(kind)
    if kind == protocol.MsgType.SHUTDOWN:
        if "--ignore-shutdown" in FLAGS:
            while True:
                time.sleep(0.05)
        break
    if kind == protocol.MsgType.LOAD:
        if "--fatal-on-load" in FLAGS:
            protocol.write_message(OUT, protocol.Error(
                code="GENERATION_FAILED", message="the session refused to build",
                fatal=True, seq=msg.seq))
            continue
        protocol.write_message(OUT, protocol.Loaded(
            model_id=msg.model_id, sample_rate=44100, voices=["F1", "M1"],
            providers=list(msg.allowed_providers), load_seconds=0.01, seq=msg.seq))
    elif kind == protocol.MsgType.SYNTHESIZE:
        if "--crash-on-synthesize" in FLAGS:
            os._exit(9)
        if "--hang-on-synthesize" in FLAGS:
            while True:
                time.sleep(0.05)
        if "--error-on-synthesize" in FLAGS:
            protocol.write_message(OUT, protocol.Error(
                code="GENERATION_FAILED", message="segment failed", job_id=msg.job_id,
                segment_index=msg.segment_index, seq=msg.seq))
            continue
        with open(msg.out_path, "wb") as fh:
            fh.write(b"RIFFfake")
        protocol.write_message(OUT, protocol.Audio(
            job_id=msg.job_id, segment_index=msg.segment_index, out_path=msg.out_path,
            frame_count=44100, sample_rate=44100, synth_seconds=0.02, peak=0.5, seq=msg.seq))
    elif kind == protocol.MsgType.PING:
        pings += 1
        delay = number("--slow-first-ping=")
        if pings == 1 and delay:
            time.sleep(delay)
        protocol.write_message(OUT, protocol.Pong(rss_bytes=222222, seq=msg.seq))
'''


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    yield
    paths.data_dir.cache_clear()


@pytest.fixture
def worker_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    return script


@pytest.fixture
def fake_log(tmp_path: Path) -> Path:
    return tmp_path / "fake_worker.log"


@pytest.fixture
def supervisors() -> Iterator[list[WorkerSupervisor]]:
    made: list[WorkerSupervisor] = []
    yield made
    for sup in made:
        try:
            sup.kill()
        except Exception:  # pragma: no cover - teardown must not mask a failure
            pass


class _Factory:
    """Remembers the container it made, so a test can look inside it."""

    def __init__(self, kind: str = "null") -> None:
        self.kind = kind
        self.containers: list[ResourceContainer] = []

    def __call__(self, budget: Budget) -> ResourceContainer:
        c = NullContainer(budget) if self.kind == "null" else make_container(budget)
        self.containers.append(c)
        return c

    @property
    def last(self) -> ResourceContainer:
        return self.containers[-1]


def _supervisor(
    supervisors: list[WorkerSupervisor],
    worker_script: Path,
    fake_log: Path,
    *flags: str,
    factory: _Factory | None = None,
    start_timeout_s: float = 10.0,
) -> WorkerSupervisor:
    sup = WorkerSupervisor(
        command=[sys.executable, str(worker_script), *flags],
        container_factory=factory or _Factory(),
        env={"PYTHONPATH": str(REPO_ROOT), "ECHOACT_FAKE_LOG": str(fake_log)},
        start_timeout_s=start_timeout_s,
    )
    supervisors.append(sup)
    return sup


def _requests(fake_log: Path) -> list[str]:
    if not fake_log.exists():
        return []
    return fake_log.read_text(encoding="utf-8").split()


#: A pid whose process has exited but whose handle the parent still holds
#: reports as "terminated" on Windows rather than disappearing, so liveness
#: is asked about the status rather than about the pid existing.
_GONE = {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE, "terminated"}


def _is_gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() in _GONE
    except psutil.NoSuchProcess:
        return True


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ----------------------------------------------------------------------
# Start-up and the warm-reuse rules
# ----------------------------------------------------------------------


def test_a_load_starts_the_worker_and_reports_what_it_prepared(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log)
    assert sup.would_need_load("m1", BUDGET) is True

    model = sup.load("m1", "/models/m1", BUDGET)

    assert sup.state is WorkerState.RUNNING
    assert model.model_id == "m1"
    assert model.sample_rate == 44100
    assert model.voices == ("F1", "M1")
    # F-87's allow-list is what the parent asked for; the check that counts
    # happens in the worker, against the session it built.
    assert model.providers == ("CPUExecutionProvider",)
    assert _requests(fake_log).count("load") == 1


def test_a_second_job_with_the_same_model_does_not_reload_it(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """F-17: the model is kept in memory between jobs."""
    sup = _supervisor(supervisors, worker_script, fake_log)
    first = sup.load("m1", "/models/m1", BUDGET)
    assert sup.would_need_load("m1", BUDGET) is False

    second = sup.load("m1", "/models/m1", BUDGET)

    assert second is first
    assert _requests(fake_log).count("load") == 1


def test_changing_only_the_voice_settings_keeps_the_model(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path, tmp_path: Path
) -> None:
    """F-18.  Voice, language, style and tempo are arguments to a synthesis
    request, so they cannot reach the loaded model at all."""
    sup = _supervisor(supervisors, worker_script, fake_log)
    sup.load("m1", "/models/m1", BUDGET)
    pid = sup.worker_pid

    for i, (voice, lang, speed) in enumerate(
        [("F1", "ko", 1.0), ("M1", "en", 1.4), ("F1", "ko", 0.7)]
    ):
        sup.synthesize(
            job_id="job_1",
            segment_index=i,
            text="안녕하세요",
            lang=lang,
            voice_id=voice,
            speed=speed,
            out_path=tmp_path / f"seg{i}.wav",
        )

    assert _requests(fake_log).count("load") == 1
    assert sup.worker_pid == pid
    assert sup.would_need_load("m1", BUDGET) is False


def test_a_different_model_is_reloaded_in_the_same_worker(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """F-19 releases the old model; nothing says the process must go too,
    and keeping it saves an interpreter start on every model switch."""
    sup = _supervisor(supervisors, worker_script, fake_log)
    sup.load("m1", "/models/m1", BUDGET)
    pid = sup.worker_pid

    model = sup.load("m2", "/models/m2", BUDGET)

    assert model.model_id == "m2"
    assert sup.worker_pid == pid
    requests = _requests(fake_log)
    assert requests.count("load") == 2
    assert "unload" in requests


def test_a_model_reprepared_somewhere_else_is_not_answered_from_the_old_weights(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """F-17's warm reuse is keyed on the directory too, so a repair or a
    relocation (F-65) reloads instead of reusing what is in memory."""
    sup = _supervisor(supervisors, worker_script, fake_log)
    sup.load("m1", "/models/m1", BUDGET)

    sup.load("m1", "/models/m1-repaired", BUDGET)

    assert _requests(fake_log).count("load") == 2
    assert (sup.loaded_model.model_dir if sup.loaded_model else "") == "/models/m1-repaired"


def test_a_different_budget_replaces_the_worker(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """F-19: a new resource limit releases the model.  The limit is fixed in
    the container when it is created, so a new budget is a new container and
    therefore a new process -- there is no way to re-limit a live one."""
    factory = _Factory()
    sup = _supervisor(supervisors, worker_script, fake_log, factory=factory)
    sup.load("m1", "/models/m1", BUDGET)
    first_pid = sup.worker_pid
    assert sup.would_need_load("m1", OTHER_BUDGET) is True

    sup.load("m1", "/models/m1", OTHER_BUDGET)

    assert sup.worker_pid != first_pid
    assert sup.budget == OTHER_BUDGET
    assert len(factory.containers) == 2
    assert factory.containers[0].closed
    assert _wait_until(lambda: _is_gone(first_pid or -1), 5.0)


def test_unload_releases_the_model_but_keeps_the_worker(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path, tmp_path: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log)
    sup.load("m1", "/models/m1", BUDGET)
    pid = sup.worker_pid

    sup.unload()

    assert sup.loaded_model is None
    assert sup.worker_pid == pid
    assert sup.state is WorkerState.RUNNING
    assert sup.would_need_load("m1", BUDGET) is True
    with pytest.raises(EchoActError) as caught:
        sup.synthesize(
            job_id="job_1",
            segment_index=0,
            text="x",
            lang="en",
            voice_id="F1",
            speed=1.0,
            out_path=tmp_path / "x.wav",
        )
    assert caught.value.code is Code.MODEL_NOT_READY


def test_a_worker_that_never_reports_ready_is_not_left_running(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log, "--no-ready", start_timeout_s=0.5)
    with pytest.raises(EchoActError) as caught:
        sup.load("m1", "/models/m1", BUDGET)
    assert caught.value.code is Code.WORKER_LOST
    assert sup.state is WorkerState.STOPPED
    assert sup.worker_pid is None


# ----------------------------------------------------------------------
# Synthesis and the seq discipline
# ----------------------------------------------------------------------


def test_a_segment_comes_back_as_a_file_the_parent_named(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path, tmp_path: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log)
    sup.load("m1", "/models/m1", BUDGET)
    out = tmp_path / "seg0.wav"

    audio = sup.synthesize(
        job_id="job_1",
        segment_index=0,
        text="안녕하세요",
        lang="ko",
        voice_id="F1",
        speed=1.0,
        out_path=out,
    )

    assert audio.out_path == str(out)
    assert audio.frame_count == 44100
    assert out.exists()


def test_a_stale_reply_is_dropped_rather_than_matched_to_the_next_request(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """The protocol's reason for carrying a seq at all: a reply for an
    abandoned request must never be handed to whatever ran next."""
    sup = _supervisor(supervisors, worker_script, fake_log, "--slow-first-ping=1.0")
    sup.load("m1", "/models/m1", BUDGET)
    load_seq = 1  # the counter starts at one and load was the first request

    with pytest.raises(EchoActError):
        sup.ping(timeout=0.2)
    abandoned = sup.dropped_replies

    pong = sup.ping(timeout=5.0)

    assert pong.seq > load_seq + 1, "the second ping must answer its own request"
    assert _wait_until(lambda: sup.dropped_replies > abandoned, 3.0), (
        "the late reply should have arrived and been discarded"
    )
    assert sup.state is WorkerState.RUNNING


def test_a_worker_error_reply_reaches_the_caller_with_its_code(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path, tmp_path: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log, "--error-on-synthesize")
    sup.load("m1", "/models/m1", BUDGET)

    with pytest.raises(EchoActError) as caught:
        sup.synthesize(
            job_id="job_7",
            segment_index=3,
            text="x",
            lang="en",
            voice_id="F1",
            speed=1.0,
            out_path=tmp_path / "x.wav",
        )

    err = caught.value
    assert err.code is Code.GENERATION_FAILED
    assert err.detail == {"job_id": "job_7", "segment_index": 3}
    # The worker stays usable: one segment failing is not the worker dying.
    assert sup.is_usable
    assert sup.state is WorkerState.RUNNING


def test_a_fatal_worker_error_stops_the_supervisor_being_used(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log, "--fatal-on-load")

    with pytest.raises(EchoActError):
        sup.load("m1", "/models/m1", BUDGET)

    assert not sup.is_usable
    with pytest.raises(EchoActError) as caught:
        sup.ping()
    assert caught.value.code is Code.GENERATION_FAILED


# ----------------------------------------------------------------------
# Crash handling
# ----------------------------------------------------------------------


def test_a_crash_mid_request_surfaces_worker_lost_and_does_not_restart(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path, tmp_path: Path
) -> None:
    """A silent restart would look like a recovery while having lost the
    model and the job's remaining segments; F-19 makes that the job engine's
    call, not the supervisor's."""
    sup = _supervisor(supervisors, worker_script, fake_log, "--crash-on-synthesize")
    sup.load("m1", "/models/m1", BUDGET)
    dead_pid = sup.worker_pid

    with pytest.raises(EchoActError) as caught:
        sup.synthesize(
            job_id="job_1",
            segment_index=0,
            text="x",
            lang="en",
            voice_id="F1",
            speed=1.0,
            out_path=tmp_path / "x.wav",
        )

    assert caught.value.code is Code.WORKER_LOST
    assert sup.state is WorkerState.LOST
    assert not sup.is_usable
    assert sup.loaded_model is None
    with pytest.raises(EchoActError) as again:
        sup.load("m1", "/models/m1", BUDGET)
    assert again.value.code is Code.WORKER_LOST
    assert sup.worker_pid == dead_pid, "no new process was started behind the caller's back"

    # An explicit teardown is what clears it: the decision is now the
    # caller's and is visible in the code that made it.
    sup.kill()
    assert sup.is_usable
    assert sup.load("m1", "/models/m1", BUDGET).model_id == "m1"


def test_a_worker_that_dies_while_idle_is_noticed_before_the_next_request(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log)
    sup.load("m1", "/models/m1", BUDGET)
    pid = sup.worker_pid
    assert pid is not None

    psutil.Process(pid).kill()

    assert _wait_until(lambda: sup.state is WorkerState.LOST, 5.0)
    with pytest.raises(EchoActError) as caught:
        sup.ping()
    assert caught.value.code is Code.WORKER_LOST


# ----------------------------------------------------------------------
# N-22: release within five seconds
# ----------------------------------------------------------------------


def test_kill_releases_within_the_deadline_while_a_segment_is_rendering(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path, tmp_path: Path
) -> None:
    """N-22 gives five seconds from an accepted cancellation to released
    resources, and A.2 says that implies aborting mid-segment."""
    sup = _supervisor(
        supervisors, worker_script, fake_log, "--hang-on-synthesize", "--ignore-shutdown"
    )
    sup.load("m1", "/models/m1", BUDGET)
    pid = sup.worker_pid
    assert pid is not None

    failure: list[BaseException] = []

    def _render() -> None:
        try:
            sup.synthesize(
                job_id="job_1",
                segment_index=0,
                text="x",
                lang="ko",
                voice_id="F1",
                speed=1.0,
                out_path=tmp_path / "x.wav",
                timeout=30.0,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            failure.append(exc)

    renderer = threading.Thread(target=_render, daemon=True)
    renderer.start()
    assert _wait_until(lambda: "synthesize" in _requests(fake_log), 5.0)

    elapsed = sup.kill()

    assert elapsed <= WORKER_RELEASE_DEADLINE_S, f"release took {elapsed:.2f}s"
    assert _wait_until(lambda: _is_gone(pid), 5.0)
    renderer.join(timeout=2.0)
    assert not renderer.is_alive(), "the blocked caller was left waiting"
    assert failure and isinstance(failure[0], EchoActError)
    assert sup.state is WorkerState.STOPPED


def test_kill_meets_the_deadline_when_the_worker_ignores_the_polite_request(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log, "--ignore-shutdown")
    sup.load("m1", "/models/m1", BUDGET)
    pid = sup.worker_pid
    assert pid is not None

    elapsed = sup.kill()

    assert elapsed <= WORKER_RELEASE_DEADLINE_S, f"release took {elapsed:.2f}s"
    assert _wait_until(lambda: _is_gone(pid), 5.0)


def test_kill_on_a_worker_that_is_already_gone_still_returns_promptly(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    sup = _supervisor(supervisors, worker_script, fake_log)
    sup.load("m1", "/models/m1", BUDGET)
    assert sup.kill() <= WORKER_RELEASE_DEADLINE_S
    assert sup.kill() < 1.0
    assert sup.state is WorkerState.STOPPED


# ----------------------------------------------------------------------
# F-22 / N-21: the figures
# ----------------------------------------------------------------------


def test_usage_reports_the_generation_jobs_own_figures(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    factory = _Factory("platform")
    sup = _supervisor(supervisors, worker_script, fake_log, factory=factory)
    sup.load("m1", "/models/m1", BUDGET)

    usage = sup.sample_usage()

    assert usage is not None
    assert usage.rss_bytes > 0
    assert usage.age_s < 5.0
    assert usage.limits is not None
    assert usage.limits.memory_bytes == BUDGET.memory_bytes
    if sys.platform == "win32":
        # N-21 wants the generation job measured apart from the whole app;
        # the Job Object accounts for exactly its members.
        assert usage.source == "job object"
        assert usage.limits.memory is Enforcement.ENFORCED
        assert factory.last.contains(sup.worker_pid or -1)


def test_usage_falls_back_to_the_workers_own_report(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """F-22 keeps updating while idle with a model retained, and Stats is
    what the worker volunteers for it."""
    sup = _supervisor(supervisors, worker_script, fake_log, "--stats")
    sup.load("m1", "/models/m1", BUDGET)

    assert _wait_until(lambda: sup.last_worker_report is not None, 5.0)
    rss, cpu, _at = sup.last_worker_report or (0, 0.0, 0.0)
    assert rss == 123456
    assert cpu == pytest.approx(4.5)

    pong = sup.ping()
    assert pong.rss_bytes == 222222


def test_a_worker_halted_for_being_over_budget_reports_that_reason(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """F-23 wants the reason reported, and on a platform where the ceiling
    is polled the container is the only party that knows it."""

    class _OverBudget(NullContainer):
        @property
        def memory_exceeded(self) -> bool:
            return True

    sup = WorkerSupervisor(
        command=[sys.executable, str(worker_script)],
        container_factory=_OverBudget,
        env={"PYTHONPATH": str(REPO_ROOT), "ECHOACT_FAKE_LOG": str(fake_log)},
    )
    supervisors.append(sup)
    sup.load("m1", "/models/m1", BUDGET)
    pid = sup.worker_pid
    assert pid is not None
    assert sup.memory_exceeded is True

    psutil.Process(pid).kill()  # what the polling monitor does to an over-budget worker

    assert _wait_until(lambda: sup.state is WorkerState.LOST, 5.0)
    with pytest.raises(EchoActError) as caught:
        sup.ping()
    assert caught.value.code is Code.OUT_OF_MEMORY
    assert caught.value.detail["memory_bytes"] == BUDGET.memory_bytes


def test_the_worker_is_not_run_at_all_if_its_container_cannot_be_applied(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """N-03 is a guarantee, not a preference."""

    class _Refusing(NullContainer):
        def adopt(self, pid: int) -> None:
            raise OSError("no")

    sup = WorkerSupervisor(
        command=[sys.executable, str(worker_script)],
        container_factory=_Refusing,
        env={"PYTHONPATH": str(REPO_ROOT), "ECHOACT_FAKE_LOG": str(fake_log)},
    )
    supervisors.append(sup)
    with pytest.raises(EchoActError) as caught:
        sup.load("m1", "/models/m1", BUDGET)
    assert caught.value.code is Code.INTERNAL
    assert not fake_log.exists(), "the worker got as far as running"


def test_the_parents_allow_list_matches_the_runtimes(
    supervisors: list[WorkerSupervisor], worker_script: Path, fake_log: Path
) -> None:
    """F-87 has one allow-list.  The supervisor mirrors it instead of
    importing it, to keep onnxruntime out of the parent process, so the two
    copies are checked against each other here."""
    from echoact.engine.runtime import ALLOWED_PROVIDERS

    assert DEFAULT_ALLOWED_PROVIDERS == ALLOWED_PROVIDERS

    sup = _supervisor(supervisors, worker_script, fake_log)
    model = sup.load("m1", "/models/m1", BUDGET)
    # The fake echoes back what it was asked for, which is what the parent
    # sent on the wire.
    assert model.providers == ALLOWED_PROVIDERS


def test_the_default_command_is_the_worker_module() -> None:
    sup = WorkerSupervisor()
    assert sup._command == (sys.executable, "-m", "echoact.engine.worker")


# ----------------------------------------------------------------------
# The real thing
# ----------------------------------------------------------------------


@pytest.mark.engine
@requires_engine
def test_the_real_worker_loads_a_model_and_renders_korean(tmp_path: Path) -> None:
    """A.9's end-to-end check at the smallest scale the supervisor can do.

    A model id this test cannot know is the one risk: the id comes from the
    F-84 manifest, which is another module's business, so a rejection on
    that ground is reported as a skip rather than as a supervisor failure.
    """
    budget = Budget(cpu_percent=20, memory_bytes=6 * (1 << 30), intra_op_threads=2)
    sup = WorkerSupervisor()
    try:
        try:
            model = sup.load("supertonic-3", MODEL_DIR, budget, timeout=120.0)
        except EchoActError as exc:
            if exc.code in {Code.MODEL_UNKNOWN, Code.MODEL_NOT_READY, Code.MODEL_CORRUPT}:
                pytest.skip(f"the worker did not accept this model id: {exc.code.value}")
            raise
        assert model.sample_rate == 44100
        assert "CPUExecutionProvider" in model.providers
        # F-87/N-01: nothing that executes remotely may be in the session.
        assert not any("Azure" in p for p in model.providers)

        out = tmp_path / "seg0.wav"
        audio = sup.synthesize(
            job_id="job_engine",
            segment_index=0,
            text="안녕하세요, 반갑습니다.",
            lang="ko",
            voice_id=model.voices[0],
            speed=1.0,
            out_path=out,
            timeout=120.0,
        )
        assert out.exists() and out.stat().st_size > 0
        assert audio.frame_count > 0
        assert audio.sample_rate == 44100

        usage = sup.sample_usage()
        assert usage is not None and usage.rss_bytes > 0
    finally:
        assert sup.kill() <= WORKER_RELEASE_DEADLINE_S


@pytest.mark.engine
@requires_engine
def test_the_real_worker_is_released_within_five_seconds_mid_segment(tmp_path: Path) -> None:
    """N-22 against the real engine, where a segment genuinely takes time."""
    budget = Budget(cpu_percent=20, memory_bytes=6 * (1 << 30), intra_op_threads=2)
    sup = WorkerSupervisor()
    try:
        try:
            model = sup.load("supertonic-3", MODEL_DIR, budget, timeout=120.0)
        except EchoActError as exc:
            if exc.code in {Code.MODEL_UNKNOWN, Code.MODEL_NOT_READY, Code.MODEL_CORRUPT}:
                pytest.skip(f"the worker did not accept this model id: {exc.code.value}")
            raise
        outcome: list[BaseException] = []

        def _render() -> None:
            try:
                sup.synthesize(
                    job_id="job_engine",
                    segment_index=0,
                    text="가나다라마바사 아자차카타파하. " * 12,
                    lang="ko",
                    voice_id=model.voices[0],
                    speed=1.0,
                    out_path=tmp_path / "long.wav",
                    timeout=180.0,
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                outcome.append(exc)

        renderer = threading.Thread(target=_render, daemon=True)
        renderer.start()
        time.sleep(0.5)

        elapsed = sup.kill()

        assert elapsed <= WORKER_RELEASE_DEADLINE_S, f"release took {elapsed:.2f}s"
        renderer.join(timeout=3.0)
        assert not renderer.is_alive()
    finally:
        sup.kill()


def test_the_fake_worker_script_is_a_faithful_protocol_speaker(worker_script: Path) -> None:
    """If the fake drifts from the protocol the tests above stop meaning
    anything, so it is compiled and its imports resolved here."""
    result = subprocess.run(
        [sys.executable, "-c", f"import py_compile; py_compile.compile({str(worker_script)!r}, doraise=True)"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
