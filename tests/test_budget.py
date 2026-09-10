"""Resource-budget resolution and monitoring (F-20, F-21, F-23, F-87, N-04, N-21)."""

from __future__ import annotations

from pathlib import Path

import psutil
import pytest

from echoact import paths
from echoact.config.budget import (
    MAX_INTRA_OP_THREADS,
    HaltReason,
    ResourceSample,
    ResourceSampler,
    default_memory_bytes,
    halt_decision,
    intra_op_threads,
    memory_ceiling_bytes,
    resolve_budget,
    resolve_budget_from_system,
    system_headroom_bytes,
)
from echoact.config.settings import Settings
from echoact.domain import Budget
from echoact.errors import Code, EchoActError
from echoact.policy import GIB, MEMORY_FLOOR_BYTES

BASELINE_CPUS = 8  # Section 8.2's reference machine.


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``Settings()`` reads the environment for F-86; keep it off the real tree."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ECHOACT_DISPLAY_LANGUAGE", "en")
    paths.data_dir.cache_clear()
    yield tmp_path
    paths.data_dir.cache_clear()


def _resolve(
    settings: Settings | None = None,
    *,
    total_gib: float = 16,
    available_gib: float | None = None,
    logical_cpus: int = BASELINE_CPUS,
) -> Budget:
    total = int(total_gib * GIB)
    available = total if available_gib is None else int(available_gib * GIB)
    return resolve_budget(
        settings or Settings(),
        total_ram_bytes=total,
        available_ram_bytes=available,
        logical_cpus=logical_cpus,
    )


# ----------------------------------------------------------------- F-21 ---


@pytest.mark.parametrize(
    ("total_gib", "expected_gib"),
    [(8, 2), (16, 4), (24, 6), (64, 6), (4, 2)],
)
def test_the_default_memory_budget_is_a_quarter_of_ram_held_to_two_and_six_gib(
    total_gib: float, expected_gib: float
) -> None:
    assert default_memory_bytes(int(total_gib * GIB)) == int(expected_gib * GIB)


@pytest.mark.parametrize(
    ("total_gib", "expected_gib"),
    [(8, 4), (16, 8), (64, 32), (128, 32)],
)
def test_the_configurable_ceiling_is_the_lesser_of_32_gib_and_half_of_ram(
    total_gib: float, expected_gib: float
) -> None:
    assert memory_ceiling_bytes(int(total_gib * GIB)) == int(expected_gib * GIB)


def test_a_configured_budget_above_the_machines_ceiling_is_brought_down() -> None:
    asked = Settings(memory_bytes=30 * GIB)
    assert _resolve(asked, total_gib=16).memory_bytes == 8 * GIB


def test_the_applied_budget_falls_below_the_configured_one_when_ram_is_short() -> None:
    """F-21: the value actually applied may be lower than the setting."""
    settings = Settings(memory_bytes=6 * GIB)
    budget = _resolve(settings, total_gib=16, available_gib=5)
    assert budget.memory_bytes == 5 * GIB - system_headroom_bytes(16 * GIB)
    assert budget.memory_bytes < settings.memory_bytes


def test_a_plentiful_machine_applies_exactly_what_was_configured() -> None:
    assert _resolve(Settings(memory_bytes=5 * GIB), total_gib=32).memory_bytes == 5 * GIB


# ----------------------------------------------------------------- N-04 ---


@pytest.mark.parametrize(
    ("total_gib", "expected_gib"),
    [(4, 1), (8, 1), (16, 1.6), (64, 6.4)],
)
def test_headroom_is_the_greater_of_a_tenth_of_ram_and_one_gib(
    total_gib: float, expected_gib: float
) -> None:
    assert system_headroom_bytes(int(total_gib * GIB)) == int(expected_gib * GIB)


def test_the_budget_never_eats_the_reserved_headroom() -> None:
    total, available = 16 * GIB, 7 * GIB
    budget = _resolve(Settings(memory_bytes=6 * GIB), total_gib=16, available_gib=7)
    assert budget.memory_bytes + system_headroom_bytes(total) <= available


# ----------------------------------------------------------------- F-23 ---


def test_a_machine_that_cannot_spare_two_gib_refuses_to_load_the_model() -> None:
    with pytest.raises(EchoActError) as caught:
        _resolve(total_gib=16, available_gib=3)
    error = caught.value
    assert error.code is Code.INSUFFICIENT_RESOURCES
    assert error.detail["required_bytes"] == MEMORY_FLOOR_BYTES
    assert error.retryable is True
    assert error.retry_after_s is not None


def test_a_machine_too_small_for_a_two_gib_ceiling_refuses_as_well() -> None:
    """Half of 3 GiB is under F-23's floor, so no setting can rescue it."""
    with pytest.raises(EchoActError) as caught:
        _resolve(Settings(memory_bytes=32 * GIB), total_gib=3)
    assert caught.value.code is Code.INSUFFICIENT_RESOURCES


def test_exactly_two_gib_after_headroom_is_allowed() -> None:
    budget = _resolve(total_gib=10, available_gib=3)
    assert budget.memory_bytes == MEMORY_FLOOR_BYTES


# ------------------------------------------------------------ F-20, F-87 ---


@pytest.mark.parametrize(
    ("logical_cpus", "cpu_percent", "expected"),
    [
        (8, 20, 2),  # Section 8.2's baseline, the configuration A.5 measured
        (4, 10, 1),
        (4, 70, 3),
        (8, 70, 4),
        (20, 20, 4),
        (64, 70, MAX_INTRA_OP_THREADS),
        (10, 25, 3),  # half-up, not Python's banker's rounding
    ],
)
def test_thread_count_comes_from_the_cpu_budget(
    logical_cpus: int, cpu_percent: int, expected: int
) -> None:
    assert intra_op_threads(logical_cpus, cpu_percent) == expected


def test_more_cpu_never_asks_for_more_threads_than_measurement_supports() -> None:
    """A.5: twenty threads ran about twice as slowly as two."""
    biggest = max(
        intra_op_threads(cpus, pct) for cpus in (4, 8, 20, 64, 256) for pct in (10, 20, 50, 70)
    )
    assert biggest == MAX_INTRA_OP_THREADS


def test_the_budget_always_carries_a_thread_count_of_its_own() -> None:
    budget = _resolve(logical_cpus=BASELINE_CPUS)
    assert budget.intra_op_threads == 2
    assert budget.inter_op_threads == 1


def test_a_cpu_percentage_outside_f20s_range_is_clamped_not_honoured() -> None:
    assert _resolve(Settings(cpu_percent=500)).cpu_percent == 70
    assert _resolve(Settings(cpu_percent=0)).cpu_percent == 10


def test_an_impossible_machine_description_is_a_programming_error() -> None:
    with pytest.raises(AssertionError):
        resolve_budget(Settings(), total_ram_bytes=0, available_ram_bytes=0, logical_cpus=4)


def test_resolving_against_the_real_machine_agrees_with_the_explicit_call() -> None:
    vm = psutil.virtual_memory()
    settings = Settings()
    try:
        from_system = resolve_budget_from_system(settings)
    except EchoActError as exc:  # a genuinely loaded machine
        assert exc.code is Code.INSUFFICIENT_RESOURCES
        return
    explicit = resolve_budget(
        settings,
        total_ram_bytes=int(vm.total),
        available_ram_bytes=int(vm.available),
        logical_cpus=psutil.cpu_count(logical=True) or 1,
    )
    assert from_system.cpu_percent == explicit.cpu_percent
    assert from_system.intra_op_threads == explicit.intra_op_threads


# ------------------------------------------------------ sampling, N-21/N-04 ---


class _FakeProcess:
    """The two psutil calls the sampler makes, and nothing else."""

    def __init__(self, rss: int, cpu: float) -> None:
        self.rss = rss
        self.cpu = cpu
        self.gone = False

    def memory_info(self) -> object:
        if self.gone:
            raise psutil.NoSuchProcess(1234)
        return type("mem", (), {"rss": self.rss})()

    def cpu_percent(self, interval: float | None = None) -> float:
        if self.gone:
            raise psutil.NoSuchProcess(1234)
        return self.cpu


def _sampler(process: _FakeProcess, clock: list[float]) -> ResourceSampler:
    return ResourceSampler(
        pid=1234,
        logical_cpus=8,
        process=process,
        memory_probe=lambda: (16 * GIB, 9 * GIB),
        clock=lambda: clock[0],
    )


def test_cpu_is_reported_against_total_logical_capacity() -> None:
    """Section 4.1 displays CPU with total logical capacity as 100%, while
    psutil counts one core as 100%."""
    clock = [0.0]
    sampler = _sampler(_FakeProcess(rss=600_000_000, cpu=200.0), clock)
    sample = sampler.sample()
    assert sample is not None
    assert sample.cpu_percent == 25.0
    assert sample.rss_bytes == 600_000_000


def test_sampling_follows_the_half_second_cadence() -> None:
    clock = [0.0]
    sampler = _sampler(_FakeProcess(rss=1, cpu=0.0), clock)
    assert sampler.due() is True
    assert sampler.poll() is not None

    clock[0] = 0.3
    assert sampler.due() is False
    assert sampler.poll() is None

    clock[0] = 0.5
    assert sampler.due() is True
    assert sampler.poll() is not None


def test_a_worker_that_has_gone_stops_being_measured_rather_than_raising() -> None:
    """N-22 kills the worker mid-segment on cancellation; that is normal."""
    clock = [0.0]
    process = _FakeProcess(rss=1, cpu=0.0)
    sampler = _sampler(process, clock)
    assert sampler.sample() is not None

    process.gone = True
    assert sampler.sample() is None
    assert sampler.alive is False


def test_the_sampler_measures_the_worker_and_not_the_whole_app() -> None:
    """N-21 separates the generation job from the GUI, database, and servers."""
    sampler = ResourceSampler(pid=4321, process=_FakeProcess(rss=7, cpu=0.0), logical_cpus=1)
    assert sampler.pid == 4321


def test_a_sampler_pointed_at_a_dead_pid_is_simply_not_alive() -> None:
    dead = 0x7FFFFFFF  # far above any pid this OS will have issued
    sampler = ResourceSampler(pid=dead)
    if sampler.alive:  # pragma: no cover - only if the pid happens to exist
        pytest.skip("pid unexpectedly in use")
    assert sampler.sample() is None


# ----------------------------------------------------- halting, F-23 ------


def _sample(rss_gib: float, available_gib: float, total_gib: float = 16) -> ResourceSample:
    return ResourceSample(
        rss_bytes=int(rss_gib * GIB),
        cpu_percent=12.5,
        system_total_bytes=int(total_gib * GIB),
        system_available_bytes=int(available_gib * GIB),
        at=1.0,
    )


def _budget(memory_gib: float = 4) -> Budget:
    return Budget(cpu_percent=20, memory_bytes=int(memory_gib * GIB), intra_op_threads=2)


def test_a_job_inside_its_budget_keeps_running() -> None:
    assert halt_decision(_sample(1.0, available_gib=8), _budget()).should_halt is False


def test_a_job_over_its_memory_limit_is_halted_with_the_reason() -> None:
    decision = halt_decision(_sample(4.5, available_gib=8), _budget())
    assert decision.should_halt is True
    assert decision.reason is HaltReason.BUDGET_EXCEEDED
    error = decision.to_error()
    assert error.code is Code.OUT_OF_MEMORY
    assert error.detail["reason"] == HaltReason.BUDGET_EXCEEDED.value
    assert error.detail["limit_bytes"] == 4 * GIB


def test_a_job_within_budget_is_still_halted_when_the_host_runs_short() -> None:
    """N-04's headroom is the host's, not the job's."""
    decision = halt_decision(_sample(1.0, available_gib=1.0), _budget())
    assert decision.should_halt is True
    assert decision.reason is HaltReason.SYSTEM_HEADROOM
    assert decision.to_error().detail["headroom_bytes"] == system_headroom_bytes(16 * GIB)


def test_a_cpu_spike_alone_never_halts_a_job() -> None:
    """N-03 makes CPU the OS container's to throttle, and N-04 concedes that
    momentary spikes between samples are not prevented."""
    busy = ResourceSample(
        rss_bytes=GIB,
        cpu_percent=99.0,
        system_total_bytes=16 * GIB,
        system_available_bytes=8 * GIB,
        at=1.0,
    )
    assert halt_decision(busy, _budget()).should_halt is False


def test_a_decision_to_continue_has_no_error_to_offer() -> None:
    with pytest.raises(AssertionError):
        halt_decision(_sample(1.0, available_gib=8), _budget()).to_error()
