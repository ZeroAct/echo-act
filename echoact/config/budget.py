"""Turning a resource *setting* into the ceiling a job actually runs under.

F-21 says the memory default is derived from the machine and that "the value
actually applied may be lower depending on currently available memory", so the
configured number and the enforced number are two different things.  F-78 makes
that distinction visible to the user; this module is where it is computed, and
``Settings`` never stores the result -- a derived number written back into a
settings file would follow the user onto a machine it does not describe.

F-23 draws the other line: below a 2 GiB budget the model is not loaded at all.
That is a refusal to start, not a degraded run, so ``resolve_budget`` raises
rather than returning something unusable.

Sampling and the halt decision live here too, because they compare against the
same numbers.  N-04 fixes the headroom and the roughly half-second cadence,
N-21 requires the generation job to be measured apart from the whole app --
which is why the sampler is pointed at the worker process's pid rather than at
this one -- and F-22 displays what it reports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

import psutil

from ..domain import Budget
from ..errors import Code, EchoActError
from ..policy import (
    CPU_PERCENT_MAX,
    CPU_PERCENT_MIN,
    HEADROOM_FRACTION,
    HEADROOM_MIN_BYTES,
    MEMORY_CEILING_BYTES,
    MEMORY_CEILING_FRACTION_OF_TOTAL,
    MEMORY_DEFAULT_FRACTION,
    MEMORY_DEFAULT_MAX_BYTES,
    MEMORY_DEFAULT_MIN_BYTES,
    MEMORY_FLOOR_BYTES,
    RESOURCE_SAMPLE_INTERVAL_S,
)
from ..util.ids import monotonic
from .settings import Settings

#: Upper bound on worker threads, whatever the CPU budget works out to.
#:
#: A.5 measured twenty threads running about twice as slowly as two on this
#: engine -- classic oversubscription -- and tuned the product at the two the
#: Section 8.2 baseline produces (8 logical CPUs at F-20's 20% default).  The
#: crossover point between "helps" and "hurts" was not measured, so the cap is
#: set at double the known-good figure and five times below the known-bad one.
#: Without a cap, F-20's 70% maximum on a 32-CPU workstation would ask for 22
#: threads and land squarely in the slow region, so the cap is what keeps
#: F-87's "threads come from the budget" from meaning "threads get worse as the
#: budget gets bigger".  It is a measured tuning constant, not a policy limit
#: from Section 4, which is why it lives beside the code that applies it.
MAX_INTRA_OP_THREADS: Final = 4

#: How soon a caller refused for want of memory could get a different answer:
#: one monitoring interval, since nothing else re-examines the machine.  F-57
#: requires a retryable refusal to carry a hint, and INSUFFICIENT_RESOURCES is
#: retryable because freeing memory elsewhere genuinely fixes it.
RESOURCE_RETRY_AFTER_S: Final = RESOURCE_SAMPLE_INTERVAL_S


def default_memory_bytes(total_ram_bytes: int) -> int:
    """F-21's out-of-the-box budget: about 25% of total RAM, held to 2-6 GiB."""
    quarter = int(total_ram_bytes * MEMORY_DEFAULT_FRACTION)
    return max(MEMORY_DEFAULT_MIN_BYTES, min(MEMORY_DEFAULT_MAX_BYTES, quarter))


def memory_ceiling_bytes(total_ram_bytes: int) -> int:
    """F-21's ceiling on what the owner may configure: at most 32 GiB, and
    never more than half of total RAM."""
    return int(min(MEMORY_CEILING_BYTES, total_ram_bytes * MEMORY_CEILING_FRACTION_OF_TOTAL))


def system_headroom_bytes(total_ram_bytes: int) -> int:
    """N-04: the greater of 10% of total RAM or 1 GiB, kept free for the host."""
    return int(max(total_ram_bytes * HEADROOM_FRACTION, HEADROOM_MIN_BYTES))


def intra_op_threads(logical_cpus: int, cpu_percent: int) -> int:
    """F-87: the worker's thread count comes from F-20's budget, never from the
    runtime's default.

    Rounded half-up rather than with Python's banker's rounding, so a machine
    that works out to exactly n.5 threads gets the larger count consistently
    instead of alternating with the parity of n.
    """
    share = logical_cpus * cpu_percent / 100.0
    threads = int(share + 0.5)
    return max(1, min(MAX_INTRA_OP_THREADS, threads))


def resolve_budget(
    settings: Settings,
    *,
    total_ram_bytes: int,
    available_ram_bytes: int,
    logical_cpus: int,
) -> Budget:
    """The ceiling this job will actually run under (F-20, F-21, F-23, N-04).

    ``available_ram_bytes`` is what makes the answer a *current* one: the
    applied memory ceiling is the configured value less whatever the host needs
    to keep, so the same settings legitimately produce a smaller budget on a
    busy machine.  F-78 shows both numbers; nothing writes this one back.

    Raises ``EchoActError(INSUFFICIENT_RESOURCES)`` when the result would fall
    under F-23's 2 GiB floor.  Returning a 1 GiB budget instead would let the
    model load and then be killed by the halt check moments later, which F-23
    rules out by saying the model is not loaded at all.
    """
    if total_ram_bytes <= 0 or logical_cpus <= 0:
        raise AssertionError("resolve_budget needs a real machine description")

    available = max(0, min(available_ram_bytes, total_ram_bytes))
    configured = (
        default_memory_bytes(total_ram_bytes)
        if settings.memory_bytes is None
        else settings.memory_bytes
    )
    # F-20 and F-21 are safety limits, not just widget ranges: a Settings built
    # in code, or restored from another machine's file, is clamped here too.
    configured = min(configured, memory_ceiling_bytes(total_ram_bytes))
    cpu_percent = max(CPU_PERCENT_MIN, min(CPU_PERCENT_MAX, settings.cpu_percent))

    headroom = system_headroom_bytes(total_ram_bytes)
    applied = min(configured, available - headroom)

    if applied < MEMORY_FLOOR_BYTES:
        raise EchoActError(
            Code.INSUFFICIENT_RESOURCES,
            detail={
                "required_bytes": MEMORY_FLOOR_BYTES,
                "configured_bytes": configured,
                "available_bytes": available,
                "headroom_bytes": headroom,
                "grantable_bytes": max(0, applied),
            },
            retry_after_s=RESOURCE_RETRY_AFTER_S,
        )

    return Budget(
        cpu_percent=cpu_percent,
        memory_bytes=int(applied),
        intra_op_threads=intra_op_threads(logical_cpus, cpu_percent),
        inter_op_threads=1,
    )


def resolve_budget_from_system(settings: Settings) -> Budget:
    """``resolve_budget`` against this machine, read once so the three numbers
    describe the same instant."""
    vm = psutil.virtual_memory()
    return resolve_budget(
        settings,
        total_ram_bytes=int(vm.total),
        available_ram_bytes=int(vm.available),
        logical_cpus=psutil.cpu_count(logical=True) or 1,
    )


# ======================================================================
# Measurement (N-04, N-21, F-22)
# ======================================================================


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One reading of the generation job and of the host, taken together.

    Both halves come from the same moment on purpose: F-23 halts on either the
    job's own usage or the system's free memory, and comparing an RSS from now
    against a free-memory figure from several seconds ago would halt jobs for
    conditions that had already passed.
    """

    #: Worker resident set size.  N-03 warns that this and the number the
    #: operating system's limit acts on may differ; this is the displayed one.
    rss_bytes: int
    #: Share of *total logical CPU capacity*, per Section 4.1's display rule --
    #: not psutil's per-core percentage, which exceeds 100 on a busy machine.
    cpu_percent: float
    system_total_bytes: int
    system_available_bytes: int
    at: float


class ResourceSampler:
    """Reads the worker process's RSS and CPU at N-04's half-second cadence.

    Points at the worker's pid, not at this process: N-21 requires the
    generation job to be measured apart from total app usage, and the GUI, the
    database, and the REST server all live in the parent.

    It owns no thread and never sleeps.  The job engine already runs a loop
    that must stay responsive to cancellation within N-22's five seconds, so
    the cadence is expressed as ``due()`` for that loop to consult; a private
    timer thread here would sample a worker the engine had already killed.
    """

    def __init__(
        self,
        pid: int,
        *,
        interval_s: float = RESOURCE_SAMPLE_INTERVAL_S,
        logical_cpus: int | None = None,
        process: Any | None = None,
        memory_probe: Callable[[], tuple[int, int]] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.pid = pid
        self.interval_s = interval_s
        self._clock = clock
        self._logical_cpus = max(1, logical_cpus or psutil.cpu_count(logical=True) or 1)
        self._memory_probe = memory_probe or _system_memory
        self._last_at: float | None = None
        self._alive = True
        if process is not None:
            self._process = process
        else:
            try:
                self._process = psutil.Process(pid)
            except psutil.Error:
                self._process = None
                self._alive = False
            else:
                # Establishes psutil's baseline; the first real reading would
                # otherwise report the process's whole lifetime average.
                try:
                    self._process.cpu_percent(None)
                except psutil.Error:
                    self._alive = False

    @property
    def alive(self) -> bool:
        """False once the worker has gone.  A killed worker is the normal end
        of a cancelled job (N-22), so it is a state, not an error."""
        return self._alive

    def due(self, at: float | None = None) -> bool:
        now = self._clock() if at is None else at
        return self._last_at is None or (now - self._last_at) >= self.interval_s

    def sample(self) -> ResourceSample | None:
        """Read now, whether or not ``due()``.  ``None`` means the worker is
        gone and there is nothing left to measure."""
        if self._process is None:
            self._alive = False
            return None
        try:
            rss = int(self._process.memory_info().rss)
            raw_cpu = float(self._process.cpu_percent(None))
        except psutil.Error:
            self._alive = False
            return None
        total, available = self._memory_probe()
        self._last_at = self._clock()
        return ResourceSample(
            rss_bytes=rss,
            cpu_percent=round(raw_cpu / self._logical_cpus, 2),
            system_total_bytes=total,
            system_available_bytes=available,
            at=self._last_at,
        )

    def poll(self) -> ResourceSample | None:
        """Sample only if the interval has elapsed.  ``None`` also means "not
        yet", so check ``alive`` to tell the two apart."""
        if not self.due():
            return None
        return self.sample()


def _system_memory() -> tuple[int, int]:
    vm = psutil.virtual_memory()
    return int(vm.total), int(vm.available)


# ======================================================================
# F-23's halt decision
# ======================================================================


class HaltReason(StrEnum):
    """Why a running job must stop.  F-23 requires the reason to be reported,
    and the two causes call for different advice: one is the app's own budget,
    the other is the rest of the machine."""

    NONE = "none"
    BUDGET_EXCEEDED = "budget_exceeded"
    SYSTEM_HEADROOM = "system_headroom"


@dataclass(frozen=True, slots=True)
class HaltDecision:
    should_halt: bool
    reason: HaltReason = HaltReason.NONE
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_error(self) -> EchoActError:
        """The failure to fail the job with.  F-23 also releases the model;
        that is the job engine's to do, because only it holds the worker."""
        if not self.should_halt:
            raise AssertionError("no halt was decided")
        return EchoActError(
            Code.OUT_OF_MEMORY,
            self.message,
            detail={"reason": self.reason.value, **dict(self.detail)},
            retry_after_s=RESOURCE_RETRY_AFTER_S,
        )


_CONTINUE: Final = HaltDecision(should_halt=False)


def halt_decision(sample: ResourceSample, budget: Budget) -> HaltDecision:
    """F-23: halt when the job is over its limit, or the host is short.

    Only memory decides this.  Halting a job for exceeding its CPU share would
    be wrong twice over: N-03 makes CPU the operating-system container's to
    throttle rather than the app's to police, and N-04 already concedes that
    momentary spikes between half-second samples are not prevented -- so a
    single busy interval would kill a job the requirements say should merely
    run more slowly.  Memory is different: an RSS reading that is over budget
    is over budget until something frees it.
    """
    if sample.rss_bytes > budget.memory_bytes:
        return HaltDecision(
            should_halt=True,
            reason=HaltReason.BUDGET_EXCEEDED,
            message="The job was halted because it exceeded its memory budget.",
            detail={"rss_bytes": sample.rss_bytes, "limit_bytes": budget.memory_bytes},
        )

    headroom = system_headroom_bytes(sample.system_total_bytes)
    if sample.system_available_bytes < headroom:
        return HaltDecision(
            should_halt=True,
            reason=HaltReason.SYSTEM_HEADROOM,
            message="The job was halted because the computer ran short of free memory.",
            detail={
                "available_bytes": sample.system_available_bytes,
                "headroom_bytes": headroom,
            },
        )
    return _CONTINUE


__all__ = [
    "MAX_INTRA_OP_THREADS",
    "RESOURCE_RETRY_AFTER_S",
    "HaltDecision",
    "HaltReason",
    "ResourceSample",
    "ResourceSampler",
    "default_memory_bytes",
    "halt_decision",
    "intra_op_threads",
    "memory_ceiling_bytes",
    "resolve_budget",
    "resolve_budget_from_system",
    "system_headroom_bytes",
]
