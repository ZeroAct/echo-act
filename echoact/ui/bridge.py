"""Getting engine and client news onto the GUI thread.

The job engine runs a job on its own thread and calls listeners from there.
Qt widgets may only be touched from the thread that created them, so every
event crosses here and nowhere else: this object is created on the main
thread, so emitting one of its signals from a worker thread is a queued
connection and Qt does the hand-off.

Kept separate from the window so the rule has a name.  A listener that
touched a widget directly would work most of the time, which is the worst
possible failure mode for a threading bug.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..jobs.engine import Event, EventKind, JobEngine
from ..playrequests import PlayRequest, PlayRequests


class EngineBridge(QObject):
    """Re-emits :class:`~echoact.jobs.engine.Event` as Qt signals."""

    accepted = Signal(object)
    state_changed = Signal(object)
    segment_ready = Signal(object)
    finished = Signal(object)
    #: Anything at all, for a view that wants the raw stream.
    any_event = Signal(object)

    def __init__(self, engine: JobEngine, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._off = engine.listen(self._on_event)

    def _on_event(self, event: Event) -> None:
        # Runs on the job thread.  Nothing below may touch a widget; these
        # emits are queued because this object lives on the main thread.
        self.any_event.emit(event)
        if event.kind is EventKind.ACCEPTED:
            self.accepted.emit(event)
        elif event.kind is EventKind.STATE:
            self.state_changed.emit(event)
        elif event.kind is EventKind.SEGMENT:
            self.segment_ready.emit(event)
        elif event.kind is EventKind.FINISHED:
            self.finished.emit(event)

    def detach(self) -> None:
        self._off()


class PlayRequestBridge(QObject):
    """The same hand-off for F-89's play requests.

    A client's request arrives on a service worker thread; the player, the
    transport, and the banner are the main thread's. So it crosses here,
    beside the engine's events, because "news crossing a thread" belongs in
    one file whatever the news is.
    """

    requested = Signal(object)

    def __init__(self, requests: PlayRequests, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._requests = requests
        self._off = requests.listen(self._on_request)

    def _on_request(self, request: PlayRequest) -> None:
        # Runs on the service thread; the emit is queued.
        self.requested.emit(request)

    def detach(self) -> None:
        self._off()
