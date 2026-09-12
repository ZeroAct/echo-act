"""F-89's one crossing: a client asks, and the window plays.

The request arrives on a service worker thread and has to be answered
there -- 403 when the owner turned external playback off, 409 while the
owner is listening to something else -- because an HTTP status cannot be
sent after the fact.  The *playing* then has to happen on the GUI thread,
which is where the player, the transport, and the notice live.  So this
holds the decision and hands over the request, and nothing else.

It looks like :class:`~echoact.jobs.engine.JobEngine`'s listener on
purpose: same subscribe-and-unsubscribe shape, same rule that a listener
runs on the caller's thread and must marshal before touching a widget.
One pattern for "news crossing a thread" is easier to keep right than two.

No Qt import here, for the same reason ``app.py`` has none: the service
and this object must work in a headless test.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .audio.player import Player, PlayerState
from .config.settings import Settings
from .errors import Code, EchoActError
from .policy import PLAYBACK_BUSY_RETRY_AFTER_S
from .util.logging import get_logger

log = get_logger("playrequests")

#: Player states that mean sound is coming out, or is about to.
_AUDIBLE = (PlayerState.PLAYING, PlayerState.WAITING)


@dataclass(frozen=True, slots=True)
class PlayRequest:
    """Who asked for what.  No body text and no path (N-20)."""

    job_id: str
    client_label: str | None = None


class PlayRequests:
    """The gate and the hand-off for F-89."""

    def __init__(self, player: Player, settings: Settings) -> None:
        self._player = player
        self._external_play = settings.external_play
        self._current: str | None = None
        self._listeners: list[Callable[[PlayRequest], None]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Owner-side state
    # ------------------------------------------------------------------

    def apply_settings(self, settings: Settings) -> None:
        self._external_play = settings.external_play

    @property
    def allowed(self) -> bool:
        return self._external_play

    @property
    def current(self) -> str | None:
        """The external job the window is playing, if any."""
        with self._lock:
            return self._current

    def took(self, job_id: str | None) -> None:
        """The window is now playing this external job, or nothing.

        Without it, ``request`` could not tell the owner's own playback
        from playback it started itself a moment ago -- and F-89 refuses
        only the first.
        """
        with self._lock:
            self._current = job_id

    # ------------------------------------------------------------------
    # Client-side request
    # ------------------------------------------------------------------

    def request(self, job_id: str, *, client_label: str | None = None) -> PlayRequest:
        """Accept a play request, or refuse it with F-89's reason.

        Refusing here rather than in the window is what lets the service
        answer 403 or 409: by the time the GUI thread saw the request the
        response would already have been sent.
        """
        if not self._external_play:
            raise EchoActError(
                Code.PLAYBACK_NOT_ALLOWED,
                "EchoAct is not set to play requests from an app out loud. "
                "The owner can turn that on under Settings.",
            )
        with self._lock:
            owner_is_listening = self._current is None and self._player.state in _AUDIBLE
            if owner_is_listening:
                # F-50: the owner is never preempted.  An external request
                # already playing is a different matter -- the newer request
                # takes the speaker from the older one, so that two "say
                # this" calls in a row behave the way they read.
                raise EchoActError(
                    Code.PLAYBACK_BUSY,
                    "Someone is listening to something else in EchoAct right now.",
                    retry_after_s=PLAYBACK_BUSY_RETRY_AFTER_S,
                )
        request = PlayRequest(job_id=job_id, client_label=client_label)
        self._emit(request)
        return request

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def listen(self, fn: Callable[[PlayRequest], None]) -> Callable[[], None]:
        """Subscribe.  Returns an unsubscribe callable.

        The listener runs on the requesting thread, so a GUI listener must
        marshal to the main thread rather than touch a widget here.
        """
        self._listeners.append(fn)

        def off() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return off

    def _emit(self, request: PlayRequest) -> None:
        for fn in list(self._listeners):
            try:
                fn(request)
            except Exception as exc:  # noqa: BLE001 - a listener must not fail the request
                log.warning("play listener failed: %s", type(exc).__name__)

