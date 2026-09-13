"""F-89's playback, kept away from the reading surface.

A client asks for its job to be spoken aloud; this plays it and touches
nothing else.  That "nothing else" is the requirement rather than tidiness:
F-51 keeps an external request from overwriting the input or taking over the
user's playback, and F-89 narrows what it may do to the speaker alone.  So
this object owns a timeline of its own and never looks at the input text,
the snapshot, or the highlight.

It shares the one :class:`~echoact.audio.player.Player`, because there is
one output device and two streams would talk over each other.  The owner
always wins that share: :meth:`yield_to_owner` is called before the window
starts anything of its own, and a request arriving while the owner is
listening was already refused by
:class:`~echoact.playrequests.PlayRequests` before it ever reached here.

Playback follows generation, as F-12 describes for the person at the
keyboard: the first ready segment starts the audio and later ones extend it,
so "say this" is heard while the rest is still being made.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..app import Application
from ..audio.player import PlayerState, Timeline, append_segment, timeline_from_segments
from ..domain import JobState
from ..errors import EchoActError
from ..jobs.engine import Event
from ..models.catalog import MANIFEST
from ..playrequests import PlayRequest
from ..util.logging import get_logger

log = get_logger("ui.external_play")


class ExternalPlayback(QObject):
    """The window's hand for F-89: one external job, playing or not."""

    #: Emitted whenever the banner should be redrawn -- started, stopped, or
    #: finished.  Carries nothing: the window reads the properties.
    changed = Signal()
    #: An :class:`~echoact.errors.EchoActError` the client will never see,
    #: because it was told its request was accepted.  F-89 still requires the
    #: failure to be reported, so it is reported here, to the owner.
    failed = Signal(object)

    def __init__(self, app: Application, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._job_id: str | None = None
        self._client: str | None = None
        self._timeline: Timeline | None = None
        self._started = False
        self._last_index = -1

    # ------------------------------------------------------------------
    # What the window asks
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """True while an external job holds the player.

        The window consults this before it moves its own highlight: the
        player's position then belongs to text that is not on screen, and
        F-89 requires the highlight to be reported unavailable rather than
        pointed at the wrong characters.
        """
        return self._job_id is not None

    @property
    def job_id(self) -> str | None:
        return self._job_id

    @property
    def client_label(self) -> str | None:
        return self._client

    # ------------------------------------------------------------------
    # The request
    # ------------------------------------------------------------------

    def begin(self, request: PlayRequest) -> None:
        """Take the player for this job.  Already on the GUI thread."""
        try:
            job = self.app.store.get_job(request.job_id, include_source_text=False)
        except EchoActError as exc:
            log.warning("play request for %s: %s", request.job_id, exc.code.value)
            return

        rate = MANIFEST.get(job.settings.model_id).sample_rate
        self._timeline = timeline_from_segments(
            rate, job.segments, complete=job.state is JobState.COMPLETE
        )
        self._job_id = job.job_id
        self._client = request.client_label
        self._started = False
        self._last_index = (
            self._timeline.entries[-1].segment_index if self._timeline.entries else -1
        )
        self.app.play_requests.took(job.job_id)
        self.app.player.load(self._timeline)
        self._start_if_possible()
        self.changed.emit()

    def on_segment(self, event: Event) -> None:
        """Extend the timeline as the external job generates.

        A segment already on the timeline is ignored rather than appended
        twice.  It can happen: the store is written before the event is
        emitted, so a request that arrived in between built its timeline
        from a row whose event was still on its way, and appending it again
        would play that sentence twice.
        """
        if event.job_id != self._job_id or self._timeline is None:
            return
        index = event.segment_index
        if index is None or index <= self._last_index:
            return
        for segment in self.app.store.list_segments(event.job_id, ready_only=True):
            if segment.index == index:
                if append_segment(self._timeline, segment) is not None:
                    self._last_index = index
                break
        self.app.player.timeline_grew()
        self._start_if_possible()

    def on_finished(self, event: Event) -> None:
        if event.job_id != self._job_id or self._timeline is None:
            return
        self._timeline.complete = True

    def poll(self) -> None:
        """Notice that the audio ran out.  Called from the window's tick.

        The player has no callback for "ended" that reaches Qt safely, and
        the window already polls it for the position, so the end of external
        playback is noticed on the same beat rather than on a timer of its
        own.
        """
        if self._job_id is None:
            return
        if self.app.player.state is PlayerState.ENDED:
            self._release()

    # ------------------------------------------------------------------
    # Giving the speaker back
    # ------------------------------------------------------------------

    def yield_to_owner(self) -> None:
        """The owner is about to use the player, so stop and let go.

        F-50 gives the owner the slot by asking for it, and the speaker
        follows the same rule: nothing here waits for the external job or
        asks the client first.  The job itself is untouched -- F-13 keeps
        playback and generation separate -- so the client can still fetch
        the audio it asked for.
        """
        if self._job_id is None:
            return
        self.app.player.stop()
        self._release()

    def _release(self) -> None:
        self._job_id = None
        self._client = None
        self._timeline = None
        self._started = False
        self._last_index = -1
        self.app.play_requests.took(None)
        self.changed.emit()

    # ------------------------------------------------------------------

    def _start_if_possible(self) -> None:
        """Start at the first ready segment, and only once.

        ``Player.play`` does nothing on an empty timeline, so a request for
        a job that has generated nothing yet is not an error here: it waits,
        and the next segment starts it.
        """
        if self._started or self._timeline is None or not self._timeline.entries:
            return
        try:
            self.app.player.play()
        except EchoActError as exc:
            # F-89: no output device means generation still succeeded and
            # only playback failed.  The client is not told -- it was told
            # the request was accepted -- so this is the app's own notice.
            log.warning("external playback could not start: %s", exc.code.value)
            self._release()
            self.failed.emit(exc)
            return
        self._started = True
