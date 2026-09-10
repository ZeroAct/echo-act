"""The operating-system resource container (N-03, N-21, F-20, F-21, F-22).

The tests that matter here are the ones that would catch a container that
*claims* to protect the host without doing it, so most of them check an
observable kernel effect -- the child is inside the job, closing the handle
kills it -- rather than a flag the code set on itself.  The remainder check
the opposite direction: that a platform without an enforced ceiling reports
MONITORED, because N-03 says an enforced ceiling is not guaranteed on macOS
and F-22 has to be able to tell the user so.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from echoact import paths
from echoact.domain import Budget
from echoact.engine import container as container_mod
from echoact.engine.container import (
    Enforcement,
    LimitBasis,
    NullContainer,
    PlatformContainer,
    PosixContainer,
    ResourceContainer,
    WindowsJobContainer,
    make_container,
)

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Job Objects are a Windows facility")

BUDGET = Budget(cpu_percent=20, memory_bytes=2 * (1 << 30), intra_op_threads=2)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may touch the real user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    yield
    paths.data_dir.cache_clear()


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[bytes]]]:
    spawned: list[subprocess.Popen[bytes]] = []
    yield spawned
    for proc in spawned:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - already gone
            pass


def _spawn(
    container: ResourceContainer,
    code: str,
    children: list[subprocess.Popen[bytes]],
    *,
    resume: bool = True,
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **container.popen_kwargs(),
    )
    children.append(proc)
    container.adopt(proc.pid)
    if resume:
        container.start_child(proc.pid)
    return proc


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
# Windows: the enforced case
# ----------------------------------------------------------------------


@windows_only
def test_windows_gets_a_job_object_and_says_its_limits_are_enforced() -> None:
    with make_container(BUDGET) as c:
        assert isinstance(c, WindowsJobContainer)
        limits = c.limits
        assert limits.memory is Enforcement.ENFORCED
        assert limits.kill_on_close is Enforcement.ENFORCED
        assert limits.memory_bytes == BUDGET.memory_bytes
        assert limits.cpu_percent == BUDGET.cpu_percent
        # N-03: the limit is tested against commit, not the working set the
        # user sees, and the container has to say which.
        assert limits.memory_basis is LimitBasis.COMMIT


@windows_only
def test_the_child_is_accounted_to_the_job(children: list[subprocess.Popen[bytes]]) -> None:
    with make_container(BUDGET) as c:
        assert isinstance(c, WindowsJobContainer)
        proc = _spawn(c, "import time; time.sleep(60)", children)
        assert proc.pid in c.job_pids()
        assert c.contains(proc.pid)


@windows_only
def test_nothing_runs_in_the_child_before_it_is_inside_the_job(
    tmp_path: Path, children: list[subprocess.Popen[bytes]]
) -> None:
    """F-20 and F-21 would be advisory for the child's first instants if it
    were assigned after starting, so it is created suspended."""
    marker = tmp_path / "ran.txt"
    code = f"open({str(marker)!r}, 'w').write('x')"
    with make_container(BUDGET) as c:
        proc = _spawn(c, code, children, resume=False)
        time.sleep(0.4)
        assert not marker.exists(), "the child executed before it was resumed"
        assert proc.poll() is None
        c.start_child(proc.pid)
        assert _wait_until(marker.exists, 5.0)


@windows_only
def test_closing_the_container_kills_a_sleeping_child(
    children: list[subprocess.Popen[bytes]],
) -> None:
    """KILL_ON_JOB_CLOSE is what N-22's five seconds ultimately rest on: the
    worker cannot outlive the container even if it ignores everything."""
    c = make_container(BUDGET)
    proc = _spawn(c, "import time; time.sleep(120)", children)
    assert proc.poll() is None
    c.close()
    assert _wait_until(lambda: proc.poll() is not None, 5.0)
    assert _wait_until(lambda: _is_gone(proc.pid), 5.0)


@windows_only
def test_accounting_separates_the_generation_job_from_the_rest_of_the_app(
    children: list[subprocess.Popen[bytes]],
) -> None:
    """N-21: the CPU and memory reported are the contained job's alone."""
    with make_container(BUDGET) as c:
        assert isinstance(c, WindowsJobContainer)
        _spawn(c, "x = 0\nfor i in range(8_000_000): x += i\nimport time; time.sleep(30)", children)
        assert _wait_until(
            lambda: (a := c.job_accounting()) is not None
            and a["user_seconds"] + a["kernel_seconds"] > 0.0,
            5.0,
        )
        acct = c.job_accounting()
        assert acct is not None
        assert acct["active_processes"] >= 1
        assert acct["peak_process_commit"] > 0

        usage = c.sample()
        assert usage is not None
        assert usage.rss_bytes > 0
        assert usage.source == "job object"
        # The on-screen figure and the enforced figure are different
        # quantities; N-03 allows them to differ and both are reported.
        assert usage.peak_commit_bytes > 0


@windows_only
def test_cpu_rate_control_falls_back_to_monitored_when_the_build_rejects_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-20 on a build without CPU rate control: report MONITORED rather
    than a ceiling that is not there."""

    class _Rejector:
        argtypes: object = None
        restype: object = None

        def __call__(self, *args: object) -> int:
            return 0

    class _Kernel32:
        def __init__(self, real: object) -> None:
            self._real = real
            self.SetInformationJobObject = _Rejector()

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    real_windll = container_mod.ctypes.WinDLL
    monkeypatch.setattr(
        container_mod.ctypes,
        "WinDLL",
        lambda *a, **kw: _Kernel32(real_windll(*a, **kw)),
    )
    with WindowsJobContainer(BUDGET) as c:
        limits = c.limits
        assert limits.cpu is Enforcement.MONITORED
        assert "priority class" in limits.facility
        # Memory is a separate facility and is unaffected by the refusal.
        assert limits.memory is Enforcement.ENFORCED
        assert not limits.fully_enforced


@windows_only
def test_an_enforced_memory_ceiling_is_not_reported_as_a_polled_one() -> None:
    """Where the kernel refuses the allocation there is nothing to poll, so
    ``memory_exceeded`` stays false and F-23's report comes from the worker's
    own failure instead."""
    with make_container(BUDGET) as c:
        assert c.memory_exceeded is False


# ----------------------------------------------------------------------
# POSIX: the case N-03 refuses to promise
# ----------------------------------------------------------------------


def test_a_mac_style_container_does_not_claim_an_enforced_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N-03: on macOS an enforced ceiling that blocks even momentary spikes
    is not guaranteed, so the container must not say ENFORCED."""
    monkeypatch.setattr(container_mod.sys, "platform", "darwin")
    limits = PosixContainer(BUDGET).limits
    assert limits.memory is Enforcement.MONITORED
    assert limits.memory_basis is LimitBasis.RESIDENT
    assert limits.cpu is Enforcement.MONITORED
    assert limits.kill_on_close is Enforcement.MONITORED
    assert not limits.fully_enforced


def test_a_linux_style_container_reports_the_rlimit_it_actually_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_mod.sys, "platform", "linux")
    limits = PosixContainer(BUDGET).limits
    assert limits.memory is Enforcement.ENFORCED
    assert limits.memory_basis is LimitBasis.ADDRESS_SPACE
    # Niceness is scheduling order, not share: never an enforced CPU cap.
    assert limits.cpu is Enforcement.MONITORED


def test_a_lower_cpu_budget_yields_the_machine_more_readily() -> None:
    """F-20's percentage becomes a nice value on POSIX; the mapping has to
    run the right way round."""
    low = PosixContainer(Budget(cpu_percent=10, memory_bytes=1 << 31, intra_op_threads=1))
    high = PosixContainer(Budget(cpu_percent=70, memory_bytes=1 << 31, intra_op_threads=1))
    assert low._nice_value() > high._nice_value()
    assert high._nice_value() == 0
    assert low._nice_value() == 19


def test_the_posix_container_applies_its_limits_before_the_image_runs() -> None:
    kwargs = PosixContainer(BUDGET).popen_kwargs()
    assert callable(kwargs["preexec_fn"])
    assert kwargs["start_new_session"] is True


# ----------------------------------------------------------------------
# The null container, and the platform choice
# ----------------------------------------------------------------------


def test_the_null_container_admits_it_protects_nothing() -> None:
    limits = NullContainer(BUDGET).limits
    assert limits.memory is Enforcement.UNAVAILABLE
    assert limits.cpu is Enforcement.UNAVAILABLE
    assert limits.kill_on_close is Enforcement.UNAVAILABLE
    assert limits.memory_basis is LimitBasis.NONE
    assert not limits.fully_enforced


def test_the_null_container_still_measures_the_job(
    children: list[subprocess.Popen[bytes]],
) -> None:
    """F-22 and N-21 want the figures even where no ceiling can be applied."""
    c = NullContainer(BUDGET)
    proc = _spawn(c, "import time; time.sleep(60)", children)
    usage = c.sample()
    assert usage is not None
    assert usage.rss_bytes > 0
    assert usage.process_count >= 1
    assert usage.source == "psutil"
    assert c.contains(proc.pid)
    c.close()
    assert _wait_until(lambda: proc.poll() is not None, 5.0)


def test_the_platform_decides_the_container_at_import() -> None:
    expected = WindowsJobContainer if WINDOWS else PosixContainer
    assert PlatformContainer is expected


def test_a_container_that_cannot_be_created_degrades_instead_of_refusing_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app that will not synthesise because a Job Object could not be
    made would be worse than one that says its limits are unavailable."""

    def _explode(*_a: object, **_kw: object) -> ResourceContainer:
        raise OSError("no container today")

    monkeypatch.setattr(container_mod, "PlatformContainer", _explode)
    c = make_container(BUDGET)
    assert isinstance(c, NullContainer)
    assert c.limits.memory is Enforcement.UNAVAILABLE
    c.close()


def test_cpu_percent_is_a_share_of_the_whole_machine(
    children: list[subprocess.Popen[bytes]],
) -> None:
    """Section 4.1 fixes the unit: percentage of total logical capacity, not
    of one core.  One busy thread on a multi-core box must therefore report
    well under 100."""
    if (psutil.cpu_count() or 1) < 2:  # pragma: no cover - single-core CI
        pytest.skip("needs more than one logical CPU to distinguish the units")
    c = make_container(BUDGET)
    try:
        _spawn(c, "x = 0\nwhile True: x += 1", children)
        c.sample()
        time.sleep(1.0)
        usage = c.sample()
        assert usage is not None
        assert 0.0 <= usage.cpu_percent < 100.0
    finally:
        c.close()
