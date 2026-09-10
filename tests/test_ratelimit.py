"""Section 4.1's request limits, plus F-57's retry hint and N-23's no-queue rule."""

from __future__ import annotations

import threading
import time

import pytest

from echoact.errors import Code, EchoActError
from echoact.policy import (
    AUTH_FAILURES_PER_MIN,
    AUTH_LOCKOUT_S,
    RATE_GENERATION_PER_MIN,
    RATE_OTHER_PER_MIN,
)
from echoact.security.ratelimit import (
    WINDOW_S,
    AuthFailureLimiter,
    RateLimiter,
    RequestClass,
)


class FakeClock:
    """A monotonic clock the test drives, so a window can be crossed exactly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(clock):
    return RateLimiter(clock=clock)


@pytest.fixture
def auth(clock):
    return AuthFailureLimiter(clock=clock)


# -- 4.1: per-client request rates ----------------------------------------


def test_a_client_gets_ten_generation_requests_a_minute(limiter):
    for i in range(RATE_GENERATION_PER_MIN):
        decision = limiter.check("cli_a", RequestClass.GENERATION)
        assert decision.allowed, f"request {i} should be inside the allowance"
        assert decision.remaining == RATE_GENERATION_PER_MIN - 1 - i
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is False


def test_other_requests_have_their_own_larger_allowance(limiter):
    for _ in range(RATE_GENERATION_PER_MIN):
        limiter.check("cli_a", RequestClass.GENERATION)
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is False
    # 4.1 states the two limits separately; generation must not spend the other.
    for _ in range(RATE_OTHER_PER_MIN):
        assert limiter.check("cli_a", RequestClass.OTHER).allowed
    assert limiter.check("cli_a", RequestClass.OTHER).allowed is False


def test_the_limits_are_per_client(limiter):
    for _ in range(RATE_GENERATION_PER_MIN):
        limiter.check("cli_a", RequestClass.GENERATION)
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is False
    assert limiter.check("cli_b", RequestClass.GENERATION).allowed is True


def test_the_window_slides_rather_than_resetting_on_the_minute(limiter, clock):
    for _ in range(RATE_GENERATION_PER_MIN):
        limiter.check("cli_a", RequestClass.GENERATION)
        clock.advance(1.0)
    # Ten requests over ten seconds: the first leaves the window 60 s after it.
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is False
    clock.advance(WINDOW_S - RATE_GENERATION_PER_MIN)
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is True
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is False


def test_a_rejection_carries_the_time_the_window_frees_a_slot(limiter, clock):
    for _ in range(RATE_GENERATION_PER_MIN):
        limiter.check("cli_a", RequestClass.GENERATION)
    clock.advance(20.0)
    decision = limiter.check("cli_a", RequestClass.GENERATION)
    assert decision.allowed is False
    # F-57: computed from the window, not invented.
    assert decision.retry_after_s == pytest.approx(WINDOW_S - 20.0)
    clock.advance(decision.retry_after_s)
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is True


def test_the_rejection_is_reported_as_a_retryable_rate_limit(limiter):
    for _ in range(RATE_GENERATION_PER_MIN):
        limiter.check("cli_a", RequestClass.GENERATION)
    with pytest.raises(EchoActError) as caught:
        limiter.raise_if_limited("cli_a", RequestClass.GENERATION)
    error = caught.value
    assert error.code is Code.RATE_LIMITED
    assert error.retryable is True
    assert error.retry_after_s == pytest.approx(WINDOW_S)
    assert error.to_payload("req_1")["retry_after_s"] == pytest.approx(WINDOW_S)


def test_a_rejected_request_is_refused_immediately_and_never_queued(limiter):
    for _ in range(RATE_GENERATION_PER_MIN):
        limiter.check("cli_a", RequestClass.GENERATION)
    started = time.monotonic()
    for _ in range(200):
        assert limiter.check("cli_a", RequestClass.GENERATION).allowed is False
    # N-23: nothing waits for a slot; 200 refusals cost microseconds each.
    assert time.monotonic() - started < 1.0


def test_a_refused_request_does_not_consume_the_allowance_it_was_refused_by(limiter, clock):
    for _ in range(RATE_GENERATION_PER_MIN):
        limiter.check("cli_a", RequestClass.GENERATION)
    for _ in range(50):
        limiter.check("cli_a", RequestClass.GENERATION)
    clock.advance(WINDOW_S + 0.1)
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is True


def test_peeking_does_not_spend_the_allowance(limiter):
    assert limiter.peek("cli_a", RequestClass.GENERATION).remaining == RATE_GENERATION_PER_MIN
    assert limiter.peek("cli_a", RequestClass.GENERATION).remaining == RATE_GENERATION_PER_MIN
    limiter.check("cli_a", RequestClass.GENERATION)
    assert limiter.peek("cli_a", RequestClass.GENERATION).remaining == (
        RATE_GENERATION_PER_MIN - 1
    )


def test_counting_is_exact_under_concurrent_requests(limiter):
    allowed: list[bool] = []
    guard = threading.Lock()
    barrier = threading.Barrier(10)

    def worker() -> None:
        barrier.wait()
        decision = limiter.check("cli_a", RequestClass.GENERATION)
        with guard:
            allowed.append(decision.allowed)

    threads = [threading.Thread(target=worker) for _ in range(10 * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sum(allowed) == RATE_GENERATION_PER_MIN


# -- bounded memory --------------------------------------------------------


def test_expired_buckets_are_reclaimed(limiter, clock):
    for i in range(50):
        limiter.check(f"cli_{i}", RequestClass.OTHER)
    assert limiter.tracked_clients == 50
    clock.advance(WINDOW_S + 1.0)
    assert limiter.sweep() == 50
    assert limiter.tracked_clients == 0


def test_a_varied_client_id_cannot_grow_the_table_without_bound(clock):
    limiter = RateLimiter(clock=clock, max_clients=8)
    for i in range(1000):
        limiter.check(f"cli_{i}", RequestClass.OTHER)
    assert limiter.tracked_clients <= 8


def test_at_the_cap_an_unknown_client_is_refused_with_a_usable_hint(clock):
    limiter = RateLimiter(clock=clock, max_clients=2)
    limiter.check("cli_a", RequestClass.OTHER)
    limiter.check("cli_b", RequestClass.OTHER)

    clock.advance(10.0)
    decision = limiter.check("cli_c", RequestClass.OTHER)
    assert decision.allowed is False, "admitting it untracked would make the cap a bypass"
    assert decision.retry_after_s == pytest.approx(WINDOW_S - 10.0)
    # The clients already inside the table keep their own allowance.
    assert limiter.check("cli_a", RequestClass.OTHER).allowed is True

    clock.advance(decision.retry_after_s)
    assert limiter.check("cli_c", RequestClass.OTHER).allowed is True


def test_forgetting_a_revoked_client_frees_its_bucket(limiter):
    limiter.check("cli_a", RequestClass.GENERATION)
    assert limiter.tracked_clients == 1
    limiter.forget("cli_a")
    assert limiter.tracked_clients == 0


# -- 4.1: authentication failures -----------------------------------------


def test_ten_failures_in_a_minute_block_that_origin_for_sixty_seconds(auth, clock):
    for i in range(AUTH_FAILURES_PER_MIN - 1):
        state = auth.record_failure("127.0.0.1")
        assert state.locked is False
        assert state.failures == i + 1
    state = auth.record_failure("127.0.0.1")
    assert state.locked is True
    assert state.retry_after_s == pytest.approx(AUTH_LOCKOUT_S)
    assert auth.check("127.0.0.1").locked is True

    clock.advance(AUTH_LOCKOUT_S - 0.001)
    assert auth.check("127.0.0.1").locked is True
    clock.advance(0.002)
    assert auth.check("127.0.0.1").locked is False


def test_the_lockout_is_reported_as_a_retryable_error_with_the_time_left(auth, clock):
    for _ in range(AUTH_FAILURES_PER_MIN):
        auth.record_failure("127.0.0.1")
    clock.advance(15.0)
    with pytest.raises(EchoActError) as caught:
        auth.raise_if_locked("127.0.0.1")
    error = caught.value
    assert error.code is Code.AUTH_LOCKED_OUT
    assert error.retry_after_s == pytest.approx(AUTH_LOCKOUT_S - 15.0)


def test_failures_spread_over_more_than_a_minute_do_not_lock(auth, clock):
    for _ in range(AUTH_FAILURES_PER_MIN * 2):
        assert auth.record_failure("127.0.0.1").locked is False
        clock.advance(WINDOW_S / (AUTH_FAILURES_PER_MIN - 1))


def test_a_locked_out_origin_cannot_extend_its_own_lockout(auth, clock):
    for _ in range(AUTH_FAILURES_PER_MIN):
        auth.record_failure("127.0.0.1")
    clock.advance(30.0)
    for _ in range(100):
        auth.record_failure("127.0.0.1")
    assert auth.check("127.0.0.1").retry_after_s == pytest.approx(AUTH_LOCKOUT_S - 30.0)


def test_the_window_starts_clean_after_a_lockout_ends(auth, clock):
    for _ in range(AUTH_FAILURES_PER_MIN):
        auth.record_failure("127.0.0.1")
    clock.advance(AUTH_LOCKOUT_S + 1.0)
    assert auth.record_failure("127.0.0.1").locked is False, "one mistake must not re-lock"


def test_a_successful_authentication_clears_the_failures(auth):
    for _ in range(AUTH_FAILURES_PER_MIN - 1):
        auth.record_failure("127.0.0.1")
    auth.record_success("127.0.0.1")
    assert auth.check("127.0.0.1").failures == 0
    assert auth.record_failure("127.0.0.1").locked is False


def test_a_lockout_stops_authentication_and_nothing_else(auth, limiter):
    for _ in range(AUTH_FAILURES_PER_MIN):
        auth.record_failure("127.0.0.1")
    # 4.1: the local service as a whole and the GUI are not terminated, so
    # every other origin and every already-authenticated client is untouched.
    assert auth.check("127.0.0.2").locked is False
    assert limiter.check("cli_a", RequestClass.GENERATION).allowed is True
    assert limiter.check("cli_a", RequestClass.OTHER).allowed is True


def test_an_origin_table_cannot_grow_without_bound(clock):
    auth = AuthFailureLimiter(clock=clock, max_origins=8)
    for i in range(1000):
        auth.record_failure(f"origin-{i}")
    assert auth.tracked_origins <= 8


def test_locking_an_origin_out_does_not_buy_it_an_uncapped_table_entry(clock):
    """The shape one failure per origin never reaches: every origin locks.

    Ten failures move an origin from the failure table to the lockout table.
    If only the failure table is capped, an inventor of origins pays ten
    attempts for a table entry nothing ever bounds.
    """
    auth = AuthFailureLimiter(clock=clock, max_origins=4)
    for i in range(500):
        for _ in range(AUTH_FAILURES_PER_MIN):
            auth.record_failure(f"origin-{i}")
    assert auth.tracked_origins <= 4


def test_sweep_reports_exactly_the_origins_it_reclaimed(auth, clock):
    for i in range(3):
        for _ in range(AUTH_FAILURES_PER_MIN):
            auth.record_failure(f"origin-{i}")
    assert auth.tracked_origins == 3, "a locked origin is still a tracked origin"

    # Mid-lockout there is nothing to reclaim: a locked origin's spent
    # failure window is not free space, or F-69's screen is told the table
    # emptied while every one of those origins is still locked out.
    assert auth.sweep() == 0
    assert auth.tracked_origins == 3
    assert auth.check("origin-0").locked is True

    clock.advance(AUTH_LOCKOUT_S + 1.0)
    assert auth.sweep() == 3
    assert auth.tracked_origins == 0


def test_a_full_table_of_lockouts_still_says_when_to_come_back(clock):
    auth = AuthFailureLimiter(clock=clock, max_origins=1)
    for _ in range(AUTH_FAILURES_PER_MIN):
        auth.record_failure("127.0.0.1")
    clock.advance(20.0)

    state = auth.record_failure("10.0.0.9")
    assert state.locked is True
    # The one tracked origin is locked out, not counting failures, so the
    # slot frees when its lockout ends -- not at some default minute.
    assert state.retry_after_s == pytest.approx(AUTH_LOCKOUT_S - 20.0)


def test_at_the_origin_cap_an_attempt_is_refused_rather_than_forgotten(clock):
    auth = AuthFailureLimiter(clock=clock, max_origins=1)
    auth.record_failure("127.0.0.1")
    state = auth.record_failure("10.0.0.9")
    assert state.locked is True, "evicting would let an attacker clear a lockout"
    assert 0.0 < state.retry_after_s <= WINDOW_S
    assert auth.check("127.0.0.1").failures == 1, "the tracked origin keeps its count"


def test_lapsed_origins_are_reclaimed(auth, clock):
    for i in range(20):
        auth.record_failure(f"origin-{i}")
    assert auth.tracked_origins == 20
    clock.advance(WINDOW_S + 1.0)
    assert auth.sweep() == 20
    assert auth.tracked_origins == 0


def test_failure_counting_is_exact_under_concurrent_attempts(auth):
    barrier = threading.Barrier(10)
    states: list[bool] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        locked = auth.record_failure("127.0.0.1").locked
        with guard:
            states.append(locked)

    threads = [threading.Thread(target=worker) for _ in range(10 * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert states.count(False) == AUTH_FAILURES_PER_MIN - 1
