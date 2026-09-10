"""The reading surface: the text, and the mark that says where we are.

The emphasis is a glyph *outline*, applied through a view-level extra
selection.  Both halves of that are forced by requirements rather than
chosen:

* An extra selection never enters the document, so F-29 holds by
  construction -- copying, saving, and regenerating see plain text because
  there is nothing else to see -- and F-30's "must not steal the caret" is
  automatic because an extra selection carries its own cursor.
* An outline is a *painting* attribute, so nothing in the mechanism can
  re-shape a glyph.  N-13's layout stability is therefore a property of the
  mechanism and not of tuning.  ``spikes/outline_emphasis.py`` measured the
  naive alternative failing: bold merged into the document reflows every
  line below the emphasised one, and bold applied through an extra selection
  silently changes nothing at all, at every scale factor and on both
  candidate widgets.

Two subtleties that are easy to get wrong:

* Segment ranges are Unicode code points (4.2); Qt positions are UTF-16
  units.  They differ exactly when the text contains anything outside the
  BMP -- which for this product means emoji, the characters F-27 calls out
  by name.  The conversion happens here and nowhere else.
* The position comes from the audio clock, not from a timer (A.2, N-12).
  This widget is told a millisecond and looks it up; it never asks anyone
  what time it is.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
    QWheelEvent,
)
from PySide6.QtWidgets import QTextEdit, QWidget

from ..domain import Segment
from ..policy import MAX_INPUT_CODEPOINTS
from .theme import METRICS, Palette, reading_font

#: Pen width for the outline, in device-independent units.  The spike found
#: 0.3 to 0.4 reads as emphasis while keeping under one percent of the ink
#: outside the emphasised characters' own cells; below that it is invisible
#: at 100% scaling and above it starts to look like a smear.
OUTLINE_WIDTH = 0.38


class HighlightState:
    """Why the mark looks the way it does.  Section 5.2 distinguishes these
    and F-28 requires waiting to be indicated separately from playing."""

    NONE = "none"
    PLAYING = "playing"
    PAUSED = "paused"
    WAITING = "waiting"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class _Utf16Map:
    """Code point index -> UTF-16 unit index, for one snapshot.

    Built once per snapshot rather than per highlight update: a 50,000
    character document changes highlight several times a second and must not
    pay for a scan each time.  When the text is entirely inside the BMP --
    the common case for Korean and Latin -- the two indices are equal and no
    table is built at all.
    """

    identity: bool
    table: list[int]

    @classmethod
    def build(cls, text: str) -> _Utf16Map:
        if max((ord(c) for c in text), default=0) < 0x10000:
            return cls(identity=True, table=[])
        table: list[int] = [0] * (len(text) + 1)
        pos = 0
        for i, ch in enumerate(text):
            table[i] = pos
            pos += 2 if ord(ch) > 0xFFFF else 1
        table[len(text)] = pos
        return cls(identity=False, table=table)

    def to_utf16(self, cp: int) -> int:
        if self.identity:
            return cp
        if cp <= 0:
            return 0
        if cp >= len(self.table):
            return self.table[-1]
        return self.table[cp]


class ReadingSurface(QTextEdit):
    """The editable input and the reading view, which are the same widget.

    They have to be: F-29 describes the user editing while audio plays, and
    a separate read-only view would either duplicate the text or forbid that.
    """

    #: Emitted when highlight availability changes, so the window can say
    #: "highlighting unavailable" per F-29 and F-31.
    availability_changed = Signal(bool)
    #: Emitted when the user scrolls by hand and following is suspended
    #: (F-30), and again when following resumes.
    following_changed = Signal(bool)
    #: Character count, for F-03's live counter.
    length_changed = Signal(int)
    #: A paste that would exceed the limit: (attempted, room remaining).
    #: F-36 forbids truncating to fit, so the window explains instead.
    paste_refused = Signal(int, int)

    # Class-level defaults, because QTextEdit's constructor delivers a
    # changeEvent before ``__init__`` reaches its own assignments and the
    # override would then read an attribute that does not exist yet.
    _applying_format = False
    _programmatic_scroll = False
    _available = False

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ReadingSurface")
        self.setAcceptRichText(False)  # F-29: nothing but plain text, ever
        self.setFont(reading_font())
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTabChangesFocus(True)  # N-30: Tab must leave the field

        self._palette = palette
        self._snapshot: str | None = None
        self._segments: tuple[Segment, ...] = ()
        # Segments that have a time, as (start_ms, index into _segments).
        # Kept apart from _segments because a segment without audio yet has
        # no place on the timeline, and indexing one list with the other's
        # position is the kind of mistake that silently marks the wrong
        # sentence.
        self._starts: list[int] = []
        self._timed: list[int] = []
        self._map = _Utf16Map(identity=True, table=[])
        self._current: int | None = None
        self._state: str = HighlightState.NONE
        self._available = False
        self._follow = True
        self._following_suspended = False
        self._programmatic_scroll = False
        self._applying_format = False

        doc = self.document()
        doc.setDocumentMargin(0)  # the stylesheet supplies the page padding
        self._apply_line_height()

        self.textChanged.connect(self._on_text_changed)
        # Line height is re-applied only when the paragraph count changes.
        # Doing it on every textChanged would re-enter through the format
        # merge, and on a 50,000 character document would also re-format
        # the whole thing on every keystroke.
        doc.blockCountChanged.connect(self._apply_line_height)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    # -- appearance ----------------------------------------------------

    def set_palette_tokens(self, palette: Palette) -> None:
        """Follow a light/dark switch without losing the current mark."""
        self._palette = palette
        self._repaint_highlight()

    def _apply_line_height(self, _block_count: int = 0) -> None:
        """Give every paragraph the reading line height.

        Merging a block format edits the document, which emits
        ``textChanged``, so this is guarded rather than merely careful: an
        unguarded version recurses until the stack runs out.
        """
        if self._applying_format:
            return
        self._applying_format = True
        try:
            self._merge_line_height()
        finally:
            self._applying_format = False

    def _merge_line_height(self) -> None:
        doc = self.document()
        fmt = QTextBlockFormat()
        fmt.setLineHeight(
            METRICS.reading_line_height_pct,
            QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
        )
        fmt.setBottomMargin(METRICS.reading_paragraph_gap)
        cursor = QTextCursor(doc)
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.mergeBlockFormat(fmt)
        cursor.clearSelection()

        # A blank line between paragraphs is itself a paragraph, so leaving
        # it at the reading leading spends three line heights on one gap.
        # Collapsing it keeps a pasted document looking like prose rather
        # than like a list.
        blank = QTextBlockFormat()
        blank.setLineHeight(
            METRICS.reading_blank_line_height_pct,
            QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
        )
        blank.setBottomMargin(0)
        block = doc.begin()
        while block.isValid():
            if not block.text().strip():
                c = QTextCursor(block)
                c.setBlockFormat(blank)
            block = block.next()

    # -- text ----------------------------------------------------------

    def set_text(self, text: str) -> None:
        """Replace the input.  Used by file opening and by the library; the
        caller has already handled F-36's confirmation."""
        self.setPlainText(text)
        self._apply_line_height()

    def source_text(self) -> str:
        """The text as code points.

        ``toPlainText`` returns exactly what the user typed with paragraph
        separators as newlines, which is the form every offset in this
        product is measured against.
        """
        return self.toPlainText()

    def _on_text_changed(self) -> None:
        if self._applying_format:
            return
        self.length_changed.emit(len(self.toPlainText()))
        self._recheck_availability()

    # -- the job's segment table ---------------------------------------

    def attach_job(self, snapshot: str, segments: list[Segment] | tuple[Segment, ...]) -> None:
        """Bind to a job's frozen source text and its segments.

        F-29 fixes the source text per job.  Everything after this compares
        the live input against ``snapshot``; the widget never re-anchors a
        highlight onto edited text.
        """
        self._snapshot = snapshot
        self._segments = tuple(segments)
        self._map = _Utf16Map.build(snapshot)
        self._rebuild_time_index()
        self._current = None
        self._recheck_availability()

    def update_segments(self, segments: list[Segment] | tuple[Segment, ...]) -> None:
        """Segments arrive as they are generated (F-12), so the time table
        grows during playback."""
        self._segments = tuple(segments)
        self._rebuild_time_index()

    def detach_job(self) -> None:
        self._snapshot = None
        self._segments = ()
        self._starts = []
        self._current = None
        self._set_state(HighlightState.NONE)
        self._recheck_availability()

    def _rebuild_time_index(self) -> None:
        self._starts = []
        self._timed = []
        for i, s in enumerate(self._segments):
            if s.time is not None:
                self._starts.append(s.time.start_ms)
                self._timed.append(i)

    # -- availability (F-29, F-31) --------------------------------------

    @property
    def highlight_available(self) -> bool:
        return self._available

    def _recheck_availability(self) -> None:
        """Availability is a pure function of the current text.

        F-29 is explicit that it carries no hidden state: divergence clears
        the mark and reports it unavailable, and a text that matches the
        snapshot again brings it back.  Implementing it as a comparison
        rather than a latch is what makes that true.
        """
        ok = self._snapshot is not None and self.toPlainText() == self._snapshot
        if ok == self._available:
            return
        self._available = ok
        if not ok:
            self._clear_marks()
            self._set_state(HighlightState.UNAVAILABLE if self._snapshot else HighlightState.NONE)
        else:
            self._repaint_highlight()
        self.availability_changed.emit(ok)

    # -- the mark -------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_segment(self) -> int | None:
        return self._current

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._repaint_highlight()

    def set_playback_ms(self, ms: int, *, waiting: bool = False) -> None:
        """Move the mark to whatever segment covers ``ms``.

        Called from the audio clock.  Note what this does NOT do: it does
        not advance on its own, it does not interpolate, and it does not
        guess.  N-12's 300 ms budget is met by being called often with a
        true position, not by predicting one.
        """
        index = self._segment_at(ms)
        if index is None:
            # Between segments, or past the last ready one.  F-28 keeps the
            # previous segment's mark rather than clearing it, because a
            # highlight that blinks off in every gap reads as a fault.
            self._set_state(HighlightState.WAITING if waiting else self._state)
            return
        changed = index != self._current
        self._current = index
        self._state = HighlightState.WAITING if waiting else HighlightState.PLAYING
        self._repaint_highlight()
        if changed and self._follow and not self._following_suspended:
            self._scroll_to_current()

    def _segment_at(self, ms: int) -> int | None:
        if not self._starts:
            return None
        slot = bisect_right(self._starts, ms) - 1
        if slot < 0:
            return None
        # Past the end of the last ready segment still resolves to it:
        # F-28 keeps the previous segment marked while playback waits on
        # the buffer, because a mark that blinks off in every gap reads as
        # a fault rather than as waiting.
        return self._timed[slot]

    def set_paused(self) -> None:
        """F-28: keep the last position, indicate the state separately."""
        if self._current is not None:
            self._set_state(HighlightState.PAUSED)

    def set_playing(self) -> None:
        if self._current is not None:
            self._set_state(HighlightState.PLAYING)

    def clear_highlight(self) -> None:
        """Stop, cancel, or natural end (F-28)."""
        self._current = None
        self._clear_marks()
        self._set_state(HighlightState.NONE)

    def _clear_marks(self) -> None:
        self.setExtraSelections([])

    def _repaint_highlight(self) -> None:
        if not self._available or self._current is None:
            self._clear_marks()
            return
        if self._state in (HighlightState.NONE, HighlightState.UNAVAILABLE):
            self._clear_marks()
            return
        seg = self._segments[self._current]
        colour = (
            self._palette.emphasis_waiting
            if self._state == HighlightState.WAITING
            else self._palette.emphasis
        )
        fmt = QTextCharFormat()
        fmt.setTextOutline(QPen(QColor(colour), OUTLINE_WIDTH))
        sel = QTextEdit.ExtraSelection()
        sel.cursor = self._cursor_for(seg)
        sel.format = fmt
        self.setExtraSelections([sel])

    def _cursor_for(self, seg: Segment) -> QTextCursor:
        cur = QTextCursor(self.document())
        cur.setPosition(self._map.to_utf16(seg.source.start))
        cur.setPosition(self._map.to_utf16(seg.source.end), QTextCursor.MoveMode.KeepAnchor)
        return cur

    # -- following (F-30) ------------------------------------------------

    @property
    def follow_enabled(self) -> bool:
        return self._follow

    def set_follow(self, on: bool) -> None:
        self._follow = on
        self._following_suspended = False
        self.following_changed.emit(on)
        if on:
            self._scroll_to_current()

    @property
    def following_suspended(self) -> bool:
        return self._following_suspended

    def return_to_position(self) -> None:
        """F-30's return-to-current-position action."""
        self._following_suspended = False
        self.following_changed.emit(self._follow)
        self._scroll_to_current()

    def _scroll_to_current(self) -> None:
        """Scroll the viewport without touching the cursor or the selection.

        ``ensureCursorVisible`` would move the real cursor, which is exactly
        what F-30 forbids, so the rectangle is computed from a throwaway
        cursor and the scrollbar is moved directly.
        """
        if self._current is None or not self._available:
            return
        seg = self._segments[self._current]
        rect = self.cursorRect(self._cursor_for(seg))
        bar = self.verticalScrollBar()
        view_h = self.viewport().height()
        # Keep the line a third of the way down: reading ahead is easier
        # than reading at the bottom edge, and it leaves room for the next
        # segment to appear without another scroll.
        target = bar.value() + rect.top() - view_h // 3
        target = max(bar.minimum(), min(bar.maximum(), target))
        if target != bar.value():
            self._programmatic_scroll = True
            bar.setValue(target)
            self._programmatic_scroll = False

    def _on_scrolled(self, _value: int) -> None:
        if self._programmatic_scroll or not self._follow:
            return
        if not self._following_suspended:
            self._following_suspended = True
            self.following_changed.emit(False)

    def wheelEvent(self, event: QWheelEvent) -> None:
        # A wheel scroll is a manual scroll even when it changes nothing,
        # e.g. already at the bottom; the scrollbar signal would not fire.
        if self._follow and not self._following_suspended:
            self._following_suspended = True
            self.following_changed.emit(False)
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Refuse input past the limit rather than truncating afterwards
        # (F-03, F-36).  Deletions, navigation, and shortcuts pass through.
        if event.text() and not event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        ):
            cur = self.textCursor()
            added = len(event.text()) - len(cur.selectedText())
            if added > 0 and len(self.toPlainText()) + added > MAX_INPUT_CODEPOINTS:
                event.ignore()
                return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt override
        """Paste, clamped by refusal rather than by silent truncation."""
        text = source.text() if source.hasText() else ""
        if not text:
            return
        cur = self.textCursor()
        room = MAX_INPUT_CODEPOINTS - (len(self.toPlainText()) - len(cur.selectedText()))
        if len(text) > room:
            # F-36 forbids circumventing the limit by truncating, so paste
            # nothing and let the window explain.  Silently dropping the
            # tail is the behaviour the requirement names.
            self.paste_refused.emit(len(text), max(0, room))
            return
        cur.insertText(text)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._apply_line_height()
