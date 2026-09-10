"""The reading surface's contract, including the one N-13 exists for.

``spikes/outline_emphasis.py`` proved the mechanism once, on a bare widget.
This proves it on the widget that ships, and keeps proving it: a later change
to the font, the line height, or the emphasis format that reintroduced reflow
would fail here rather than in a release review.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from echoact.domain import Segment, TextRange, TimeRange
from echoact.policy import MAX_INPUT_CODEPOINTS
from echoact.ui import theme
from echoact.ui.reading import HighlightState, ReadingSurface, _Utf16Map

KO = "에코액트는 문서를 소리내어 읽어 줍니다."
EN = "EchoAct reads your documents aloud on this machine."
TEXT = f"{KO} {EN} 세 번째 문장입니다. A fourth sentence follows it here."


@pytest.fixture(scope="session")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def surface(app: QApplication) -> ReadingSurface:
    w = ReadingSurface(theme.LIGHT)
    w.set_text(TEXT)
    w.resize(420, 300)  # narrow enough that the sample wraps several times
    w.ensurePolished()
    w.grab()  # forces a layout without needing a visible window
    return w


def _segments() -> list[Segment]:
    """Four segments tiling the sample, with plausible times."""
    bounds = [(0, 21), (21, 74), (74, 87), (87, len(TEXT))]
    out = []
    t = 0
    for i, (a, b) in enumerate(bounds):
        dur = (b - a) * 150
        out.append(
            Segment(
                index=i,
                source=TextRange(a, b),
                spoken_text=TEXT[a:b],
                language="ko" if i % 2 == 0 else "en",
                time=TimeRange(t, t + dur),
                ready=True,
            )
        )
        t += dur
    return out


def _glyph_positions(w: ReadingSurface) -> list[tuple[int, int]]:
    """Where every character sits, in viewport coordinates."""
    doc = w.document()
    cur = QTextCursor(doc)
    out = []
    for i in range(doc.characterCount()):
        cur.setPosition(i)
        r = w.cursorRect(cur)
        out.append((r.x(), r.y()))
    return out


# ---------------------------------------------------------------- N-13 ---


def test_emphasis_does_not_move_a_single_glyph(surface: ReadingSurface) -> None:
    before = _glyph_positions(surface)
    surface.attach_job(TEXT, _segments())
    surface.set_playback_ms(0)
    assert surface.current_segment == 0
    after = _glyph_positions(surface)
    moved = sum(1 for a, b in zip(before, after, strict=True) if a != b)
    assert moved == 0, f"{moved} characters moved when the emphasis was applied"


def test_emphasis_actually_paints_something(surface: ReadingSurface) -> None:
    """The companion to the test above.

    Zero movement is trivially satisfied by an emphasis that does nothing --
    which is exactly what bold-through-an-extra-selection does, and why the
    spike's first answer was wrong. So also assert that pixels change.
    """
    plain = surface.grab().toImage()
    surface.attach_job(TEXT, _segments())
    surface.set_playback_ms(0)
    marked = surface.grab().toImage()
    assert plain.size() == marked.size()
    differing = sum(
        1
        for y in range(0, marked.height(), 2)
        for x in range(0, marked.width(), 2)
        if plain.pixel(x, y) != marked.pixel(x, y)
    )
    assert differing > 20, "the emphasis changed no pixels; it is not being drawn"


def test_emphasis_does_not_leak_onto_distant_lines(surface: ReadingSurface) -> None:
    """A pen wide enough to smear onto other paragraphs would break the
    spirit of N-13 even though it moves nothing.

    Stated geometry-free on purpose. Qt reports a line rectangle at the top
    of a proportionally enlarged line box while painting the glyphs
    elsewhere inside it, so an assertion phrased in line rectangles tests
    that quirk rather than the emphasis. Marking the LAST segment and
    requiring the top of the view to be untouched needs no such mapping.
    """
    segs = _segments()
    surface.attach_job(TEXT, segs)
    plain = surface.grab().toImage()
    surface.set_playback_ms(segs[-1].time.start_ms)
    assert surface.current_segment == len(segs) - 1
    marked = surface.grab().toImage()

    # Where the last segment starts, and everything comfortably above it.
    cur = QTextCursor(surface.document())
    cur.setPosition(segs[-1].source.start)
    first_line_top = surface.cursorRect(cur).top()
    ceiling = first_line_top - 8
    assert ceiling > 12, "the sample must wrap onto several lines for this to mean anything"

    stray = {
        y: sum(1 for x in range(marked.width()) if plain.pixel(x, y) != marked.pixel(x, y))
        for y in range(ceiling)
    }
    stray = {y: n for y, n in stray.items() if n}
    assert not stray, f"marking the last segment changed pixels far above it: {stray}"


# ---------------------------------------------------------------- F-29 ---


def test_highlight_is_unavailable_once_the_input_diverges(surface: ReadingSurface) -> None:
    surface.attach_job(TEXT, _segments())
    surface.set_playback_ms(0)
    assert surface.highlight_available

    surface.setPlainText(TEXT + " edited")
    assert not surface.highlight_available
    assert surface.state == HighlightState.UNAVAILABLE
    assert surface.extraSelections() == []


def test_highlight_returns_when_the_text_matches_again(surface: ReadingSurface) -> None:
    """F-29 says availability is a function of the current text alone and
    carries no hidden state, so restoring the text must restore the mark."""
    surface.attach_job(TEXT, _segments())
    surface.set_playback_ms(0)
    surface.setPlainText("something else entirely")
    assert not surface.highlight_available

    surface.setPlainText(TEXT)
    assert surface.highlight_available
    surface.set_playback_ms(0)
    assert len(surface.extraSelections()) == 1


def test_emphasis_never_enters_the_document(surface: ReadingSurface) -> None:
    surface.attach_job(TEXT, _segments())
    surface.set_playback_ms(0)
    assert surface.toPlainText() == TEXT
    cur = QTextCursor(surface.document())
    cur.select(QTextCursor.SelectionType.Document)
    assert cur.selection().toPlainText() == TEXT
    assert "**" not in surface.toPlainText()
    # And the document itself carries no character formatting.
    cur.setPosition(3)
    assert cur.charFormat().textOutline().style().name == "NoPen"


def test_marking_does_not_move_the_caret_or_the_selection(surface: ReadingSurface) -> None:
    cur = surface.textCursor()
    cur.setPosition(5)
    cur.setPosition(9, QTextCursor.MoveMode.KeepAnchor)
    surface.setTextCursor(cur)

    surface.attach_job(TEXT, _segments())
    surface.set_playback_ms(0)
    surface.set_playback_ms(10_000)

    after = surface.textCursor()
    assert (after.anchor(), after.position()) == (5, 9)


# ---------------------------------------------------------------- F-28 ---


def test_states_follow_playback(surface: ReadingSurface) -> None:
    segs = _segments()
    surface.attach_job(TEXT, segs)

    surface.set_playback_ms(0)
    assert surface.state == HighlightState.PLAYING
    assert surface.current_segment == 0

    surface.set_paused()
    assert surface.state == HighlightState.PAUSED
    assert surface.current_segment == 0  # position retained

    surface.set_playback_ms(segs[1].time.start_ms + 5)
    assert surface.current_segment == 1

    surface.clear_highlight()
    assert surface.state == HighlightState.NONE
    assert surface.extraSelections() == []


def test_waiting_keeps_the_last_position_but_says_so(surface: ReadingSurface) -> None:
    """F-12 has playback wait at a segment that is not generated yet.

    Only the first two segments are ready here, which is the situation the
    requirement describes: the playhead runs past the end of what exists and
    the mark must stay on the last real segment rather than jumping ahead or
    vanishing.
    """
    segs = _segments()[:2]
    surface.attach_job(TEXT, segs)
    surface.set_playback_ms(segs[1].time.start_ms)
    surface.set_playback_ms(segs[1].time.end_ms + 500, waiting=True)
    assert surface.current_segment == 1
    assert surface.state == HighlightState.WAITING


def test_a_time_before_any_segment_marks_nothing(surface: ReadingSurface) -> None:
    segs = _segments()
    shifted = [
        Segment(
            index=s.index,
            source=s.source,
            spoken_text=s.spoken_text,
            language=s.language,
            time=TimeRange(s.time.start_ms + 500, s.time.end_ms + 500),
            ready=True,
        )
        for s in segs
    ]
    surface.attach_job(TEXT, shifted)
    surface.set_playback_ms(10)
    assert surface.current_segment is None
    assert surface.extraSelections() == []


# ---------------------------------------------------------------- F-30 ---


def test_manual_scroll_suspends_following(surface: ReadingSurface, app) -> None:
    long_text = (TEXT + "\n") * 40
    surface.set_text(long_text)
    surface.grab()
    assert surface.follow_enabled

    bar = surface.verticalScrollBar()
    assert bar.maximum() > 0, "the sample must be tall enough to scroll"
    bar.setValue(bar.maximum() // 2)
    assert surface.following_suspended

    surface.return_to_position()
    assert not surface.following_suspended


# ------------------------------------------------------- code points -----


def test_utf16_map_is_identity_for_bmp_text() -> None:
    m = _Utf16Map.build("에코액트 EchoAct")
    assert m.identity
    assert m.to_utf16(4) == 4


def test_utf16_map_accounts_for_surrogate_pairs() -> None:
    text = "a\U0001F3A7b"  # headphones emoji is two UTF-16 units
    m = _Utf16Map.build(text)
    assert not m.identity
    assert m.to_utf16(0) == 0
    assert m.to_utf16(1) == 1
    assert m.to_utf16(2) == 3
    assert m.to_utf16(3) == 4


def test_emoji_do_not_shift_the_marked_range(app: QApplication) -> None:
    """F-27 by way of 4.2: segment ranges are code points, Qt counts UTF-16.

    Getting this wrong marks the wrong characters in exactly the documents
    F-27 names -- the ones with emoji in them.
    """
    text = "\U0001F3A7\U0001F4D6 첫 문장입니다. 두 번째 문장입니다."
    second = text.index("두")
    w = ReadingSurface(theme.LIGHT)
    w.set_text(text)
    w.resize(400, 200)
    w.grab()
    seg = Segment(
        index=0,
        source=TextRange(second, len(text)),
        spoken_text=text[second:],
        language="ko",
        time=TimeRange(0, 1000),
        ready=True,
    )
    w.attach_job(text, [seg])
    w.set_playback_ms(0)

    sel = w.extraSelections()[0]
    picked = sel.cursor.selectedText()
    assert picked == text[second:], f"marked {picked!r}"


# ------------------------------------------------------- F-03 / F-36 -----


def test_paste_beyond_the_limit_is_refused_not_truncated(
    surface: ReadingSurface, app: QApplication
) -> None:
    from PySide6.QtCore import QMimeData

    surface.set_text("a" * (MAX_INPUT_CODEPOINTS - 10))
    refusals: list[tuple[int, int]] = []
    surface.paste_refused.connect(lambda a, b: refusals.append((a, b)))

    data = QMimeData()
    data.setText("b" * 100)
    surface.insertFromMimeData(data)

    assert refusals == [(100, 10)]
    assert len(surface.toPlainText()) == MAX_INPUT_CODEPOINTS - 10
    assert "b" not in surface.toPlainText()
