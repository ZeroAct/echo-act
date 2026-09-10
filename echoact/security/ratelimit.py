"""Section 4.1's request limits, as sliding windows.

Three limits live here, and 4.1 keeps them separate on purpose: 10 generation
requests per minute per client, 120 other requests per minute per client, and
authentication failures, which are counted per connection origin rather than
per client because a failed authentication has not identified a client yet.

Two requirements shape the implementation more than the numbers do.

N-23 forbids an unbounded queue: a request over the limit is refused now, so
nothing here sleeps, blocks, or holds a caller.  Every rejection carries a
retry-after computed from the window -- the moment the oldest counted request
falls out of it, or the moment a lockout ends -- because F-57 promises the
hint exists so a client need not guess a backoff.  A made-up constant would
satisfy the letter and mislead the caller.

Memory is bounded on both axes.  Windows are trimmed on every touch, empty
buckets are dropped, and the number of tracked keys is capped; at the cap a
key that is not already tracked is refused rather than admitted untracked or
swapped in over an existing one.  Untracked admission would make the cap a way
to bypass the limit, and eviction would make it a way to clear one's own
lockout, both by inventing keys.

No web framework is imported: F-69 and F-71 show the same state in the GUI,
and 4.1's authentication lockout must never take the service or the GUI down
with it -- it refuses authentication attempts from one origin and nothing else.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..errors import Code, EchoActError
from ..policy import (
    AUTH_FAILURES_PER_MIN,
    AUTH_LOCKOUT_S,
    RATE_GENERATION_PER_MIN,
    RATE_OTHER_PER_MIN,
)
from ..util.ids import monotonic

#: The "per minute" in every one of policy's ``*_PER_MIN`` figures.  A sliding
#: window rather than a calendar minute: a fixed bucket would let a client
#: spend two full allowances back to back across the boundary.
WINDOW_S: Final = 60.0

#: How many distinct keys each table will hold.  4.1 fixes no figure, because
#: on a loopback-only service the real bound is the number of issued
#: credentials; this is the backstop that keeps the table finite anyway.  Both
#: are far above any plausible legitimate count -- 4.1 allows one credential
#: per integration, not thousands -- so reaching either means something is
#: wrong, and being refused is the right answer to that.
MAX_TRACKED_CLIENTS: Final = 1024
MAX_TRACKED_ORIGINS: Final = 1024

Clock = Callable[[], float]


class RequestClass(StrEnum):
    """4.1 counts generation separately from everything else.

    A generation request consumes only the generation allowance: the two
    limits are stated as separate sentences, and charging a generation request
    to both would silently make the "other" limit 110 for a busy client.
    """

    GENERATION = "generation"
    OTHER = "other"


_LIMITS: Final[dict[RequestClass, int]] = {
    RequestClass.GENERATION: RATE_GENERATION_PER_MIN,
    RequestClass.OTHER: RATE_OTHER_PER_MIN,
}


@dataclass(frozen=True, slots=True)
class Decision:
    """The answer to one rate-limit question.

    ``retry_after_s`` is 0.0 when allowed and strictly positive when not, so a
    caller can pass it straight into F-57's hint without a special case.
    """

    allowed: bool
    request_class: RequestClass
    limit: int
    remaining: int
    retry_after_s: float

    def raise_if_limited(self) -> None:
        if self.allowed:
            return
        raise EchoActError(
            Code.RATE_LIMITED,
            retry_after_s=self.retry_after_s,
            detail={
                "limit": self.limit,
                "window_s": WINDOW_S,
                "request_class": self.request_class.value,
            },
        )


@dataclass(frozen=True, slots=True)
class Lockout:
    """The authentication state of one connection origin (4.1).

    ``locked`` is what blocks an attempt; ``failures`` is how many are inside
    the current window, which F-69's service screen can show without the owner
    having to trigger a lockout to find out.
    """

    locked: bool
    failures: int
    limit: int
    retry_after_s: float

    def raise_if_locked(self) -> None:
        if not self.locked:
            return
        raise EchoActError(
            Code.AUTH_LOCKED_OUT,
            retry_after_s=self.retry_after_s,
            detail={"limit": self.limit, "window_s": WINDOW_S},
        )


class _Window:
    """Timestamps of the events counted in one sliding window."""

    __slots__ = ("_hits",)

    def __init__(self) -> None:
        self._hits: deque[float] = deque()

    def trim(self, now: float) -> None:
        cutoff = now - WINDOW_S
        hits = self._hits
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def __len__(self) -> int:
        return len(self._hits)

    @property
    def empty(self) -> bool:
        return not self._hits

    def add(self, now: float) -> None:
        self._hits.append(now)

    def clear(self) -> None:
        self._hits.clear()

    def retry_after(self, now: float) -> float:
        """When the oldest counted event leaves the window.

        This is the earliest instant at which the same request could succeed,
        which is what F-57's hint is supposed to mean.  An empty window would
        mean the caller was not limited at all, so it answers 0.0.
        """
        if not self._hits:
            return 0.0
        return max(0.0, self._hits[0] + WINDOW_S - now)


class RateLimiter:
    """Per-client request rates (4.1), thread-safe and bounded.

    The REST service serves several threads at once, and 4.1's limits are per
    client rather than per connection, so the counting has to be shared and
    therefore locked.  Every operation is O(expired events) and never waits.
    """

    def __init__(
        self,
        *,
        clock: Clock = monotonic,
        max_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        # A monotonic clock, never the wall clock: a clock adjustment must not
        # extend or erase a limit window (see ``echoact.util.ids``).
        self._clock = clock
        self._max_clients = max_clients
        self._lock = threading.Lock()
        self._windows: dict[str, dict[RequestClass, _Window]] = {}

    @property
    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._windows)

    def check(self, client_id: str, request_class: RequestClass) -> Decision:
        """Count one request and say whether it is allowed.

        Consumes the allowance when it allows.  Call it once per request, at
        the point the request is admitted, so a rejected request costs nothing
        against the caller's own limit.
        """
        return self._evaluate(client_id, request_class, consume=True)

    def peek(self, client_id: str, request_class: RequestClass) -> Decision:
        """The same answer without consuming, for F-69's status screen."""
        return self._evaluate(client_id, request_class, consume=False)

    def raise_if_limited(self, client_id: str, request_class: RequestClass) -> Decision:
        """``check`` that raises RATE_LIMITED with F-57's hint."""
        decision = self.check(client_id, request_class)
        decision.raise_if_limited()
        return decision

    def forget(self, client_id: str) -> None:
        """Drop a client's windows, e.g. once its credential is revoked."""
        with self._lock:
            self._windows.pop(client_id, None)

    def sweep(self) -> int:
        """Drop clients with nothing counted any more; returns how many.

        The cap already bounds the table, so this is housekeeping rather than
        safety: it keeps ``tracked_clients`` truthful for F-69's screen and
        keeps N-26's eight-hour run free of a table that only ever grows.
        """
        now = self._clock()
        with self._lock:
            stale = [k for k, b in self._windows.items() if _all_expired(b, now)]
            for client_id in stale:
                del self._windows[client_id]
            return len(stale)

    def _evaluate(self, client_id: str, request_class: RequestClass, *, consume: bool) -> Decision:
        limit = _LIMITS[request_class]
        now = self._clock()
        with self._lock:
            buckets = self._windows.get(client_id)
            if buckets is None:
                if self._prune_locked(now):
                    # At the cap, an unknown client is refused rather than
                    # admitted untracked, which would make the cap a bypass.
                    return Decision(
                        allowed=False,
                        request_class=request_class,
                        limit=limit,
                        remaining=0,
                        retry_after_s=self._soonest_free_locked(now),
                    )
                if not consume:
                    # A peek must not create the bucket it is asking about, or
                    # F-69's screen would fill the table by drawing itself.
                    return Decision(
                        allowed=True,
                        request_class=request_class,
                        limit=limit,
                        remaining=limit,
                        retry_after_s=0.0,
                    )
                buckets = {c: _Window() for c in RequestClass}
                self._windows[client_id] = buckets

            window = buckets[request_class]
            window.trim(now)
            if len(window) >= limit:
                return Decision(
                    allowed=False,
                    request_class=request_class,
                    limit=limit,
                    remaining=0,
                    retry_after_s=window.retry_after(now),
                )
            if consume:
                window.add(now)
            return Decision(
                allowed=True,
                request_class=request_class,
                limit=limit,
                remaining=limit - len(window),
                retry_after_s=0.0,
            )

    def _prune_locked(self, now: float) -> bool:
        """Drop clients with nothing left in either window.  True if still full."""
        if len(self._windows) < self._max_clients:
            return False
        for client_id in [k for k, b in self._windows.items() if _all_expired(b, now)]:
            del self._windows[client_id]
        return len(self._windows) >= self._max_clients

    def _soonest_free_locked(self, now: float) -> float:
        """How long until some tracked client's window empties a slot.

        The table cannot stay full longer than one window, so this is a real
        figure rather than a guess, and it is capped at the window for the
        degenerate case where every bucket was just touched.
        """
        soonest = WINDOW_S
        for buckets in self._windows.values():
            for window in buckets.values():
                after = window.retry_after(now)
                if 0.0 < after < soonest:
                    soonest = after
        return soonest


class AuthFailureLimiter:
    """4.1's separate limit on authentication failures, per connection origin.

    Ten failures inside a minute block authentication retries from that origin
    for sixty seconds.  The block is exactly that: the service keeps serving
    every other origin, already-authenticated requests are untouched, and the
    GUI never notices -- 4.1 says the local service as a whole and the GUI are
    not terminated, so a lockout may not be escalated into a shutdown.

    "Origin" is whatever the service can attribute a connection to, normally
    the peer address.  On a loopback-only listener (N-17) that is one value in
    practice, which is the honest reading: every caller is on this machine, so
    the lockout is a global brake on password guessing rather than a way to
    isolate one attacker from another.
    """

    def __init__(
        self,
        *,
        clock: Clock = monotonic,
        max_origins: int = MAX_TRACKED_ORIGINS,
    ) -> None:
        self._clock = clock
        self._max_origins = max_origins
        self._lock = threading.Lock()
        self._failures: dict[str, _Window] = {}
        self._locked_until: dict[str, float] = {}

    @property
    def tracked_origins(self) -> int:
        with self._lock:
            return len(set(self._failures) | set(self._locked_until))

    def sweep(self) -> int:
        """Drop origins with no live failures and no live lockout."""
        now = self._clock()
        with self._lock:
            stale = [o for o, w in self._failures.items() if _window_expired(w, now)]
            for origin in stale:
                del self._failures[origin]
            lapsed = [o for o, until in self._locked_until.items() if until <= now]
            for origin in lapsed:
                del self._locked_until[origin]
                self._failures.pop(origin, None)
            return len(set(stale) | set(lapsed))

    def check(self, origin: str) -> Lockout:
        """Ask whether this origin may attempt authentication at all.

        Call it before verifying a credential.  It counts nothing: a locked-out
        attempt must not extend its own lockout, or an eager client would never
        be let back in.
        """
        now = self._clock()
        with self._lock:
            return self._state_locked(origin, now)

    def raise_if_locked(self, origin: str) -> None:
        """``check`` that raises AUTH_LOCKED_OUT with the remaining time."""
        self.check(origin).raise_if_locked()

    def record_failure(self, origin: str) -> Lockout:
        """Count one authentication failure and report the resulting state.

        The lockout starts at the failure that reaches the limit and runs for
        4.1's sixty seconds from that instant, not from the first failure in
        the window.
        """
        now = self._clock()
        with self._lock:
            state = self._state_locked(origin, now)
            if state.locked:
                return state
            window = self._failures.get(origin)
            if window is None:
                if self._prune_locked(now):
                    # Table full: treat the origin as locked out rather than
                    # untracked.  Refusing an authentication attempt is the
                    # safe direction, and the caller is told when to retry.
                    return Lockout(
                        locked=True,
                        failures=0,
                        limit=AUTH_FAILURES_PER_MIN,
                        retry_after_s=self._soonest_free_locked(now),
                    )
                window = _Window()
                self._failures[origin] = window
            window.add(now)
            if len(window) >= AUTH_FAILURES_PER_MIN:
                self._locked_until[origin] = now + AUTH_LOCKOUT_S
                window.clear()
                return Lockout(
                    locked=True,
                    failures=AUTH_FAILURES_PER_MIN,
                    limit=AUTH_FAILURES_PER_MIN,
                    retry_after_s=AUTH_LOCKOUT_S,
                )
            return Lockout(
                locked=False,
                failures=len(window),
                limit=AUTH_FAILURES_PER_MIN,
                retry_after_s=0.0,
            )

    def record_success(self, origin: str) -> None:
        """Clear the window after an authentication that worked.

        A client that mistypes a credential twice and then presents a valid one
        has demonstrated it is not guessing; carrying those failures forward
        for the rest of the minute would eventually lock out the legitimate
        integration.  It concedes nothing: reaching this point already required
        a valid credential.
        """
        with self._lock:
            self._failures.pop(origin, None)
            self._locked_until.pop(origin, None)

    def forget(self, origin: str) -> None:
        with self._lock:
            self._failures.pop(origin, None)
            self._locked_until.pop(origin, None)

    def _state_locked(self, origin: str, now: float) -> Lockout:
        until = self._locked_until.get(origin)
        if until is not None:
            if now < until:
                return Lockout(
                    locked=True,
                    failures=AUTH_FAILURES_PER_MIN,
                    limit=AUTH_FAILURES_PER_MIN,
                    retry_after_s=until - now,
                )
            # The lockout has run out.  Forget the failures that caused it, or
            # the next single mistake would re-lock the origin immediately.
            del self._locked_until[origin]
            self._failures.pop(origin, None)
        window = self._failures.get(origin)
        if window is None:
            return Lockout(
                locked=False, failures=0, limit=AUTH_FAILURES_PER_MIN, retry_after_s=0.0
            )
        window.trim(now)
        if window.empty:
            del self._failures[origin]
            return Lockout(
                locked=False, failures=0, limit=AUTH_FAILURES_PER_MIN, retry_after_s=0.0
            )
        return Lockout(
            locked=False, failures=len(window), limit=AUTH_FAILURES_PER_MIN, retry_after_s=0.0
        )

    def _prune_locked(self, now: float) -> bool:
        if len(self._failures) < self._max_origins:
            return False
        for origin in [o for o, w in self._failures.items() if _window_expired(w, now)]:
            del self._failures[origin]
        for origin in [o for o, until in self._locked_until.items() if until <= now]:
            del self._locked_until[origin]
            self._failures.pop(origin, None)
        return len(self._failures) >= self._max_origins

    def _soonest_free_locked(self, now: float) -> float:
        soonest = WINDOW_S
        for window in self._failures.values():
            after = window.retry_after(now)
            if 0.0 < after < soonest:
                soonest = after
        return soonest


def _window_expired(window: _Window, now: float) -> bool:
    window.trim(now)
    return window.empty


def _all_expired(buckets: dict[RequestClass, _Window], now: float) -> bool:
    return all(_window_expired(w, now) for w in buckets.values())


__all__ = [
    "MAX_TRACKED_CLIENTS",
    "MAX_TRACKED_ORIGINS",
    "WINDOW_S",
    "AuthFailureLimiter",
    "Decision",
    "Lockout",
    "RateLimiter",
    "RequestClass",
]
