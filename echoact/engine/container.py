"""The operating-system resource container that holds the synthesis worker.

N-03 is the requirement this module exists for, and it is deliberately
uneven: on Windows the CPU and memory of the generation job are *enforced*,
while on macOS "an enforced ceiling that blocks even momentary usage spikes
is not guaranteed".  So the container does not present one fiction with a
different implementation underneath.  It applies the strongest facility the
platform has and then says, per limit, whether that facility is ``ENFORCED``
(the kernel refuses to let the child exceed it) or ``MONITORED`` (we sample,
and a spike between samples passes unseen).  F-22 shows the answer, which is
why :class:`ContainerLimits` is part of the public surface rather than an
implementation note.

The container is also what makes N-21 measurable.  A Job Object accounts for
its members and nothing else, so the CPU time and peak memory reported here
are the generation job's alone -- not the GUI's, the database's, or the
service's.  The polling monitor exists for the same reason on platforms with
no such accounting, and on Windows it supplies the current working set and a
CPU rate, which the Job Object does not keep.

**Which memory figure is which.**  N-03 warns that "the RAM usage shown on
screen and the memory value used for limit decisions may differ", and on
Windows they genuinely do: ``ProcessMemoryLimit`` is enforced against
*committed* memory, while the figure a user recognises as RAM usage is the
working set.  A process can commit 3 GiB and hold 800 MiB resident.  So
:class:`ContainerUsage` reports both and labels them: ``rss_bytes`` is the
on-screen figure, ``peak_commit_bytes`` is the quantity the Windows limit is
enforced against, and :attr:`ContainerLimits.memory_basis` names which one
this platform's limit actually tests.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import psutil

from ..domain import Budget
from ..policy import CPU_PERCENT_MAX, CPU_PERCENT_MIN, RESOURCE_SAMPLE_INTERVAL_S
from ..util.ids import monotonic
from ..util.logging import get_logger

log = get_logger("engine.container")

_LOGICAL_CPUS: Final = os.cpu_count() or 1


class Enforcement(StrEnum):
    """How seriously a limit is meant.  N-03 requires this distinction to
    reach the user, so it is a value, not a comment."""

    #: The kernel refuses the excess.  The child cannot exceed the limit.
    ENFORCED = "enforced"
    #: Sampled every ``RESOURCE_SAMPLE_INTERVAL_S`` and acted on after the
    #: fact.  A spike shorter than the interval is not caught.
    MONITORED = "monitored"
    #: Neither.  The figure is reported and nothing is applied.
    UNAVAILABLE = "unavailable"


class LimitBasis(StrEnum):
    """Which memory quantity a platform's memory limit is tested against.

    N-03 allows the on-screen figure and the limit figure to differ; this
    names the second one so a display can say which is which.
    """

    COMMIT = "commit"
    RESIDENT = "resident"
    ADDRESS_SPACE = "address_space"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ContainerLimits:
    """What was actually applied, for F-22's display and N-03's honesty."""

    memory: Enforcement
    cpu: Enforcement
    #: Whether closing the container is by itself enough to kill the worker.
    kill_on_close: Enforcement
    memory_basis: LimitBasis
    memory_bytes: int
    cpu_percent: int
    #: Names the facility, e.g. "windows job object, hard CPU cap".  Safe for
    #: a log and for the GUI: it never contains a path or user text.
    facility: str = ""

    @property
    def fully_enforced(self) -> bool:
        return self.memory is Enforcement.ENFORCED and self.cpu is Enforcement.ENFORCED


@dataclass(frozen=True, slots=True)
class ContainerUsage:
    """One sample of the contained job's resource use.

    ``rss_bytes`` is the on-screen figure (F-22).  ``peak_commit_bytes`` is
    the peak of the quantity Windows enforces against; it is zero where the
    platform does not account for it.  N-03 permits the two to differ, and
    this type is where that difference is visible rather than hidden.
    """

    rss_bytes: int
    peak_rss_bytes: int
    peak_commit_bytes: int
    user_seconds: float
    kernel_seconds: float
    #: Percentage of *total logical CPU capacity*, matching Section 4.1's
    #: unit rule and F-20's setting -- not percentage of one core.
    cpu_percent: float
    process_count: int
    #: ``monotonic()`` at the sample, never wall clock, so a clock change
    #: cannot make a rate negative.
    sampled_at: float
    source: str


class ResourceContainer:
    """Common interface.  A container is created per budget, holds exactly
    one worker process tree, and is single-use: :meth:`close` ends it.

    The lifecycle is fixed by the Windows requirement that a child must be
    inside the job before it runs anything, or F-20 and F-21 would be
    advisory for the first instants of the process.  Callers therefore do
    ``popen_kwargs()`` -> spawn -> :meth:`adopt` -> :meth:`start_child`, and
    a platform with no such hazard makes ``start_child`` a no-op.
    """

    facility = "none"

    def __init__(self, budget: Budget, *, name: str = "echoact-worker") -> None:
        self.budget = budget
        self.name = name
        self._pid: int | None = None
        self._proc: psutil.Process | None = None
        self._closed = False
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        self._sample_lock = threading.Lock()
        self._usage: ContainerUsage | None = None
        self._peak_rss = 0
        self._prev_cpu_seconds: float | None = None
        self._prev_sampled_at: float | None = None
        self._memory_exceeded = False

    # -- construction-time facts ------------------------------------------

    @property
    def limits(self) -> ContainerLimits:
        raise NotImplementedError  # pragma: no cover - abstract

    def popen_kwargs(self) -> dict[str, Any]:
        """Extra ``subprocess.Popen`` arguments this container needs."""
        return {}

    # -- lifecycle ---------------------------------------------------------

    def adopt(self, pid: int) -> None:
        """Place an already-created process under this container.

        Raises ``OSError`` if the platform facility rejects the process; the
        caller must then abandon the child rather than run it unconstrained.
        """
        self._pid = pid
        try:
            self._proc = psutil.Process(pid)
        except psutil.Error as exc:  # pragma: no cover - died immediately
            raise OSError(f"cannot observe pid {pid}") from exc
        self._start_monitor()

    def start_child(self, pid: int) -> None:
        """Let an adopted child begin executing.  A no-op where the child was
        never suspended."""

    def terminate(self) -> None:
        """Kill what is inside without releasing the container.

        The whole tree, not just the process we spawned: the interpreter a
        virtual environment hands out can be a launcher that runs the real
        one as a child, and killing only the launcher would leave the worker
        holding the model -- exactly what N-22 says must not survive.
        """
        p = self._proc
        if p is None:
            return
        try:
            victims = [*p.children(recursive=True), p]
        except psutil.Error:
            victims = [p]
        for victim in victims:
            try:
                victim.kill()
            except psutil.Error:
                continue

    def close(self) -> None:
        """Release the container.  Where the platform supports it this also
        kills anything still inside, which is what N-22's five-second
        deadline ultimately rests on."""
        self._closed = True
        self._stop.set()
        m = self._monitor
        if m is not None and m is not threading.current_thread():
            m.join(timeout=1.0)
        self._monitor = None

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> ResourceContainer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- accounting --------------------------------------------------------

    def usage(self) -> ContainerUsage | None:
        """The most recent sample, or ``None`` before the first one."""
        with self._sample_lock:
            return self._usage

    def sample(self) -> ContainerUsage | None:
        """Take a sample now.  The monitor thread calls this on a timer;
        callers may force one to refresh a display."""
        u = self._collect()
        if u is not None:
            with self._sample_lock:
                self._usage = u
        return u

    @property
    def memory_exceeded(self) -> bool:
        """True once a *monitored* memory limit was seen to be exceeded.

        Where memory is ENFORCED this stays false: the kernel fails the
        allocation instead, so F-23's report comes from the worker's own
        failure rather than from a sample.
        """
        return self._memory_exceeded

    def contains(self, pid: int) -> bool:
        """Whether the platform facility currently accounts for this pid."""
        return self._pid == pid and self._alive()

    # -- internals ---------------------------------------------------------

    def _alive(self) -> bool:
        p = self._proc
        try:
            return p is not None and p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def _start_monitor(self) -> None:
        if self._monitor is not None:
            return
        self.sample()
        t = threading.Thread(target=self._monitor_loop, name=f"{self.name}-monitor", daemon=True)
        self._monitor = t
        t.start()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(RESOURCE_SAMPLE_INTERVAL_S):
            if not self._alive():
                continue
            try:
                self.sample()
                self._enforce_by_polling()
            except Exception:  # pragma: no cover - a sample must never crash
                log.debug("resource sample failed", exc_info=True)

    def _enforce_by_polling(self) -> None:
        """Where memory is MONITORED, act on a sample.  Overridden to do
        nothing where the kernel already enforces the ceiling."""

    def _process_sample(self) -> tuple[int, float, int] | None:
        """``(rss, cpu_seconds, process_count)`` over the worker's tree."""
        p = self._proc
        if p is None:
            return None
        try:
            with p.oneshot():
                rss = int(p.memory_info().rss)
                times = p.cpu_times()
                cpu = float(times.user + times.system)
            count = 1
            for child in p.children(recursive=True):
                try:
                    rss += int(child.memory_info().rss)
                    ct = child.cpu_times()
                    cpu += float(ct.user + ct.system)
                    count += 1
                except psutil.Error:
                    continue
            return rss, cpu, count
        except psutil.Error:
            return None

    def _rate(self, cpu_seconds: float, at: float) -> float:
        prev_cpu, prev_at = self._prev_cpu_seconds, self._prev_sampled_at
        self._prev_cpu_seconds, self._prev_sampled_at = cpu_seconds, at
        if prev_cpu is None or prev_at is None or at <= prev_at:
            return 0.0
        used = max(0.0, cpu_seconds - prev_cpu)
        return 100.0 * used / ((at - prev_at) * _LOGICAL_CPUS)

    def _collect(self) -> ContainerUsage | None:
        raise NotImplementedError  # pragma: no cover - abstract

    def _psutil_collect(self) -> ContainerUsage | None:
        """The sample every container without kernel accounting takes."""
        at = monotonic()
        proc = self._process_sample()
        if proc is None:
            return None
        rss, cpu_seconds, count = proc
        self._peak_rss = max(self._peak_rss, rss)
        return ContainerUsage(
            rss_bytes=rss,
            peak_rss_bytes=self._peak_rss,
            peak_commit_bytes=0,
            user_seconds=cpu_seconds,
            kernel_seconds=0.0,
            cpu_percent=self._rate(cpu_seconds, at),
            process_count=count,
            sampled_at=at,
            source="psutil",
        )


# ======================================================================
# Windows: a Job Object
# ======================================================================

# JOBOBJECT_CPU_RATE_CONTROL_INFORMATION is not wrapped by pywin32, so the
# CPU half of F-20 goes through ctypes.  The information class number and the
# flags are from the Win32 headers and are part of the stable ABI.
_JOB_CPU_RATE_CONTROL_INFO_CLASS: Final = 15
_CPU_RATE_CONTROL_ENABLE: Final = 0x1
_CPU_RATE_CONTROL_HARD_CAP: Final = 0x4
#: ``CpuRate`` is in hundredths of a percent of *total* machine CPU, which is
#: exactly F-20's unit, so the only conversion is a factor of 100.
_CPU_RATE_SCALE: Final = 100

_CREATE_SUSPENDED: Final = 0x00000004
_CREATE_NO_WINDOW: Final = 0x08000000
_THREAD_SUSPEND_RESUME: Final = 0x0002
_BELOW_NORMAL_PRIORITY_CLASS: Final = 0x00004000
_RESUME_THREAD_FAILED: Final = 0xFFFFFFFF


class _CpuRateControl(ctypes.Structure):
    _fields_ = (("ControlFlags", ctypes.c_uint32), ("CpuRate", ctypes.c_uint32))


class WindowsJobContainer(ResourceContainer):
    """N-03's enforced case.

    A Job Object gives all three things the requirements ask for at once: a
    memory ceiling the kernel refuses to exceed (F-21), a CPU rate the
    scheduler enforces (F-20), and ``KILL_ON_JOB_CLOSE`` so the worker cannot
    outlive the container -- the last being what lets N-22 promise release
    within five seconds even if the worker ignores every polite request.

    Limiting the process directly was rejected: a working-set limit only
    trims, it does not refuse, and an affinity mask fixes *which* cores are
    used rather than how much of them, so neither can express F-20's "20% of
    total CPU".
    """

    facility = "windows job object"

    def __init__(self, budget: Budget, *, name: str = "echoact-worker") -> None:
        super().__init__(budget, name=name)
        import win32job

        self._win32job = win32job
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Unnamed: a name would be a machine-wide handle another process
        # could open, and nothing needs to find this one.
        self._job: Any = win32job.CreateJobObject(None, "")
        self._cpu_enforcement = Enforcement.UNAVAILABLE
        self._cpu_facility = "none"
        self._apply_memory_limit()
        self._apply_cpu_limit()

    # -- limit application -------------------------------------------------

    def _apply_memory_limit(self) -> None:
        wj = self._win32job
        info = wj.QueryInformationJobObject(self._job, wj.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = (
            wj.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | wj.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | wj.JOB_OBJECT_LIMIT_JOB_MEMORY
            | wj.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        # Both limits carry the same figure: one worker process is expected,
        # and the job-wide limit catches anything the worker itself spawns,
        # which N-21 counts against the generation job either way.
        info["ProcessMemoryLimit"] = int(self.budget.memory_bytes)
        info["JobMemoryLimit"] = int(self.budget.memory_bytes)
        wj.SetInformationJobObject(self._job, wj.JobObjectExtendedLimitInformation, info)

    def _apply_cpu_limit(self) -> None:
        percent = max(CPU_PERCENT_MIN, min(CPU_PERCENT_MAX, int(self.budget.cpu_percent)))
        rate = max(1, min(10_000, percent * _CPU_RATE_SCALE))
        self._k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._k32.SetInformationJobObject.restype = ctypes.c_int

        for flags, label in (
            (_CPU_RATE_CONTROL_ENABLE | _CPU_RATE_CONTROL_HARD_CAP, "hard CPU cap"),
            (_CPU_RATE_CONTROL_ENABLE, "CPU rate target"),
        ):
            data = _CpuRateControl(flags, rate)
            ok = self._k32.SetInformationJobObject(
                int(self._job),
                _JOB_CPU_RATE_CONTROL_INFO_CLASS,
                ctypes.byref(data),
                ctypes.sizeof(data),
            )
            if ok:
                self._cpu_enforcement = Enforcement.ENFORCED
                self._cpu_facility = label
                return
            log.debug(
                "job cpu rate control rejected (flags=%#x err=%d)",
                flags,
                ctypes.get_last_error(),
            )

        # Older or restricted builds reject the information class outright.
        # Say so rather than reporting a limit that is not there: a lowered
        # priority class still yields the machine under contention, but it is
        # a courtesy, not a ceiling, so CPU drops to MONITORED.
        try:
            wj = self._win32job
            # Through the *extended* structure, not the basic one: the job
            # already carries the memory flags, and the basic information
            # class rejects a flag set that contains them.
            info = wj.QueryInformationJobObject(self._job, wj.JobObjectExtendedLimitInformation)
            basic = info["BasicLimitInformation"]
            basic["LimitFlags"] = basic["LimitFlags"] | wj.JOB_OBJECT_LIMIT_PRIORITY_CLASS
            basic["PriorityClass"] = _BELOW_NORMAL_PRIORITY_CLASS
            wj.SetInformationJobObject(self._job, wj.JobObjectExtendedLimitInformation, info)
            self._cpu_facility = "below-normal priority class"
        except Exception:  # pragma: no cover - depends on the Windows build
            log.warning("no CPU limiting facility available on this build")
            self._cpu_facility = "none"
        self._cpu_enforcement = Enforcement.MONITORED

    @property
    def limits(self) -> ContainerLimits:
        return ContainerLimits(
            memory=Enforcement.ENFORCED,
            cpu=self._cpu_enforcement,
            kill_on_close=Enforcement.ENFORCED,
            memory_basis=LimitBasis.COMMIT,
            memory_bytes=int(self.budget.memory_bytes),
            cpu_percent=int(self.budget.cpu_percent),
            facility=f"{self.facility}, {self._cpu_facility}",
        )

    # -- lifecycle ---------------------------------------------------------

    def popen_kwargs(self) -> dict[str, Any]:
        """Create the child suspended.

        A child assigned after it has begun running has already had time to
        allocate outside the limit.  ``CREATE_SUSPENDED`` closes that window
        completely: nothing in the child executes until :meth:`start_child`,
        so F-20 and F-21 hold from its first instruction rather than from a
        few milliseconds in.
        """
        return {"creationflags": _CREATE_SUSPENDED | _CREATE_NO_WINDOW}

    def adopt(self, pid: int) -> None:
        import win32api
        import win32con

        access = (
            win32con.PROCESS_SET_QUOTA
            | win32con.PROCESS_TERMINATE
            | win32con.PROCESS_QUERY_INFORMATION
        )
        # pywin32 raises ``pywintypes.error``, which is not an OSError, so it
        # is translated here rather than leaking a Windows-only type into a
        # caller that has to work on three platforms.
        try:
            handle = win32api.OpenProcess(access, False, pid)
        except Exception as exc:
            raise OSError(f"cannot open pid {pid} for job assignment: {exc}") from exc
        try:
            self._win32job.AssignProcessToJobObject(self._job, handle)
        except Exception as exc:
            raise OSError(f"cannot assign pid {pid} to the job object: {exc}") from exc
        finally:
            handle.Close()
        super().adopt(pid)

    def start_child(self, pid: int) -> None:
        """Resume the suspended child, now that it is inside the job.

        ``subprocess`` closes the primary thread handle it got from
        ``CreateProcess``, so the thread is reopened by id instead.  A freshly
        created suspended process has exactly one thread.
        """
        k32 = self._k32
        k32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenThread.restype = ctypes.c_void_p
        k32.ResumeThread.argtypes = [ctypes.c_void_p]
        k32.ResumeThread.restype = ctypes.c_uint32
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        try:
            threads = psutil.Process(pid).threads()
        except psutil.Error as exc:
            raise OSError(f"cannot enumerate threads of pid {pid}") from exc
        if not threads:  # pragma: no cover - a live process always has one
            raise OSError(f"pid {pid} has no threads to resume")

        resumed = False
        for t in threads:
            handle = k32.OpenThread(_THREAD_SUSPEND_RESUME, False, int(t.id))
            if not handle:
                continue
            try:
                if k32.ResumeThread(handle) != _RESUME_THREAD_FAILED:
                    resumed = True
            finally:
                k32.CloseHandle(handle)
        if not resumed:
            raise OSError(f"could not resume pid {pid}")

    def terminate(self) -> None:
        """Kill everything in the job at once, without closing it."""
        if self._job is None:
            return
        try:
            self._win32job.TerminateJobObject(self._job, 1)
        except Exception:  # pragma: no cover - already gone
            log.debug("terminate job object failed", exc_info=True)

    def close(self) -> None:
        super().close()
        job, self._job = self._job, None
        if job is None:
            return
        # KILL_ON_JOB_CLOSE makes this handle the leash: dropping the last
        # handle kills whatever is still inside.
        try:
            job.Close()
        except Exception:  # pragma: no cover - double close
            log.debug("closing job object failed", exc_info=True)

    # -- accounting --------------------------------------------------------

    def job_accounting(self) -> dict[str, Any] | None:
        """Raw Job Object accounting: the generation job's own totals, which
        is exactly what N-21 needs to separate it from whole-app usage."""
        if self._job is None:
            return None
        wj = self._win32job
        try:
            acct = wj.QueryInformationJobObject(
                self._job, wj.JobObjectBasicAndIoAccountingInformation
            )
            ext = wj.QueryInformationJobObject(self._job, wj.JobObjectExtendedLimitInformation)
        except Exception:
            return None
        basic = acct["BasicInfo"]
        return {
            # 100-nanosecond units, per the Win32 struct.
            "user_seconds": basic["TotalUserTime"] / 1e7,
            "kernel_seconds": basic["TotalKernelTime"] / 1e7,
            "active_processes": int(basic["ActiveProcesses"]),
            "total_processes": int(basic["TotalProcesses"]),
            "peak_process_commit": int(ext["PeakProcessMemoryUsed"]),
            "peak_job_commit": int(ext["PeakJobMemoryUsed"]),
        }

    def job_pids(self) -> tuple[int, ...]:
        """The pids the kernel currently accounts to this job."""
        if self._job is None:
            return ()
        wj = self._win32job
        try:
            listing = wj.QueryInformationJobObject(self._job, wj.JobObjectBasicProcessIdList)
        except Exception:
            return ()
        return tuple(int(p) for p in listing)

    def contains(self, pid: int) -> bool:
        return pid in self.job_pids()

    def _collect(self) -> ContainerUsage | None:
        at = monotonic()
        acct = self.job_accounting()
        proc = self._process_sample()
        if acct is None and proc is None:
            return None
        rss = proc[0] if proc else 0
        self._peak_rss = max(self._peak_rss, rss)
        if acct is not None:
            user = acct["user_seconds"]
            kernel = acct["kernel_seconds"]
            cpu_seconds = user + kernel
            count = acct["active_processes"]
            peak_commit = max(acct["peak_process_commit"], acct["peak_job_commit"])
            source = "job object"
        else:  # pragma: no cover - job closed between the two reads
            cpu_seconds = proc[1] if proc else 0.0
            user = kernel = 0.0
            count = proc[2] if proc else 0
            peak_commit = 0
            source = "psutil"
        return ContainerUsage(
            rss_bytes=rss,
            peak_rss_bytes=self._peak_rss,
            peak_commit_bytes=peak_commit,
            user_seconds=user,
            kernel_seconds=kernel,
            cpu_percent=self._rate(cpu_seconds, at),
            process_count=count,
            sampled_at=at,
            source=source,
        )


# ======================================================================
# POSIX: rlimits, niceness, a process group, and honest labelling
# ======================================================================


class PosixContainer(ResourceContainer):
    """N-03's unenforced case, stated as such.

    macOS has no Job Object.  What it does have is applied here -- an address
    space rlimit, a nice value, and a process group so :meth:`close` can
    reach the whole tree -- but none of it is the ceiling Windows gets:

    * ``RLIMIT_AS`` bounds address space, not resident memory, and a 64-bit
      process reserves far more than it touches, so a limit tight enough to
      mean anything for RSS would refuse allocations the worker never faults
      in.  It is applied with headroom on Linux, where refusing an allocation
      is a genuine ceiling, and as a coarse backstop on Darwin, where memory
      is reported MONITORED because N-03 explicitly declines to promise an
      enforced ceiling there.
    * Niceness changes scheduling order, not share.  An idle machine will
      happily give a niced process everything, so F-20's percentage is a
      target here and never a cap.

    The polling monitor therefore does real work on this platform: it is what
    turns F-23's "if usage exceeds the limit ... the job is halted" into
    behaviour rather than a hope.
    """

    facility = "posix rlimit + nice"

    #: Address space may exceed the RSS budget by this factor before the
    #: rlimit refuses, because reserved-but-untouched mappings are normal and
    #: a 1:1 limit would kill a healthy worker.
    _AS_HEADROOM: Final = 4
    #: The most yielding nice value, used at F-20's lowest CPU setting.
    _MAX_NICE: Final = 19

    def __init__(self, budget: Budget, *, name: str = "echoact-worker") -> None:
        super().__init__(budget, name=name)
        self._enforced_memory = sys.platform.startswith("linux")

    @property
    def limits(self) -> ContainerLimits:
        return ContainerLimits(
            memory=Enforcement.ENFORCED if self._enforced_memory else Enforcement.MONITORED,
            cpu=Enforcement.MONITORED,
            kill_on_close=Enforcement.MONITORED,
            memory_basis=(
                LimitBasis.ADDRESS_SPACE if self._enforced_memory else LimitBasis.RESIDENT
            ),
            memory_bytes=int(self.budget.memory_bytes),
            cpu_percent=int(self.budget.cpu_percent),
            facility=self.facility,
        )

    def popen_kwargs(self) -> dict[str, Any]:
        """Limits are applied in the child before ``exec``.

        That is the POSIX equivalent of the Windows suspended-create: the
        image never runs a single instruction outside its limits.
        """
        limit = int(self.budget.memory_bytes) * self._AS_HEADROOM
        nice = self._nice_value()

        def _apply() -> None:  # pragma: no cover - runs in the forked child
            import resource

            for which in ("RLIMIT_AS", "RLIMIT_DATA"):
                res = getattr(resource, which, None)
                if res is None:
                    continue
                try:
                    _soft, hard = resource.getrlimit(res)
                    ceiling = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
                    resource.setrlimit(res, (ceiling, hard))
                    break
                except (ValueError, OSError):
                    continue
            try:
                os.nice(nice)
            except OSError:
                pass

        return {"preexec_fn": _apply, "start_new_session": True}

    def _nice_value(self) -> int:
        span = CPU_PERCENT_MAX - CPU_PERCENT_MIN
        frac = (int(self.budget.cpu_percent) - CPU_PERCENT_MIN) / (span or 1)
        return int(round(self._MAX_NICE * (1.0 - max(0.0, min(1.0, frac)))))

    def terminate(self) -> None:
        self._signal_group()

    def close(self) -> None:
        super().close()
        # No KILL_ON_JOB_CLOSE here; the process group is the nearest thing,
        # and a child that leaves the group survives.  ``limits`` says so.
        self._signal_group()

    def _signal_group(self) -> None:
        pid = self._pid
        if pid is None:
            return
        try:
            os.killpg(os.getpgid(pid), 9)
        except (OSError, AttributeError):
            pass

    def _enforce_by_polling(self) -> None:
        if self._enforced_memory:
            return
        u = self.usage()
        if u is None or u.rss_bytes <= int(self.budget.memory_bytes):
            return
        self._memory_exceeded = True
        log.warning(
            "worker exceeded the monitored memory budget (%d > %d); halting it",
            u.rss_bytes,
            self.budget.memory_bytes,
        )
        self.terminate()

    def _collect(self) -> ContainerUsage | None:
        return self._psutil_collect()


class NullContainer(ResourceContainer):
    """No limits at all: for tests, and for a platform whose facilities we
    cannot use.

    It still samples, because F-22's display and N-21's separate measurement
    are worth having even where no ceiling can be applied, and because a test
    of the supervisor should exercise the same sampling path the product
    uses.  Every limit reports UNAVAILABLE, so nothing can mistake it for
    protection.
    """

    facility = "none"

    @property
    def limits(self) -> ContainerLimits:
        return ContainerLimits(
            memory=Enforcement.UNAVAILABLE,
            cpu=Enforcement.UNAVAILABLE,
            kill_on_close=Enforcement.UNAVAILABLE,
            memory_basis=LimitBasis.NONE,
            memory_bytes=int(self.budget.memory_bytes),
            cpu_percent=int(self.budget.cpu_percent),
            facility=self.facility,
        )

    def close(self) -> None:
        super().close()
        # Nothing enforces anything here, but a test container that leaked a
        # process would be worse than useless, so the child is still killed.
        self.terminate()

    def _collect(self) -> ContainerUsage | None:
        return self._psutil_collect()


#: Chosen once, at import, by platform -- not per call, so a test can see
#: which implementation this machine will really use.
if sys.platform == "win32":
    PlatformContainer: type[ResourceContainer] = WindowsJobContainer
elif os.name == "posix":
    PlatformContainer = PosixContainer
else:  # pragma: no cover - no third kind exists today
    PlatformContainer = NullContainer


def make_container(budget: Budget, *, name: str = "echoact-worker") -> ResourceContainer:
    """The container this platform can actually provide.

    Falls back to :class:`NullContainer` if the platform facility cannot be
    created.  Refusing to synthesise because a Job Object could not be made
    would be worse than saying the limits are unavailable, which
    :attr:`ResourceContainer.limits` makes visible and F-22 shows.
    """
    try:
        return PlatformContainer(budget, name=name)
    except Exception:
        log.warning(
            "resource container unavailable; running without enforced limits", exc_info=True
        )
        return NullContainer(budget, name=name)


__all__ = [
    "ContainerLimits",
    "ContainerUsage",
    "Enforcement",
    "LimitBasis",
    "NullContainer",
    "PlatformContainer",
    "PosixContainer",
    "ResourceContainer",
    "WindowsJobContainer",
    "make_container",
]
