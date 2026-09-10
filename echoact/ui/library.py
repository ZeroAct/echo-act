"""The library screen: saved documents and generation history.

F-38 to F-43 and F-45, in one widget the window can put in a dialog or a
stacked page.  Three properties shape everything below.

*It never calls the engine.*  Like the panels in :mod:`echoact.ui.controls`
it shows state and emits intent -- ``open_document``, ``play_job``,
``regenerate_job``, ``export_job``, ``delete_requested`` -- and the window
decides.  It does read the store directly, because a library that cannot
list is not a library; reading rows is not the same authority as starting a
job.

*A list costs a list query and nothing more.*  F-40 requires a page to be
displayable without reading whole audio files, and F-56 makes the
body-text-free summary the default projection.  So the trees are built from
:class:`~echoact.db.store.JobSummary` and
:class:`~echoact.db.store.DocumentSummary` only: an audio length comes from
the stored frame count, a document's size from ``length(body)``, and the
source-text snapshot is fetched exactly once -- for the one entry the user
opened, through F-56's separate ``get_job_text`` call.

*A regenerated result is a different result.*  A.5 measured two runs of the
same text with the same settings differing by 0.47 on a signal bounded at
1.0.  F-41 therefore forbids presenting a regeneration as the old result,
and the regenerate action says so where it is pressed rather than in a
release note.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..db.store import DeletionScope, DocumentSummary, JobSummary, ResultIntegrity, Store
from ..domain import JobState, Result, RetentionMode
from ..errors import EchoActError
from ..models.manifest import Manifest
from ..policy import LIST_PAGE_DEFAULT, LIST_PAGE_MAX
from ..util import ids
from .controls import label, separator
from .i18n import add_korean, approximate_duration, bytes_size, count, duration, memory_size, tr
from .theme import METRICS, Palette, qcolor

#: How long typing settles before a search runs.  A view cadence, not a
#: policy limit: it exists so a five-letter query is one FTS5 match instead
#: of five, and nothing in Section 4 has an opinion about it.
SEARCH_DEBOUNCE_MS = 200

#: The date filter's choices, in days back from now (F-40 filters history by
#: date).  Offered windows, not policy: Section 4 fixes retention and log
#: lifetimes, never how a person narrows a list.
DATE_WINDOWS_DAYS = (1, 7, 30)

_SECONDS_PER_DAY = 86_400

#: Rows per page.  Both come from 4.1 rather than from taste -- the default
#: is what a query returns when nobody asks, and the larger choice is the
#: ceiling the store clamps to anyway.
PAGE_SIZES = (LIST_PAGE_DEFAULT, LIST_PAGE_MAX)

add_korean(
    {
        "Documents": "문서",
        "History": "생성 기록",
        "Search titles and text": "제목과 본문 검색",
        "Untitled": "제목 없음",
        "Title": "제목",
        "Created": "만든 날짜",
        "Edited": "수정한 날짜",
        "Characters": "글자 수",
        "Nothing saved yet": "저장된 항목이 없습니다",
        "No generation history yet": "생성 기록이 없습니다",
        "Nothing matches that search": "검색 결과가 없습니다",
        "Nothing matches those filters": "조건에 맞는 항목이 없습니다",
        # history columns and filters
        "When": "시각",
        "State": "상태",
        "Retention": "보관",
        "Length": "길이",
        "Any time": "전체 기간",
        "Today": "오늘",
        "Last {n} days": "최근 {n}일",
        "Any model": "모든 모델",
        "Any state": "모든 상태",
        "In progress": "진행 중",
        "Anything kept": "보관 여부 무관",
        "Kept": "보관됨",
        "One-off": "일회성",
        "Accepted": "접수됨",
        "Canceling": "취소하는 중",
        # retention (F-42)
        "Kept until you delete it": "삭제할 때까지 보관됩니다",
        "One-off · expires in {when}": "일회성 · {when} 후 만료",
        "One-off · the audio has expired": "일회성 · 오디오가 만료되었습니다",
        "One-off · nothing is kept": "일회성 · 저장되지 않습니다",
        "One-off · removed when the app closes": "일회성 · 앱을 닫으면 삭제됩니다",
        # playability (F-45)
        "audio expired": "오디오 만료됨",
        "audio missing": "오디오 없음",
        "audio damaged": "오디오 손상됨",
        "audio not kept": "오디오 미보관",
        "This one-off audio has expired, so it cannot be played.":
            "이 일회성 오디오는 만료되어 재생할 수 없습니다.",
        "The audio file is missing, so it cannot be played.":
            "오디오 파일이 없어 재생할 수 없습니다.",
        "The audio file failed its integrity check, so it is not played.":
            "오디오 파일이 무결성 검사를 통과하지 못해 재생하지 않습니다.",
        "The audio for this entry is no longer stored.":
            "이 항목의 오디오는 더 이상 저장되어 있지 않습니다.",
        "The app closed before this finished, so there is no audio.":
            "생성이 끝나기 전에 앱이 종료되어 오디오가 없습니다.",
        "This generation failed, so there is no audio.":
            "생성에 실패하여 오디오가 없습니다.",
        "This generation was canceled, so there is no audio.":
            "생성이 취소되어 오디오가 없습니다.",
        "This generation has not finished.": "생성이 아직 끝나지 않았습니다.",
        # detail panel (F-39, F-41)
        "Select an entry to see its text and settings.":
            "항목을 선택하면 본문과 설정을 볼 수 있습니다.",
        "Source text": "원본 본문",
        "The source text was not kept for this entry.":
            "이 항목의 원본 본문은 보관되지 않았습니다.",
        "The saved text could not be read.": "저장된 본문을 읽지 못했습니다.",
        "The saved text could not be read, so it cannot be generated again yet.":
            "저장된 본문을 읽지 못해 아직 다시 생성할 수 없습니다.",
        "Origin": "요청 경로",
        "Applied budget": "적용된 자원 한도",
        "Segments": "문장",
        "{done} of {total}": "{total}개 중 {done}개",
        "Started": "시작",
        "Finished": "종료",
        "Audio": "오디오",
        "Regenerate": "다시 생성",
        "Regenerating makes a new entry. The old one is kept, and the new audio "
        "will not be identical.":
            "다시 생성하면 새 항목이 만들어집니다. 기존 항목은 그대로 남고, 새 오디오는 "
            "이전과 완전히 같지 않습니다.",
        "There is no saved text to generate again from.":
            "다시 생성할 저장된 본문이 없습니다.",
        # paging
        "{first}–{last} of {total}": "{total}개 중 {first}–{last}",
        "Nothing to show": "표시할 항목이 없습니다",
        "Previous page": "이전 페이지",
        "Next page": "다음 페이지",
        "Per page": "쪽당 항목 수",
        # deletion (F-43)
        "Delete all history": "기록 전체 삭제",
        "Delete these items?": "이 항목을 삭제할까요?",
        "Delete all generation history?": "생성 기록을 모두 삭제할까요?",
        "{n} documents": "문서 {n}개",
        "1 document": "문서 1개",
        "{n} history entries": "기록 {n}개",
        "1 history entry": "기록 1개",
        "{n} audio files, {size}": "오디오 파일 {n}개, {size}",
        "{size} of saved text": "저장된 본문 {size}",
        "Delete the entries and their audio": "항목과 오디오를 함께 삭제",
        "Delete only the audio and keep the entries": "오디오만 삭제하고 항목은 남기기",
        "Generation history is stored separately. No job's saved text is "
        "deleted by this.":
            "생성 기록은 별도로 저장됩니다. 이 삭제로 지워지는 작업 본문은 없습니다.",
        "WAV files you exported yourself are not touched.":
            "직접 내보낸 WAV 파일은 삭제되지 않습니다.",
        "Cancel the generation that is still running before deleting this.":
            "아직 실행 중인 생성을 취소한 뒤에 삭제하세요.",
        "This cannot be undone.": "되돌릴 수 없습니다.",
    }
)

_UNKNOWN = "—"


def _set_role(widget: QWidget, role: str) -> None:
    """Change a widget's theme role after it has been shown.

    Qt resolves a stylesheet's property selectors at polish time, so
    assigning ``role`` alone repaints nothing; the unpolish/polish pair is
    what makes the token in ``theme.stylesheet`` take effect.
    """
    if widget.property("role") == role:
        return
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class WrapRow(QLayout):
    """A row of controls that wraps onto a second line instead of forcing
    the window wider.

    N-30's floor is 1280x720 at 200% OS scaling, which is 640x360 logical
    pixels, and a :class:`QHBoxLayout`'s minimum width is the sum of
    everything in it.  A filter row of four combo boxes and two buttons
    therefore sets a minimum no such screen can grant: the row wins, the
    screen is clipped sideways, and -- unlike a column that is merely too
    tall -- there is no scrollbar to recover the controls with.  Wrapping
    makes the minimum the widest *single* control instead, so the screen
    compresses and every filter and page button stays reachable.

    :meth:`add_stretch` leaves the wide layout exactly as it was: while
    everything fits on one line the spacer pushes what follows to the right
    edge, and once it does not, the spacer is where the line breaks.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(METRICS.gap)

    def add_stretch(self) -> None:
        """A gap that right-aligns on one line and breaks the line on two."""
        self.addItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        )

    # -- QLayout's plumbing --------------------------------------------

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), place=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, place=True)

    def sizeHint(self) -> QSize:
        return self._natural()

    def minimumSize(self) -> QSize:
        """The widest single control, which is the whole point: a row that
        can wrap never needs room for all of its contents at once."""
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size.grownBy(self.contentsMargins())

    # -- placement -----------------------------------------------------

    def _natural(self) -> QSize:
        """What the row wants: one line, spacers at nothing."""
        widths = [item.sizeHint().width() for item in self._items if item.spacerItem() is None]
        heights = [item.sizeHint().height() for item in self._items if item.spacerItem() is None]
        width = sum(widths) + self.spacing() * max(0, len(widths) - 1)
        return QSize(width, max(heights, default=0)).grownBy(self.contentsMargins())

    def _arrange(self, rect: QRect, *, place: bool) -> int:
        """Lay the row out inside ``rect`` and report the height it needed.

        Called twice for the same width -- once by ``heightForWidth`` to ask
        and once by ``setGeometry`` to place -- so the two can never
        disagree about where a line breaks.
        """
        margins = self.contentsMargins()
        area = rect.marginsRemoved(margins)
        gap = self.spacing()
        natural = self._natural().width() - margins.left() - margins.right()
        one_line = natural <= area.width()
        spacers = sum(1 for item in self._items if item.spacerItem() is not None)
        share = (area.width() - natural) // spacers if one_line and spacers else 0

        x, y, line = area.x(), area.y(), 0
        for item in self._items:
            if item.spacerItem() is not None:
                if one_line:
                    x += share
                elif line:
                    x, y, line = area.x(), y + line + gap, 0
                continue
            hint = item.sizeHint()
            width = max(item.minimumSize().width(), min(hint.width(), area.width()))
            if line and x + width > area.right() + 1:
                x, y, line = area.x(), y + line + gap, 0
            if place:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
            x += width + gap
            line = max(line, hint.height())
        return y + line - rect.y() + margins.bottom()


# ======================================================================
# Row text.  Pure functions, so what a row claims can be tested without a
# widget -- and so no two places can word the same state differently.
# ======================================================================


def state_text(state: JobState) -> str:
    """Section 5.1's states, in words.  ``Interrupted`` is one of them:
    F-45 requires a job a forced termination left behind to say so rather
    than to look merely unfinished."""
    return {
        JobState.ACCEPTED: tr("Accepted"),
        JobState.PREPARING_MODEL: tr("Preparing the model"),
        JobState.GENERATING: tr("Generating"),
        JobState.CANCELING: tr("Canceling"),
        JobState.COMPLETE: tr("Complete"),
        JobState.FAILED: tr("Failed"),
        JobState.CANCELED: tr("Canceled"),
        JobState.INTERRUPTED: tr("Interrupted"),
    }[state]


@dataclass(frozen=True, slots=True)
class Playability:
    """Whether a history entry can be played, and if not, why (F-45).

    The reason is a whole sentence because it is shown to a person who is
    asking why the play button is grey; ``short`` is the same finding in the
    two words a table column has room for.
    """

    playable: bool
    reason: str = ""
    short: str = ""


def playability(
    summary: JobSummary,
    *,
    integrity: ResultIntegrity | None = None,
    expired: bool | None = None,
) -> Playability:
    """F-45's "the reason playback is unavailable", from a summary alone.

    ``integrity`` and ``expired`` override what the summary recorded, so the
    detail panel can re-check the file it is about to offer while a list row
    stays a pure read of the query -- F-40 does not allow a page of rows to
    open a page of files.
    """
    state = summary.state
    if state is JobState.INTERRUPTED:
        return Playability(False, tr("The app closed before this finished, so there is no audio."))
    if state is JobState.FAILED:
        return Playability(
            False,
            summary.error_message or tr("This generation failed, so there is no audio."),
        )
    if state is JobState.CANCELED:
        return Playability(False, tr("This generation was canceled, so there is no audio."))
    if not state.is_terminal:
        return Playability(False, tr("This generation has not finished."))

    if not summary.has_result:
        return Playability(
            False, tr("The audio for this entry is no longer stored."), tr("audio not kept")
        )
    gone = summary.result_expired if expired is None else expired
    if gone:
        return Playability(
            False, tr("This one-off audio has expired, so it cannot be played."), tr("audio expired")
        )
    found = summary.result_integrity if integrity is None else integrity
    if found is ResultIntegrity.MISSING:
        return Playability(
            False, tr("The audio file is missing, so it cannot be played."), tr("audio missing")
        )
    if found is ResultIntegrity.CORRUPT:
        return Playability(
            False,
            tr("The audio file failed its integrity check, so it is not played."),
            tr("audio damaged"),
        )
    return Playability(True)


def retention_text(
    summary: JobSummary, *, expires_at: float | None = None, now: float | None = None
) -> str:
    """F-42, in the row: kept or one-off, and when a one-off goes.

    A one-off GUI result has no stored expiry -- 4.1 ties it to the app
    closing rather than to a clock -- so that case says what actually
    happens instead of inventing a time.
    """
    if summary.retention is RetentionMode.RETAINED:
        return tr("Kept until you delete it")
    if not summary.has_result:
        return tr("One-off · nothing is kept")
    moment = ids.now() if now is None else now
    if expires_at is None:
        if summary.result_expired:
            return tr("One-off · the audio has expired")
        return tr("One-off · removed when the app closes")
    if expires_at <= moment:
        return tr("One-off · the audio has expired")
    return tr("One-off · expires in {when}").format(
        when=approximate_duration(int((expires_at - moment) * 1000))
    )


def when_text(ts: float | None) -> str:
    """A timestamp a person can compare against their own memory.  Numeric
    in both languages: a localised month name in a narrow column wraps, and
    F-38's requirement is that the times are displayed, not that they are
    prose."""
    if not ts:
        return _UNKNOWN
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


@dataclass(frozen=True, slots=True)
class JobRow:
    """One history row's five cells, plus what the eye needs beyond words."""

    job_id: str
    when: str
    voice: str
    state: str
    retention: str
    length: str
    playable: bool
    #: The whole sentence behind an unplayable row, for the tooltip and the
    #: accessible description; empty when the row plays.
    reason: str
    #: A palette role name (``""``, ``"warn"`` or ``"danger"``) for the
    #: state cell.  Never the only signal: the cell text carries the finding
    #: in words, which is what N-30 asks for.
    severity: str


def build_job_row(
    summary: JobSummary,
    *,
    model_name: str | None = None,
    expires_at: float | None = None,
    now: float | None = None,
) -> JobRow:
    """F-39's row, from the summary the list query already returned.

    The expiry decides both the retention cell and the state cell, so a row
    cannot say "expires in 20 minutes" beside "playable" once the clock has
    passed the moment the query was made.
    """
    moment = ids.now() if now is None else now
    expired = None if expires_at is None else expires_at <= moment
    play = playability(summary, expired=expired)
    state = state_text(summary.state)
    if play.short:
        state = f"{state} · {play.short}"
    severity = ""
    if summary.state in (JobState.FAILED, JobState.INTERRUPTED):
        severity = "danger"
    elif not play.playable and summary.state is JobState.COMPLETE:
        severity = "warn"
    settings = summary.settings
    voice = f"{model_name or settings.model_id} · {settings.voice_id} · {settings.tempo:.2f}x"
    return JobRow(
        job_id=summary.job_id,
        when=when_text(summary.created_at),
        voice=voice,
        state=state,
        retention=retention_text(summary, expires_at=expires_at, now=now),
        length=duration(summary.audio_duration_ms) if summary.audio_duration_ms else _UNKNOWN,
        playable=play.playable,
        reason="" if play.playable else play.reason,
        severity=severity,
    )


@dataclass(frozen=True, slots=True)
class DocumentRow:
    document_id: str
    title: str
    created: str
    edited: str
    size: str


def build_document_row(summary: DocumentSummary) -> DocumentRow:
    """F-38's row: title with both timestamps, and a size that came from
    ``length(body)`` rather than from the body."""
    return DocumentRow(
        document_id=summary.document_id,
        title=summary.title.strip() or tr("Untitled"),
        created=when_text(summary.created_at),
        edited=when_text(summary.modified_at),
        size=count(summary.body_codepoints),
    )


# ======================================================================
# Deletion (F-43)
# ======================================================================


@dataclass(frozen=True, slots=True)
class DeleteRequest:
    """What the user confirmed, for the window to carry out.

    The screen previews and asks; it does not delete.  The store hands back
    the audio paths a deletion orphans and deliberately does not unlink
    them, so the act belongs where the audio directory's layout is known --
    which is also the only way F-43's "WAV files you exported separately are
    not deleted" can be guaranteed.
    """

    document_ids: tuple[str, ...] = ()
    job_ids: tuple[str, ...] = ()
    #: Remove the audio and keep the history entry.  F-43 distinguishes
    #: documents, jobs and audio, so this is a scope of its own and not a
    #: weaker kind of job deletion.
    audio_only: bool = False
    all_history: bool = False
    #: Exactly the preview the user agreed to, so a caller can report what
    #: was promised rather than recount it.
    scope: DeletionScope | None = None


def scope_lines(scope: DeletionScope, *, audio_only: bool = False) -> list[str]:
    """F-43's preview, itemised so documents, entries and audio stay
    distinguishable in the sentence the user reads."""
    lines: list[str] = []
    if not audio_only:
        if scope.documents == 1:
            lines.append(tr("1 document"))
        elif scope.documents:
            lines.append(tr("{n} documents").format(n=count(scope.documents)))
        if scope.jobs == 1:
            lines.append(tr("1 history entry"))
        elif scope.jobs:
            lines.append(tr("{n} history entries").format(n=count(scope.jobs)))
    if scope.results:
        lines.append(
            tr("{n} audio files, {size}").format(
                n=count(scope.results), size=bytes_size(scope.audio_bytes)
            )
        )
    if scope.text_bytes and not audio_only:
        lines.append(tr("{size} of saved text").format(size=bytes_size(scope.text_bytes)))
    return lines


class DeleteConfirmDialog(QDialog):
    """F-43's confirmation: the scope, the choices, and no default button
    that deletes.

    Cancel is the default for the same reason the licence dialog's decline
    is: a destructive answer given to the Enter key has not been given.
    """

    def __init__(
        self,
        palette: Palette,
        scope: DeletionScope,
        *,
        kind: str,
        all_history: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        m = METRICS
        self._scope = scope
        self._audio_choice: QRadioButton | None = None

        self.setModal(True)
        self.setWindowTitle(
            tr("Delete all generation history?") if all_history else tr("Delete these items?")
        )
        self.setMinimumWidth(440)

        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad_wide)
        box.setSpacing(m.gap)

        box.addWidget(label(self.windowTitle(), "section"))
        self.summary = label("", "secondary")
        self.summary.setWordWrap(True)
        box.addWidget(self.summary)

        # F-43: deleting a document is never presented as having deleted a
        # job's saved text.  The two are separate copies (4.2, N-14), so the
        # dialog says so where the user is deciding.
        if kind == "documents":
            note = label(
                tr("Generation history is stored separately. No job's saved text is deleted by this."),
                "muted",
            )
            note.setWordWrap(True)
            box.addWidget(note)

        if kind == "jobs" and scope.results and not all_history:
            box.addWidget(separator())
            self._both = QRadioButton(tr("Delete the entries and their audio"))
            self._both.setChecked(True)
            self._audio_choice = QRadioButton(tr("Delete only the audio and keep the entries"))
            box.addWidget(self._both)
            box.addWidget(self._audio_choice)
            # The preview follows the choice.  F-43 asks for the scope to be
            # previewed, and a preview that still lists the history entries
            # after the user narrowed the deletion to audio is not one.
            self._audio_choice.toggled.connect(lambda _: self._show_scope())

        keep = label(tr("WAV files you exported yourself are not touched."), "muted")
        keep.setWordWrap(True)
        box.addWidget(keep)

        undo = label(tr("This cannot be undone."), "muted")
        box.addWidget(undo)

        blocked = bool(scope.blocked_job_ids)
        if blocked:
            # 5.3: a running job is cancelled first and is never deleted in
            # part, so the dialog refuses rather than deleting what it can.
            warn = label(
                tr("Cancel the generation that is still running before deleting this."), "warn"
            )
            warn.setWordWrap(True)
            box.addWidget(warn)

        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        self.delete_button = buttons.addButton(tr("Delete"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.setDefault(False)
        self.delete_button.setAutoDefault(False)
        self.delete_button.setEnabled(not blocked)
        self.cancel_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self._show_scope()

    def _show_scope(self) -> None:
        lines = scope_lines(self._scope, audio_only=self.audio_only) or [tr("Nothing to show")]
        self.summary.setText("\n".join("·  " + line for line in lines))

    @property
    def audio_only(self) -> bool:
        return self._audio_choice is not None and self._audio_choice.isChecked()


# ======================================================================
# Paging (4.1: 20 by default, 100 at most)
# ======================================================================


class PageBar(QWidget):
    """Page position, movement, and page size.

    The size choices are 4.1's own two numbers.  Offering anything larger
    would be a control that lies: the store clamps to the ceiling whatever
    it is handed.
    """

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        m = METRICS
        self._offset = 0
        self._total = 0
        self._shown = 0

        # A wrapping row, not a QHBoxLayout: six controls side by side put a
        # floor under the whole screen's width, and N-30's narrowest
        # supported screen is below it.
        row = WrapRow(self)
        row.setSpacing(m.gap)

        self.range_label = label("", "muted")
        row.addWidget(self.range_label)
        row.add_stretch()

        row.addWidget(label(tr("Per page"), "muted"))
        self.size = QComboBox()
        for value in PAGE_SIZES:
            self.size.addItem(count(value), value)
        self.size.setAccessibleName(tr("Per page"))
        self.size.setFixedWidth(88)
        row.addWidget(self.size)

        self.previous = QPushButton(tr("Previous page"))
        self.previous.setProperty("variant", "quiet")
        self.previous.setAccessibleName(tr("Previous page"))
        self.next = QPushButton(tr("Next page"))
        self.next.setProperty("variant", "quiet")
        self.next.setAccessibleName(tr("Next page"))
        row.addWidget(self.previous)
        row.addWidget(self.next)

        self.previous.clicked.connect(self._back)
        self.next.clicked.connect(self._forward)
        self.size.currentIndexChanged.connect(self._resize)

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def page_size(self) -> int:
        value = self.size.currentData()
        return int(value) if value else LIST_PAGE_DEFAULT

    def reset(self) -> None:
        """Back to the first page, without a reload of its own: a filter
        change reloads once, from the pane."""
        self._offset = 0

    def apply_page(self, *, total: int, offset: int, shown: int) -> None:
        self._total = total
        self._offset = offset
        self._shown = shown
        if total <= 0:
            self.range_label.setText(tr("Nothing to show"))
        else:
            self.range_label.setText(
                tr("{first}–{last} of {total}").format(
                    first=count(offset + 1), last=count(offset + shown), total=count(total)
                )
            )
        self.previous.setEnabled(offset > 0)
        self.next.setEnabled(offset + shown < total)

    def _back(self) -> None:
        self._offset = max(0, self._offset - self.page_size)
        self.changed.emit()

    def _forward(self) -> None:
        if self._offset + self._shown < self._total:
            self._offset += self.page_size
            self.changed.emit()

    def _resize(self) -> None:
        self._offset = 0
        self.changed.emit()


def _tree(columns: Sequence[str], name: str) -> QTreeWidget:
    """A list that is a table, and that works with the arrow keys alone.

    A tree rather than a list because F-39 and F-40 both want several facts
    per row aligned down the page, and because the header is already themed.
    Selection is whole-row and the item is activated by Enter, so N-30's
    "usable by keyboard" needs no extra shortcut.
    """
    t = QTreeWidget()
    t.setColumnCount(len(columns))
    t.setHeaderLabels(list(columns))
    t.setRootIsDecorated(False)
    t.setUniformRowHeights(True)
    t.setAlternatingRowColors(True)
    t.setAllColumnsShowFocus(True)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    t.setAccessibleName(name)
    header = t.header()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for i in range(1, len(columns)):
        header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    return t


# ======================================================================
# Documents (F-38, F-40)
# ======================================================================


class DocumentsPane(QWidget):
    """The saved-document list, its search, and its deletion."""

    open_document = Signal(str)
    delete_requested = Signal(object)
    problem = Signal(object)

    def __init__(self, store: Store, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._palette = palette
        m = METRICS

        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)

        top = QHBoxLayout()
        top.setSpacing(m.gap)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search titles and text"))
        self.search.setAccessibleName(tr("Search titles and text"))
        top.addWidget(self.search, 1)
        self.open_button = QPushButton(tr("Open"))
        self.open_button.setAccessibleName(tr("Open"))
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.setAccessibleName(tr("Delete"))
        top.addWidget(self.open_button)
        top.addWidget(self.delete_button)
        box.addLayout(top)

        self.tree = _tree(
            (tr("Title"), tr("Created"), tr("Edited"), tr("Characters")), tr("Documents")
        )
        box.addWidget(self.tree, 1)

        self.status = label("", "muted")
        self.status.setWordWrap(True)
        box.addWidget(self.status)

        self.pages = PageBar()
        box.addWidget(self.pages)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._search_now)

        self.search.textChanged.connect(lambda _: self._debounce.start())
        self.tree.itemActivated.connect(lambda *_: self._open_selected())
        self.tree.itemSelectionChanged.connect(self._update_enabled)
        self.open_button.clicked.connect(self._open_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.pages.changed.connect(self.refresh)
        self._update_enabled()

    # -- data ----------------------------------------------------------

    def refresh(self) -> None:
        """One page, one query.  F-40's page is retrieved without touching
        a body: the store's summary projection reports ``length(body)`` and
        leaves the text in the row."""
        query = self.search.text().strip()
        try:
            page = self._store.search_documents(
                query, limit=self.pages.page_size, offset=self.pages.offset
            )
        except EchoActError as exc:
            self._fail(exc)
            return
        self.tree.clear()
        for summary in page.items:
            row = build_document_row(summary)
            item = QTreeWidgetItem([row.title, row.created, row.edited, row.size])
            item.setData(0, Qt.ItemDataRole.UserRole, row.document_id)
            item.setTextAlignment(3, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.tree.addTopLevelItem(item)
        if page.items:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            self.status.setText("")
        else:
            self.status.setText(
                tr("Nothing matches that search") if query else tr("Nothing saved yet")
            )
        self.pages.apply_page(total=page.total, offset=page.offset, shown=len(page.items))
        self._update_enabled()

    def _search_now(self) -> None:
        self.pages.reset()
        self.refresh()

    def _fail(self, exc: EchoActError) -> None:
        """F-25: a failed query says why, and the pane stays usable."""
        self.status.setText(f"{exc.code.value} — {exc.message}")
        self.problem.emit(exc)

    # -- selection and actions -----------------------------------------

    def selected_ids(self) -> tuple[str, ...]:
        return tuple(
            item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()
        )

    def _update_enabled(self) -> None:
        chosen = self.selected_ids()
        self.open_button.setEnabled(len(chosen) == 1)
        self.delete_button.setEnabled(bool(chosen))

    def _open_selected(self) -> None:
        chosen = self.selected_ids()
        if len(chosen) == 1:
            self.open_document.emit(chosen[0])

    def _delete_selected(self) -> None:
        chosen = self.selected_ids()
        if not chosen:
            return
        try:
            scope = self._store.preview_deletion(document_ids=list(chosen))
        except EchoActError as exc:
            self._fail(exc)
            return
        dialog = DeleteConfirmDialog(self._palette, scope, kind="documents", parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.delete_requested.emit(DeleteRequest(document_ids=chosen, scope=scope))


# ======================================================================
# History (F-39 to F-43, F-45)
# ======================================================================


class JobDetail(QWidget):
    """One reopened history entry: its text, its settings, and what can be
    done with it (F-41, F-45).

    Nothing here starts anything.  F-45 is explicit that an interrupted job
    and a broken result are neither regenerated nor played automatically, so
    every action is a button, and the buttons that cannot work are disabled
    with the reason above them rather than hidden.
    """

    play_requested = Signal(str)
    export_requested = Signal(str)
    regenerate_requested = Signal(str)
    delete_requested = Signal(str)
    problem = Signal(object)

    def __init__(
        self,
        store: Store,
        *,
        manifest: Manifest | None = None,
        clock: Callable[[], float] = ids.now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._manifest = manifest
        self._clock = clock
        self._summary: JobSummary | None = None
        m = METRICS

        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)

        self.heading = label("", "section")
        self.heading.setWordWrap(True)
        box.addWidget(self.heading)

        self.reason = label("", "warn")
        self.reason.setWordWrap(True)
        box.addWidget(self.reason)

        self.retention = label("", "secondary")
        self.retention.setWordWrap(True)
        box.addWidget(self.retention)

        self.facts = label("", "muted")
        self.facts.setWordWrap(True)
        self.facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.facts)

        box.addWidget(separator())
        box.addWidget(label(tr("Source text"), "secondary"))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName(tr("Source text"))
        box.addWidget(self.text, 1)

        actions = QHBoxLayout()
        actions.setSpacing(m.gap)
        self.play_button = QPushButton(tr("Play"))
        self.play_button.setAccessibleName(tr("Play"))
        self.export_button = QPushButton(tr("Save audio"))
        self.export_button.setAccessibleName(tr("Save audio"))
        self.regenerate_button = QPushButton(tr("Regenerate"))
        self.regenerate_button.setAccessibleName(tr("Regenerate"))
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.setAccessibleName(tr("Delete"))
        for button in (
            self.play_button,
            self.export_button,
            self.regenerate_button,
            self.delete_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        box.addLayout(actions)

        # F-41 and A.5: the same settings do not reproduce the same audio,
        # so the warning sits next to the button rather than in a notice
        # the user meets afterwards.
        self.caution = label(
            tr(
                "Regenerating makes a new entry. The old one is kept, and the new audio "
                "will not be identical."
            ),
            "muted",
        )
        self.caution.setWordWrap(True)
        box.addWidget(self.caution)

        self.play_button.clicked.connect(self._play)
        self.export_button.clicked.connect(self._export)
        self.regenerate_button.clicked.connect(self._regenerate)
        self.delete_button.clicked.connect(self._delete)
        self.show_job(None)

    # -- population ----------------------------------------------------

    def show_job(self, summary: JobSummary | None) -> None:
        """Reopen one entry (F-41), or clear the panel.

        The snapshot is read here and only here: F-56 keeps it behind a
        request of its own, and a page of rows must not carry one each.
        """
        self._summary = summary
        if summary is None:
            self.heading.setText(tr("Select an entry to see its text and settings."))
            for widget in (self.reason, self.retention, self.facts):
                widget.setText("")
            self.text.setPlainText("")
            self.caution.hide()
            for button in (
                self.play_button,
                self.export_button,
                self.regenerate_button,
                self.delete_button,
            ):
                button.setEnabled(False)
            return

        self.caution.show()
        result: Result | None = None
        integrity: ResultIntegrity | None = None
        snapshot: str | None = None
        failure: EchoActError | None = None
        try:
            if summary.has_result:
                result = self._store.get_result_for_job(summary.job_id)
                integrity = self._recheck(summary, result)
            snapshot = self._store.get_job_text(summary.job_id)
        except EchoActError as exc:
            failure = exc
            self.problem.emit(exc)

        now = self._clock()
        expires_at = result.expires_at if result is not None else None
        # ``None``, never ``False``: a read that did not happen knows nothing
        # about the expiry, and a hard ``False`` would override the expiry
        # the summary already recorded -- offering Play and Save beside a
        # retention line that says the audio has expired.  F-45 wants the
        # reason, and `build_job_row` makes the same fallback so that the
        # row and the panel cannot contradict each other.
        expired = None if expires_at is None else expires_at <= now
        play = playability(summary, integrity=integrity, expired=expired)

        self.heading.setText(f"{when_text(summary.created_at)} · {state_text(summary.state)}")
        # F-25: a store that could not answer is a failure of this panel,
        # not a fact about the entry, so its code and message are shown
        # beside what is known rather than in place of it -- and are not
        # overwritten by the playability verdict computed from the summary.
        lines = [] if play.playable else [play.reason]
        if failure is not None:
            lines.append(f"{failure.code.value} — {failure.message}")
        self.reason.setText("\n".join(lines))
        self.reason.setVisible(bool(lines))
        # A job that failed or was interrupted lost its audio; a job whose
        # file went missing has a problem with the file.  They are different
        # findings and F-25 wants them to read differently.
        _set_role(
            self.reason,
            "danger"
            if failure is not None or summary.state in (JobState.FAILED, JobState.INTERRUPTED)
            else "warn",
        )
        self.retention.setText(retention_text(summary, expires_at=expires_at, now=now))
        self.facts.setText(self._facts(summary, result))

        if snapshot is None:
            self.text.setPlainText("")
            # "not kept" is a claim about F-42 retention.  A read that threw
            # has not established it, so it says what actually happened.
            self.text.setPlaceholderText(
                tr("The saved text could not be read.")
                if failure is not None
                else tr("The source text was not kept for this entry.")
            )
        else:
            self.text.setPlaceholderText("")
            self.text.setPlainText(snapshot)

        can_regenerate = bool(snapshot)
        self.play_button.setEnabled(play.playable)
        self.export_button.setEnabled(play.playable)
        self.regenerate_button.setEnabled(can_regenerate)
        self.delete_button.setEnabled(True)
        if failure is not None and not can_regenerate:
            self.caution.setText(
                tr("The saved text could not be read, so it cannot be generated again yet.")
            )
        elif not can_regenerate:
            self.caution.setText(tr("There is no saved text to generate again from."))
        else:
            self.caution.setText(
                tr(
                    "Regenerating makes a new entry. The old one is kept, and the new audio "
                    "will not be identical."
                )
            )

    def _recheck(self, summary: JobSummary, result: Result | None) -> ResultIntegrity | None:
        """F-45's detection for the one entry being opened.

        Shallow: a stat against the recorded byte size, which is what a
        forced termination actually leaves behind, and which does not read a
        hundred megabytes to draw a panel.  A recorded ``corrupt`` or
        ``missing`` is never re-run, because a size that happens to match
        would silently overwrite a digest failure with "fine".
        """
        if result is None:
            return None
        recorded = summary.result_integrity
        if recorded in (ResultIntegrity.MISSING, ResultIntegrity.CORRUPT):
            return recorded
        return self._store.verify_result(result.result_id, deep=False)

    def _facts(self, summary: JobSummary, result: Result | None) -> str:
        """F-39's stored facts, including the budget actually applied."""
        s = summary.settings
        model = s.model_id
        if self._manifest is not None and self._manifest.has(s.model_id):
            model = self._manifest.get(s.model_id).display_name
        parts = [
            f"{tr('Model')}: {model}",
            f"{tr('Voice')}: {s.voice_id} · {tr('Tempo')} {s.tempo:.2f}x",
            f"{tr('Origin')}: {summary.request_path.value}"
            + (f" ({summary.client_label})" if summary.client_label else ""),
            f"{tr('Started')}: {when_text(summary.started_at)}",
            f"{tr('Finished')}: {when_text(summary.ended_at)}",
            f"{tr('Segments')}: "
            + tr("{done} of {total}").format(
                done=count(summary.generated_segments), total=count(summary.total_segments)
            ),
        ]
        if summary.audio_duration_ms:
            audio = duration(summary.audio_duration_ms)
            if result is not None:
                audio += f" · {bytes_size(result.byte_size)}"
            parts.append(f"{tr('Audio')}: {audio}")
        if summary.budget is not None:
            b = summary.budget
            parts.append(
                f"{tr('Applied budget')}: {tr('CPU')} {b.cpu_percent}% · "
                f"{memory_size(b.memory_bytes)}"
            )
        if summary.error_code:
            parts.append(f"{summary.error_code}: {summary.error_message or ''}".strip(": "))
        return "\n".join(parts)

    # -- actions -------------------------------------------------------

    @property
    def job_id(self) -> str | None:
        return None if self._summary is None else self._summary.job_id

    def first_action(self) -> QPushButton:
        """The button Enter on a row should land on.

        Playing is offered first when there is something to play, and
        regenerating when there is not -- but neither happens on its own,
        which is exactly what F-45 forbids.
        """
        for button in (self.play_button, self.regenerate_button, self.delete_button):
            if button.isEnabled():
                return button
        return self.delete_button

    def _play(self) -> None:
        if self._summary is not None:
            self.play_requested.emit(self._summary.job_id)

    def _export(self) -> None:
        if self._summary is not None:
            self.export_requested.emit(self._summary.job_id)

    def _regenerate(self) -> None:
        if self._summary is not None:
            self.regenerate_requested.emit(self._summary.job_id)

    def _delete(self) -> None:
        if self._summary is not None:
            self.delete_requested.emit(self._summary.job_id)


class HistoryPane(QWidget):
    """The generation-history list, its filters, and the reopened entry."""

    play_job = Signal(str)
    export_job = Signal(str)
    regenerate_job = Signal(str)
    delete_requested = Signal(object)
    problem = Signal(object)

    def __init__(
        self,
        store: Store,
        palette: Palette,
        *,
        manifest: Manifest | None = None,
        clock: Callable[[], float] = ids.now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._palette = palette
        self._manifest = manifest
        self._clock = clock
        self._rows: dict[str, JobSummary] = {}
        m = METRICS

        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)

        self.filters = self._filter_row()
        box.addWidget(self.filters)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = _tree(
            (tr("When"), tr("Voice"), tr("State"), tr("Retention"), tr("Length")), tr("History")
        )
        split.addWidget(self.tree)

        self.detail = JobDetail(store, manifest=manifest, clock=clock)
        holder = QScrollArea()
        holder.setWidget(self.detail)
        holder.setWidgetResizable(True)
        holder.setFrameShape(QFrame.Shape.NoFrame)
        # N-30: at 200% scaling the panel is taller than the window, and a
        # scroll area is the layout change that keeps it reachable instead
        # of clipped.
        split.addWidget(holder)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        box.addWidget(split, 1)

        self.status = label("", "muted")
        self.status.setWordWrap(True)
        box.addWidget(self.status)

        self.pages = PageBar()
        box.addWidget(self.pages)

        self.tree.itemSelectionChanged.connect(self._on_selection)
        self.tree.itemActivated.connect(lambda *_: self._activate())
        self.pages.changed.connect(self.refresh)
        self.detail.play_requested.connect(self.play_job)
        self.detail.export_requested.connect(self.export_job)
        self.detail.regenerate_requested.connect(self.regenerate_job)
        self.detail.delete_requested.connect(lambda job_id: self._delete((job_id,)))
        self.detail.problem.connect(self.problem)

    def _filter_row(self) -> QWidget:
        """F-40's filters: date, model, and completion state.  Retention is
        here too, because F-42 requires the one-off/kept distinction to be
        visible and a filter is the cheapest way to answer "what is actually
        being kept?".

        Six controls in a row that cannot wrap would set the whole screen's
        minimum width -- N-30's 1280x720 at 200% scaling is 640 logical
        pixels wide and there is no sideways scrollbar to fall back on --
        so this is a :class:`WrapRow`.
        """
        m = METRICS
        holder = QWidget()
        row = WrapRow(holder)
        row.setSpacing(m.gap)

        self.date = QComboBox()
        self.date.addItem(tr("Any time"), None)
        self.date.addItem(tr("Today"), DATE_WINDOWS_DAYS[0])
        for days in DATE_WINDOWS_DAYS[1:]:
            self.date.addItem(tr("Last {n} days").format(n=count(days)), days)
        self.date.setAccessibleName(tr("When"))

        self.model = QComboBox()
        self.model.addItem(tr("Any model"), None)
        if self._manifest is not None:
            for entry in self._manifest.models:
                self.model.addItem(entry.display_name, entry.model_id)
        self.model.setAccessibleName(tr("Model"))

        self.state = QComboBox()
        self.state.addItem(tr("Any state"), None)
        self.state.addItem(tr("Complete"), (JobState.COMPLETE,))
        self.state.addItem(tr("Failed"), (JobState.FAILED,))
        self.state.addItem(tr("Canceled"), (JobState.CANCELED,))
        self.state.addItem(tr("Interrupted"), (JobState.INTERRUPTED,))
        self.state.addItem(
            tr("In progress"),
            tuple(s for s in JobState if s.is_active),
        )
        self.state.setAccessibleName(tr("State"))

        self.retention = QComboBox()
        self.retention.addItem(tr("Anything kept"), None)
        self.retention.addItem(tr("Kept"), RetentionMode.RETAINED)
        self.retention.addItem(tr("One-off"), RetentionMode.ONE_OFF)
        self.retention.setAccessibleName(tr("Retention"))

        for widget in (self.date, self.model, self.state, self.retention):
            widget.currentIndexChanged.connect(self._filters_changed)
            row.addWidget(widget)
        row.add_stretch()

        # F-43 offers deleting related entries together.  The rows the user
        # picked are the only relation this schema has: 4.2 and N-14 keep a
        # job's snapshot its own copy with no link back to a document, so
        # there is no set of "the jobs made from this document" to offer.
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.setAccessibleName(tr("Delete"))
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.delete_selected)
        row.addWidget(self.delete_button)

        self.delete_all_button = QPushButton(tr("Delete all history"))
        self.delete_all_button.setProperty("variant", "danger")
        self.delete_all_button.setAccessibleName(tr("Delete all history"))
        self.delete_all_button.clicked.connect(self._delete_all)
        row.addWidget(self.delete_all_button)
        return holder

    # -- data ----------------------------------------------------------

    def refresh(self) -> None:
        """One page of history, with no audio opened and no snapshot read.

        The one extra read per row is the expiry of a one-off result, which
        is a row in ``results`` and is what F-42 means by conveying when
        retention expires.  It is deliberately not derived from the policy
        TTL: a derived time would drift from the one the cleanup sweep
        actually uses.
        """
        keep = self.retention.currentData()
        states = self.state.currentData()
        days = self.date.currentData()
        now = self._clock()
        try:
            page = self._store.list_jobs(
                created_from=None if days is None else now - days * _SECONDS_PER_DAY,
                model_id=self.model.currentData(),
                states=states,
                retention=None if keep is None else RetentionMode(keep),
                limit=self.pages.page_size,
                offset=self.pages.offset,
            )
        except EchoActError as exc:
            self._fail(exc)
            return

        self.tree.clear()
        self._rows = {}
        for summary in page.items:
            self._rows[summary.job_id] = summary
            self.tree.addTopLevelItem(self._item(summary, now))
        if page.items:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            self.status.setText("")
        else:
            self.detail.show_job(None)
            self.status.setText(
                tr("No generation history yet")
                if self._filters_are_open()
                else tr("Nothing matches those filters")
            )
        self.pages.apply_page(total=page.total, offset=page.offset, shown=len(page.items))
        self.delete_all_button.setEnabled(page.total > 0)

    def _item(self, summary: JobSummary, now: float) -> QTreeWidgetItem:
        """One row.  The state cell carries the finding in words and the
        colour only reinforces it, which is what N-30 requires of a signal
        a colour-blind reader has to act on."""
        row = build_job_row(
            summary,
            model_name=self._model_name(summary.settings.model_id),
            expires_at=self._expiry(summary),
            now=now,
        )
        item = QTreeWidgetItem([row.when, row.voice, row.state, row.retention, row.length])
        item.setData(0, Qt.ItemDataRole.UserRole, row.job_id)
        if row.severity:
            item.setForeground(2, severity_colour(self._palette, row.severity))
        if row.reason:
            item.setToolTip(2, row.reason)
            item.setData(2, Qt.ItemDataRole.AccessibleDescriptionRole, row.reason)
        return item

    def _expiry(self, summary: JobSummary) -> float | None:
        if summary.retention is not RetentionMode.ONE_OFF or not summary.has_result:
            return None
        try:
            result = self._store.get_result_for_job(summary.job_id)
        except EchoActError:
            return None
        return None if result is None else result.expires_at

    def _model_name(self, model_id: str) -> str:
        if self._manifest is not None and self._manifest.has(model_id):
            return self._manifest.get(model_id).display_name
        return model_id

    def _filters_are_open(self) -> bool:
        return (
            self.date.currentData() is None
            and self.model.currentData() is None
            and self.state.currentData() is None
            and self.retention.currentData() is None
        )

    def _filters_changed(self) -> None:
        self.pages.reset()
        self.refresh()

    def _fail(self, exc: EchoActError) -> None:
        self.status.setText(f"{exc.code.value} — {exc.message}")
        self.problem.emit(exc)

    # -- selection and actions -----------------------------------------

    def selected_ids(self) -> tuple[str, ...]:
        return tuple(
            item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()
        )

    def select_job(self, job_id: str) -> bool:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == job_id:
                self.tree.setCurrentItem(item)
                return True
        return False

    def _on_selection(self) -> None:
        """Reopening is what selection means here (F-41).

        One entry's snapshot is read at a time, never a page of them, so
        arrowing down the list stays a list query plus one row.
        """
        chosen = self.selected_ids()
        if len(chosen) == 1:
            self.detail.show_job(self._rows.get(chosen[0]))
        else:
            self.detail.show_job(None)
        self.delete_button.setEnabled(bool(chosen))

    def _activate(self) -> None:
        """Enter on a row opens it and moves to its first action.

        It does not play.  F-45 wants nothing played automatically, and a
        list where Enter starts audio is exactly the surprise that rules
        out.
        """
        self._on_selection()
        if self.detail.job_id is not None:
            self.detail.first_action().setFocus(Qt.FocusReason.TabFocusReason)

    def delete_selected(self) -> None:
        chosen = self.selected_ids()
        if chosen:
            self._delete(chosen)

    def _delete(self, job_ids: Sequence[str]) -> None:
        try:
            scope = self._store.preview_deletion(job_ids=list(job_ids))
        except EchoActError as exc:
            self._fail(exc)
            return
        dialog = DeleteConfirmDialog(self._palette, scope, kind="jobs", parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.delete_requested.emit(
            DeleteRequest(job_ids=tuple(job_ids), audio_only=dialog.audio_only, scope=scope)
        )

    def _delete_all(self) -> None:
        """F-43's "delete all history", which is a scope of its own and
        leaves documents alone."""
        try:
            scope = self._store.preview_deletion(all_history=True)
        except EchoActError as exc:
            self._fail(exc)
            return
        dialog = DeleteConfirmDialog(
            self._palette, scope, kind="jobs", all_history=True, parent=self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.delete_requested.emit(DeleteRequest(all_history=True, scope=scope))


# ======================================================================
# The screen
# ======================================================================


class LibraryScreen(QWidget):
    """Documents and history, side by side in one widget.

    A plain :class:`QWidget` rather than a dialog or a page, because the
    window decides where it goes: F-79 requires the library to stay usable
    when the local service cannot start, which is easier to honour when the
    library is not welded to one container.
    """

    open_document = Signal(str)
    play_job = Signal(str)
    export_job = Signal(str)
    regenerate_job = Signal(str)
    delete_requested = Signal(object)
    problem = Signal(object)

    def __init__(
        self,
        store: Store,
        palette: Palette,
        *,
        manifest: Manifest | None = None,
        clock: Callable[[], float] = ids.now,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self._store = store
        self._palette = palette
        m = METRICS

        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)

        self.tabs = QTabWidget()
        self.documents = DocumentsPane(store, palette)
        self.history = HistoryPane(store, palette, manifest=manifest, clock=clock)
        self.tabs.addTab(self.documents, tr("Documents"))
        self.tabs.addTab(self.history, tr("History"))
        box.addWidget(self.tabs, 1)

        self.documents.open_document.connect(self.open_document)
        self.documents.delete_requested.connect(self.delete_requested)
        self.documents.problem.connect(self.problem)
        self.history.play_job.connect(self.play_job)
        self.history.export_job.connect(self.export_job)
        self.history.regenerate_job.connect(self.regenerate_job)
        self.history.delete_requested.connect(self.delete_requested)
        self.history.problem.connect(self.problem)

        self.refresh()

    def refresh(self) -> None:
        """Reload both lists.  Called after the window has carried out a
        deletion or created a job, so the screen never has to guess what
        changed."""
        self.documents.refresh()
        self.history.refresh()

    def show_documents(self) -> None:
        self.tabs.setCurrentWidget(self.documents)

    def show_history(self) -> None:
        self.tabs.setCurrentWidget(self.history)

    def select_job(self, job_id: str) -> bool:
        """Open one entry, for a window arriving from a completion notice."""
        self.show_history()
        return self.history.select_job(job_id)


def severity_colour(palette: Palette, severity: str) -> QColor:
    """The token a row's state cell is tinted with.

    Exposed so a caller that draws its own rows cannot invent a colour:
    N-13 and N-30 both require the word to carry the meaning, and this only
    reinforces it.
    """
    return qcolor({"warn": palette.warn, "danger": palette.danger}.get(severity, palette.text))


__all__ = [
    "DATE_WINDOWS_DAYS",
    "PAGE_SIZES",
    "SEARCH_DEBOUNCE_MS",
    "DeleteConfirmDialog",
    "DeleteRequest",
    "DocumentRow",
    "DocumentsPane",
    "HistoryPane",
    "JobDetail",
    "JobRow",
    "LibraryScreen",
    "PageBar",
    "Playability",
    "WrapRow",
    "build_document_row",
    "build_job_row",
    "playability",
    "retention_text",
    "scope_lines",
    "severity_colour",
    "state_text",
    "when_text",
]
