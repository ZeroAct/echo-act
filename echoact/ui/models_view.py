"""The model management screen: F-63, F-64, F-65, F-09, F-84, and the
review half of F-80's licence surface.

One card per manifest entry, and never fewer than the manifest has.  F-04
forbids hiding a model that cannot run under the current budget and forbids
substituting another for it, so a card that cannot run is drawn exactly like
one that can, with :meth:`~echoact.models.registry.ModelRegistry.can_run`'s
reason printed under the name.  The list is therefore built from the
manifest, not from what happens to be on disk or to fit.

Two things this screen does not do.  It does not decide anything the job
engine owns: F-65 requires a model in use to be released by cancelling the
job first, and the registry cannot see jobs, so the card asks for a release
through :attr:`ModelCard.release_requested` and the window answers.  And it
does not present the licence for acceptance twice: :mod:`echoact.ui.licence`
owns the N-11 acceptance dialog and is used unchanged; what this screen adds
is a *review* pane, because F-80 wants the restrictions readable afterwards
and not only once, at the moment they are agreed to.

Everything the registry does that touches bytes runs in
:class:`RegistryTask`, a ``QThread``.  A deep verification pass hashes 385 MB
and a download moves it; either one on the GUI thread would freeze the
window for minutes, which rule 7 forbids outright.  Progress comes back as a
queued signal and is throttled at the source, because a per-chunk emit from
a fast local mirror can post events faster than the main thread drains them.
"""

from __future__ import annotations

import threading
import time
from enum import StrEnum

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..domain import Budget, Gender, RequestPath, SpeakingStyle
from ..errors import Code, EchoActError
from ..models.manifest import ModelEntry
from ..models.registry import (
    CancelToken,
    DownloadOutcome,
    DownloadPhase,
    DownloadProgress,
    ModelRegistry,
    ModelState,
    ModelStatus,
    VerifyReport,
)
from ..policy import TEMPO_MAX, TEMPO_MIN, WORKER_RELEASE_DEADLINE_S
from ..util.logging import get_logger
from . import icons
from .controls import label, separator
from .i18n import add_korean, bytes_size, count, memory_size, tr
from .licence import LicenceDialog
from .theme import METRICS, Palette

log = get_logger("ui.models")

#: Smallest gap between two progress repaints, in seconds.  Like
#: ``main_window.TICK_MS`` this is a repaint cadence rather than a limit, so
#: it lives with the widget that paints: the registry reports every chunk,
#: and a 385 MB transfer from a fast mirror would otherwise queue tens of
#: thousands of events onto the main thread to redraw a bar 400 pixels wide.
PROGRESS_INTERVAL_S = 0.04

#: How long the screen waits for a worker to notice its cancel token when
#: the window is closing.  N-22's five seconds, which is what the registry's
#: between-chunk cancellation checks are written against.
SHUTDOWN_WAIT_MS = int(WORKER_RELEASE_DEADLINE_S * 1000)

add_korean(
    {
        "Model cache uses {size}": "모델 캐시 사용량 {size}",
        "Not downloaded": "다운로드되지 않음",
        "Partially downloaded": "일부만 다운로드됨",
        "Damaged": "손상됨",
        "Version": "버전",
        "Source": "출처",
        "Storage": "저장 공간",
        "Languages": "언어",
        "Audio": "오디오",
        "Licence": "라이선스",
        "Accepted": "동의함",
        "Not accepted yet": "아직 동의하지 않음",
        "{used} of {total}": "{total} 중 {used}",
        "{rate} kHz mono": "{rate} kHz 모노",
        "Runs within the current budget": "현재 자원 한도에서 실행할 수 있습니다",
        "Unavailable: {reason}": "사용할 수 없음: {reason}",
        "Requires {size} of storage.": "저장 공간 {size}이(가) 필요합니다.",
        "{size} of it is still to download.": "그중 {size}을(를) 더 내려받아야 합니다.",
        "Needs at least {memory} of memory and {cpu}% CPU; the current budget is "
        "{have_memory} and {have_cpu}%.": "최소 메모리 {memory}, CPU {cpu}%가 필요합니다. "
        "현재 한도는 {have_memory}, {have_cpu}%입니다.",
        "These files are read from the supertonic package's cache. EchoAct never writes "
        "there, and Delete does not remove them.": "이 파일은 supertonic 패키지의 캐시에서 "
        "읽습니다. 에코액트는 그곳에 쓰지 않으며, 삭제해도 그 파일은 지우지 않습니다.",
        "Download": "다운로드",
        "Resume download": "다운로드 이어받기",
        "Retry download": "다운로드 다시 시도",
        "Check files": "파일 검사",
        "Repair": "복구",
        "Checking files": "파일 검사 중",
        "Checking {file}": "{file} 검사 중",
        "Downloading {file}": "{file} 내려받는 중",
        "{done} of {total}": "{total} 중 {done}",
        "All {n} files match the manifest.": "{n}개 파일이 모두 매니페스트와 일치합니다.",
        "{n} files do not match the manifest.": "{n}개 파일이 매니페스트와 일치하지 않습니다.",
        "{n} files are missing.": "{n}개 파일이 없습니다.",
        "The check was stopped. Nothing was changed.": "검사를 중지했습니다. 변경된 것은 "
        "없습니다.",
        "The download was canceled. The model is not ready.": "다운로드를 취소했습니다. "
        "모델은 준비되지 않았습니다.",
        "Downloaded {size}. {n} files already on disk were reused.": "{size}을(를) "
        "내려받았습니다. 이미 있던 파일 {n}개는 다시 사용했습니다.",
        "The model is ready.": "모델을 사용할 준비가 되었습니다.",
        "Nothing needed downloading.": "내려받을 것이 없습니다.",
        "Freed {size}. Documents and audio results were kept.": "{size}을(를) 확보했습니다. "
        "문서와 오디오 결과는 그대로 두었습니다.",
        "Delete this model?": "이 모델을 삭제할까요?",
        "Only EchoAct's own copy is removed. Documents and audio results are kept, and no "
        "other program's cache is touched.": "에코액트가 가진 사본만 지웁니다. 문서와 오디오 "
        "결과는 유지되며, 다른 프로그램의 캐시는 건드리지 않습니다.",
        "A job is using this model.": "실행 중인 작업이 이 모델을 사용하고 있습니다.",
        "Deleting it cancels that job first and releases the model. Documents and audio "
        "results are kept.": "삭제하려면 그 작업을 먼저 취소하고 모델을 해제합니다. 문서와 "
        "오디오 결과는 유지됩니다.",
        "Cancel the job and delete": "작업을 취소하고 삭제",
        "Keep": "유지",
        "The licence was not accepted, so nothing was downloaded.": "라이선스에 동의하지 않아 "
        "아무것도 내려받지 않았습니다.",
        "Voices and styles": "음성과 말하기 스타일",
        "Licence and restrictions": "라이선스와 제한 사항",
        "{n} voices": "음성 {n}개",
        "Speaking styles": "말하기 스타일",
        "Tempo range": "속도 범위",
        "Why these restrictions apply to you": "이 제한 사항이 적용되는 이유",
        "Allow apps to request this download": "앱이 이 다운로드를 요청하도록 허용",
        "Without this, a REST or MCP request cannot start a download.": "이 설정이 없으면 "
        "REST나 MCP 요청으로는 다운로드를 시작할 수 없습니다.",
    }
)

#: state -> (word, icon, palette role).  N-30: never the colour alone.
_STATE_TEXT: dict[ModelState, tuple[str, str, str]] = {
    ModelState.READY: ("Ready", "check-circle", "ok"),
    ModelState.PARTIAL: ("Partially downloaded", "warning", "warn"),
    ModelState.CORRUPT: ("Damaged", "error", "danger"),
    ModelState.NOT_PRESENT: ("Not downloaded", "download", "muted"),
}

_LANGUAGE_NAMES = {"ko": "Korean", "en": "English"}

_STYLE_NAMES = {
    SpeakingStyle.NATURAL: "Natural",
    SpeakingStyle.CALM: "Calm",
    SpeakingStyle.BRIGHT: "Bright",
    SpeakingStyle.NARRATION: "Narration",
}


class TaskKind(StrEnum):
    """Which registry operation a :class:`RegistryTask` is running."""

    VERIFY = "verify"
    DOWNLOAD = "download"
    REPAIR = "repair"


def _restyle(widget: QWidget, role: str | None) -> None:
    """Change a widget's ``role`` property after it has been polished.

    Qt resolves a stylesheet selector against a dynamic property once, when
    the widget is polished; setting the property later changes nothing on
    screen until the style is asked again.  Every status colour here is a
    role, and every one of them changes at runtime.
    """
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _short(revision: str) -> str:
    return revision[:12] if len(revision) > 12 else revision


class RegistryTask(QThread):
    """One registry operation, off the GUI thread (rule 7, F-64, F-65).

    A ``QThread`` rather than a pool: there is at most one operation per
    model, it owns a :class:`~echoact.models.registry.CancelToken` that has
    to outlive the call, and the window has to be able to wait for it on the
    way out.  Signals emitted from :meth:`run` cross to the main thread as
    queued connections because the object was created there.
    """

    progressed = Signal(object)  # DownloadProgress
    verified = Signal(object)  # VerifyReport
    prepared = Signal(object)  # DownloadOutcome
    failed = Signal(object)  # EchoActError

    def __init__(
        self,
        registry: ModelRegistry,
        model_id: str,
        kind: TaskKind,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.registry = registry
        self.model_id = model_id
        self.kind = kind
        self.token = CancelToken()
        #: The thread the work actually ran on.  Recorded because "not on
        #: the GUI thread" is a requirement rather than an implementation
        #: detail, and this is the only way to state it in a test.
        self.worker_ident: int | None = None
        self._last_emit = 0.0
        self._last_path: str | None = None

    def stop(self) -> None:
        """Ask the work to stop between chunks (F-64, N-22)."""
        self.token.cancel()

    def run(self) -> None:
        self.worker_ident = threading.get_ident()
        try:
            if self.kind is TaskKind.VERIFY:
                self.verified.emit(
                    self.registry.verify(
                        self.model_id, deep=True, cancel=self.token, progress=self._tick
                    )
                )
            elif self.kind is TaskKind.REPAIR:
                self.prepared.emit(
                    self.registry.repair(
                        self.model_id, self._tick, self.token, request_path=RequestPath.GUI
                    )
                )
            else:
                self.prepared.emit(
                    self.registry.download(
                        self.model_id, self._tick, self.token, request_path=RequestPath.GUI
                    )
                )
        except EchoActError as exc:
            self.failed.emit(exc)
        except Exception as exc:
            # A worker thread that dies with a traceback leaves the screen
            # spinning for ever, so anything unexpected becomes the one
            # exception type the app knows how to show.  Only the kind, the
            # model and the exception class are logged: N-20 allows a code
            # and an identifier, not free text.
            log.warning("%s failed for %s: %s", self.kind.value, self.model_id, type(exc).__name__)
            self.failed.emit(EchoActError(Code.INTERNAL, cause=exc))

    def _tick(self, progress: DownloadProgress) -> None:
        """Throttle progress at the source, on the worker thread.

        Dropping a tick costs nothing -- each one carries absolute totals,
        not a delta -- while posting every one of them would fill the main
        thread's queue with repaints of the same bar.  A change of file or a
        terminal phase always goes through, so the text never lags behind
        the work by a whole file.
        """
        now = time.monotonic()
        terminal = progress.phase in (DownloadPhase.COMPLETE, DownloadPhase.CANCELLED)
        if (
            terminal
            or progress.relative_path != self._last_path
            or now - self._last_emit >= PROGRESS_INTERVAL_S
        ):
            self._last_emit = now
            self._last_path = progress.relative_path
            self.progressed.emit(progress)


class ModelCard(QFrame):
    """One model, as F-63 lists it and F-64 and F-65 act on it."""

    licence_accepted = Signal(str)
    prepared = Signal(str)
    deleted = Signal(str)
    #: F-65: the model is held by a running job and the owner confirmed
    #: cancelling it.  The registry cannot see jobs, so the window does this.
    release_requested = Signal(str)
    problem = Signal(object)  # EchoActError
    busy_changed = Signal(bool)

    def __init__(
        self,
        palette: Palette,
        registry: ModelRegistry,
        entry: ModelEntry,
        budget: Budget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self._palette = palette
        self._registry = registry
        self._entry = entry
        self._budget = budget
        self._status: ModelStatus | None = None
        self._task: RegistryTask | None = None
        self._in_use = False
        self._failed = False
        #: Evidence from the last operation that established something --
        #: a completed deep pass, an interrupted download, a stopped check
        #: that found damage -- which outranks the cheap presence-and-size
        #: pass a redraw would use.  Never a verdict an attempt merely
        #: failed to reach: see :meth:`_on_verified`.
        self._pinned_state: ModelState | None = None

        self._build()
        self.reload()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        m = METRICS
        p = self._palette
        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad_wide)
        box.setSpacing(m.gap)

        head = QHBoxLayout()
        head.setSpacing(m.gap)
        head.addWidget(label(self._entry.display_name, "title"))
        self.state_icon = QLabel()
        self.state_text = label("", "muted")
        head.addWidget(self.state_icon)
        head.addWidget(self.state_text)
        head.addStretch(1)
        box.addLayout(head)

        # F-04: whether the model can run stands on its own line, in the same
        # place whether the answer is yes or no, so no layout accident can
        # bury the reason.
        avail = QHBoxLayout()
        avail.setSpacing(m.gap)
        self.avail_icon = QLabel()
        self.avail_text = label("", "secondary")
        self.avail_text.setWordWrap(True)
        avail.addWidget(self.avail_icon, 0, Qt.AlignmentFlag.AlignTop)
        avail.addWidget(self.avail_text, 1)
        box.addLayout(avail)

        grid = QGridLayout()
        grid.setHorizontalSpacing(m.pad)
        grid.setVerticalSpacing(m.gap_tight)
        self._values: dict[str, QLabel] = {}
        rows = (
            ("version", tr("Version")),
            ("source", tr("Source")),
            ("storage", tr("Storage")),
            ("languages", tr("Languages")),
            ("audio", tr("Audio")),
            ("licence", tr("Licence")),
        )
        for row, (key, name) in enumerate(rows):
            grid.addWidget(label(name, "muted"), row, 0, Qt.AlignmentFlag.AlignTop)
            value = label("", "secondary")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setAccessibleName(name)
            grid.addWidget(value, row, 1)
            self._values[key] = value
        grid.setColumnStretch(1, 1)
        box.addLayout(grid)

        # F-63's "before downloading, report the required storage and the
        # execution constraints under the current resource budget".
        self.requirements = label("", "muted")
        self.requirements.setWordWrap(True)
        box.addWidget(self.requirements)

        self.package_note = label(
            tr(
                "These files are read from the supertonic package's cache. EchoAct never "
                "writes there, and Delete does not remove them."
            ),
            "muted",
        )
        self.package_note.setWordWrap(True)
        self.package_note.hide()
        box.addWidget(self.package_note)

        self.progress_row = QWidget()
        prow = QVBoxLayout(self.progress_row)
        prow.setContentsMargins(0, 0, 0, 0)
        prow.setSpacing(m.gap_tight)
        self.progress_text = label("", "secondary")
        self.progress_text.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName(tr("Download"))
        prow.addWidget(self.progress_text)
        prow.addWidget(self.progress)
        self.progress_row.hide()
        box.addWidget(self.progress_row)

        self.result = label("", "muted")
        self.result.setWordWrap(True)
        box.addWidget(self.result)

        actions = QHBoxLayout()
        actions.setSpacing(m.gap)
        self.download_button = QPushButton(tr("Download"))
        self.download_button.setProperty("variant", "primary")
        self.download_button.setIcon(icons.icon("download", p.text_on_accent))
        self.verify_button = QPushButton(tr("Check files"))
        self.verify_button.setIcon(icons.icon("check-circle", p.text_secondary, p.text_muted))
        self.repair_button = QPushButton(tr("Repair"))
        self.repair_button.setIcon(icons.icon("refresh", p.text_secondary, p.text_muted))
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setProperty("variant", "danger")
        self.delete_button.setIcon(icons.icon("trash", p.danger, p.text_muted))
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setProperty("variant", "quiet")
        self.cancel_button.setIcon(icons.icon("close", p.text_secondary, p.text_muted))
        for button in (
            self.download_button,
            self.verify_button,
            self.repair_button,
            self.delete_button,
            self.cancel_button,
        ):
            button.setIconSize(icons.icon_size(16))
            button.setAccessibleName(button.text())
            actions.addWidget(button)
        actions.addStretch(1)
        box.addLayout(actions)

        # Section 5.3: an integration may only start a download for a model
        # the owner authorised in the GUI first, so the switch belongs beside
        # the download it authorises.
        self.authorise = QCheckBox(tr("Allow apps to request this download"))
        self.authorise.setToolTip(tr("Without this, a REST or MCP request cannot start a download."))
        box.addWidget(self.authorise)

        box.addWidget(separator())

        self.details_button, self.details = self._pane(box, tr("Voices and styles"))
        self._fill_details()
        self.licence_button, self.licence_pane = self._pane(box, tr("Licence and restrictions"))
        self._fill_licence()

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self.download_button.clicked.connect(self._on_download)
        self.verify_button.clicked.connect(self._on_verify)
        self.repair_button.clicked.connect(self._on_repair)
        self.delete_button.clicked.connect(self._on_delete)
        self.cancel_button.clicked.connect(self._on_cancel)
        self.authorise.toggled.connect(self._on_authorise)

    def _pane(self, box: QVBoxLayout, title: str) -> tuple[QPushButton, QWidget]:
        """A disclosure button and the panel it shows.

        Collapsed by default: the shipped licence has thirteen restrictions
        and the model has ten voices, and a screen that opens with all of
        that is one nobody reads.  F-80 asks for them to be reviewable, not
        unavoidable.
        """
        p = self._palette
        button = QPushButton("  " + title)
        button.setProperty("variant", "quiet")
        button.setCheckable(True)
        button.setIcon(icons.icon("chevron-right", p.text_secondary, p.text_muted))
        button.setIconSize(icons.icon_size(14))
        button.setAccessibleName(title)
        row = QHBoxLayout()
        row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)

        panel = QWidget()
        panel.hide()
        box.addWidget(panel)

        def toggled(open_: bool) -> None:
            panel.setVisible(open_)
            glyph = "chevron-down" if open_ else "chevron-right"
            button.setIcon(icons.icon(glyph, p.text_secondary, p.text_muted))

        button.toggled.connect(toggled)
        return button, panel

    def _fill_details(self) -> None:
        """F-63: the voice and speaking-style controls the model offers.

        The styles are the app's four (F-08) rather than a per-model list:
        the manifest records voices but not styles, so claiming a per-model
        set here would be inventing one.
        """
        m = METRICS
        entry = self._entry
        box = QVBoxLayout(self.details)
        box.setContentsMargins(m.pad, 0, 0, m.gap)
        box.setSpacing(m.gap_tight)
        box.addWidget(label(tr("{n} voices").format(n=count(len(entry.voices))), "section"))
        for gender in (Gender.FEMALE, Gender.MALE):
            for voice in entry.voices_for(gender):
                # F-53 puts the description in front of a person too, not
                # only in the API answer.
                line = label(f"·  {voice.display_name} — {voice.description}", "muted")
                line.setWordWrap(True)
                box.addWidget(line)
        styles = ", ".join(tr(_STYLE_NAMES[style]) for style in SpeakingStyle)
        box.addWidget(label(f"{tr('Speaking styles')}: {styles}", "muted"))
        box.addWidget(label(f"{tr('Tempo range')}: {TEMPO_MIN:.2f}x – {TEMPO_MAX:.2f}x", "muted"))

    def _fill_licence(self) -> None:
        """F-80 and N-11: the restrictions, reviewable after acceptance.

        The acceptance dialog in :mod:`echoact.ui.licence` is shown once,
        before first preparation.  F-80 also requires licences to be
        reviewable inside the app, and a term that can only be read at the
        moment it is agreed to is not reviewable -- so the same text,
        quoted rather than paraphrased, is on the screen that owns the model.
        """
        m = METRICS
        terms = self._entry.license
        box = QVBoxLayout(self.licence_pane)
        box.setContentsMargins(m.pad, 0, 0, m.gap)
        box.setSpacing(m.gap_tight)
        box.addWidget(label(terms.name, "section"))
        box.addWidget(label(tr("You may not use the model to:"), "secondary"))
        for restriction in terms.restrictions:
            box.addWidget(self._quote(restriction))
        if terms.notes:
            box.addWidget(label(tr("Also worth knowing"), "secondary"))
            for note in terms.notes:
                box.addWidget(self._quote(note))
        box.addWidget(label(tr("Why these restrictions apply to you"), "secondary"))
        box.addWidget(self._quote(terms.pass_through_obligation))
        box.addWidget(
            label(
                tr("The full licence is in {file} in the model's repository.").format(
                    file=terms.source_file
                ),
                "muted",
            )
        )

    @staticmethod
    def _quote(text: str) -> QLabel:
        lb = label("·  " + text, "muted")
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return lb

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self._entry.model_id

    @property
    def busy(self) -> bool:
        return self._task is not None

    @property
    def task(self) -> RegistryTask | None:
        """The running operation, for a caller that has to wait on it."""
        return self._task

    @property
    def status(self) -> ModelStatus | None:
        return self._status

    @property
    def state(self) -> ModelState | None:
        """What the card is showing, which is not always the cheap answer:
        a completed deep pass, an interrupted download, and a check that
        found damage before it stopped each pin their own evidence."""
        if self._pinned_state is not None:
            return self._pinned_state
        return self._status.state if self._status else None

    def set_budget(self, budget: Budget) -> None:
        """F-78: the budget changed, so what can run may have changed."""
        self._budget = budget
        self.reload()

    def set_in_use(self, in_use: bool) -> None:
        """F-65: whether a running job is holding this model."""
        self._in_use = in_use
        self._update_buttons()

    def reload(self) -> None:
        """Re-read the model's state and redraw.

        A presence-and-size pass, not a digest one: this screen redraws on
        every visit and on every budget change, and hashing 385 MB to do so
        would be exactly the freeze rule 7 forbids.  ``Check files`` is the
        deep pass, and it is explicit and cancellable.

        The cheap pass cannot *prove* a model sound, but it can disprove it:
        a file that is gone or the wrong size settles the question.  So a
        pinned deep verdict survives a redraw that agrees with it and is
        dropped by one that does not, which is what keeps a stale "ready"
        from outliving the files it was about.
        """
        try:
            self._status = self._registry.status(self.model_id, self._budget)
        except EchoActError as exc:
            self.problem.emit(exc)
            return
        if self._pinned_state is not None and self._status.state is not ModelState.READY:
            self._pinned_state = None
        self._render()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> None:
        status = self._status
        if status is None:
            return
        entry = self._entry
        p = self._palette
        dpr = self.devicePixelRatioF()

        state = self._pinned_state or status.state
        word, glyph, role = _STATE_TEXT[state]
        colour = {"ok": p.ok, "warn": p.warn, "danger": p.danger, "muted": p.text_muted}[role]
        self.state_icon.setPixmap(icons.pixmap(glyph, colour, 16, dpr))
        self.state_text.setText(tr(word))
        _restyle(self.state_text, role)
        self.state_text.setAccessibleName(f"{entry.display_name}: {tr(word)}")

        if status.runnable:
            self.avail_icon.setPixmap(icons.pixmap("check", p.ok, 14, dpr))
            self.avail_text.setText(tr("Runs within the current budget"))
            _restyle(self.avail_text, "secondary")
        else:
            self.avail_icon.setPixmap(icons.pixmap("warning", p.warn, 14, dpr))
            self.avail_text.setText(
                tr("Unavailable: {reason}").format(reason=status.unavailable_reason or "")
            )
            _restyle(self.avail_text, "warn")

        # F-84: the pinned revision is the version, and the full one is a
        # tooltip because twelve characters identify it and forty crowd the
        # row.
        self._values["version"].setText(_short(entry.revision))
        self._values["version"].setToolTip(entry.revision)
        self._values["source"].setText(entry.repo_id)
        self._values["storage"].setText(
            tr("{used} of {total}").format(
                used=bytes_size(max(status.disk_bytes, status.bytes_present)),
                total=bytes_size(status.bytes_total),
            )
        )
        self._values["languages"].setText(
            ", ".join(tr(_LANGUAGE_NAMES.get(code, code)) for code in status.languages)
        )
        self._values["audio"].setText(
            tr("{rate} kHz mono").format(rate=f"{entry.sample_rate / 1000:.1f}")
        )
        self._values["licence"].setText(
            f"{status.license_name} — "
            + tr("Accepted" if status.license_accepted else "Not accepted yet")
        )

        self.requirements.setText(self._requirements(status, state))
        self.requirements.setVisible(bool(self.requirements.text()))
        self.package_note.setVisible(status.using_package_cache)

        blocked = self.authorise.blockSignals(True)
        self.authorise.setChecked(status.download_authorised)
        self.authorise.blockSignals(blocked)

        self._update_buttons()

    def _requirements(self, status: ModelStatus, state: ModelState) -> str:
        """F-63's pre-download report: storage, then the budget constraint.

        Shown whenever the model is not ready, which is exactly the set of
        moments that are "before downloading".  The remaining figure is
        given as well as the total because F-64 reuses what is already
        sound, so the total would overstate what the next attempt costs.
        """
        if state is ModelState.READY:
            return ""
        parts = [tr("Requires {size} of storage.").format(size=bytes_size(status.bytes_total))]
        remaining = status.bytes_total - status.bytes_present
        if 0 < remaining < status.bytes_total:
            parts.append(tr("{size} of it is still to download.").format(size=bytes_size(remaining)))
        parts.append(
            tr(
                "Needs at least {memory} of memory and {cpu}% CPU; the current budget is "
                "{have_memory} and {have_cpu}%."
            ).format(
                memory=memory_size(status.minimum_memory_bytes),
                cpu=status.minimum_cpu_percent,
                have_memory=memory_size(self._budget.memory_bytes),
                have_cpu=self._budget.cpu_percent,
            )
        )
        return " ".join(parts)

    def _download_label(self) -> str:
        """F-64 names the three cases, and so does the button."""
        if self._failed:
            return tr("Retry download")
        if self.state is ModelState.PARTIAL:
            return tr("Resume download")
        return tr("Download")

    def _update_buttons(self) -> None:
        busy = self.busy
        state = self.state
        present = bool(self._status and self._status.disk_bytes > 0)
        self.download_button.setText(self._download_label())
        self.download_button.setAccessibleName(self.download_button.text())
        self.download_button.setVisible(state is not ModelState.READY)
        self.download_button.setEnabled(not busy)
        self.repair_button.setVisible(state in (ModelState.CORRUPT, ModelState.PARTIAL))
        self.repair_button.setEnabled(not busy)
        self.verify_button.setEnabled(not busy)
        # A model whose only sound copy is another program's cache has
        # nothing of ours to delete, and F-65 does not let this screen reach
        # into that cache.
        self.delete_button.setVisible(present)
        self.delete_button.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.authorise.setEnabled(not busy)

    def _set_result(self, text: str, role: str | None) -> None:
        self.result.setText(text)
        _restyle(self.result, role or "muted")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_authorise(self, allowed: bool) -> None:
        try:
            self._registry.set_download_authorised(self.model_id, allowed)
        except EchoActError as exc:
            self.problem.emit(exc)
            self.reload()

    def _on_download(self) -> None:
        if not self._licence_gate():
            return
        self._start(TaskKind.DOWNLOAD)

    def _on_repair(self) -> None:
        if not self._licence_gate():
            return
        self._start(TaskKind.REPAIR)

    def _on_verify(self) -> None:
        """F-65's check.  No licence gate: reading files that are already
        here to say whether they are sound is not preparing the model."""
        self._start(TaskKind.VERIFY)

    def _licence_gate(self) -> bool:
        """N-11: the terms are accepted before the model is first prepared.

        Asked here rather than left to the registry's refusal, because a
        code in a status line is not "presented as terms the user accepts".
        Declining is not an error: nothing is downloaded, and the card says
        so rather than showing a failure.
        """
        if not self._registry.license_acceptance_required(self.model_id):
            return True
        if not self.request_licence():
            self._set_result(tr("The licence was not accepted, so nothing was downloaded."), "warn")
            return False
        self._registry.accept_license(self.model_id)
        self.licence_accepted.emit(self.model_id)
        self.reload()
        return True

    def request_licence(self) -> bool:
        """Show N-11's acceptance dialog and report the answer.

        Its own method because a modal dialog spins an event loop of its
        own: nothing outside it can press its buttons, so a test replaces
        this one method and the flow around it stays real.
        """
        dialog = LicenceDialog(self._entry, self._palette, self)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def confirm(self, title: str, body: str, accept_text: str) -> bool:
        """A destructive-action confirmation, replaceable for the same
        reason as :meth:`request_licence`.

        The rejecting button is the default: F-65's delete is irreversible
        and 385 MB to fetch again.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(body)
        accept = box.addButton(accept_text, QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton(tr("Keep"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is accept

    def _on_delete(self) -> None:
        """F-65's delete, including the in-use case.

        The confirmation says what is *not* deleted, because that is the
        part of F-65 a user cannot check by looking: retained documents and
        audio results live under other roots, and the supertonic package's
        cache belongs to another program.
        """
        if self._in_use:
            if not self.confirm(
                tr("A job is using this model."),
                tr(
                    "Deleting it cancels that job first and releases the model. Documents "
                    "and audio results are kept."
                ),
                tr("Cancel the job and delete"),
            ):
                return
            # The window cancels the job and answers with set_in_use(False).
            # If it does not, the registry refuses below and says why, which
            # is better than this screen assuming a release it did not see.
            self.release_requested.emit(self.model_id)
        elif not self.confirm(
            tr("Delete this model?"),
            tr(
                "Only EchoAct's own copy is removed. Documents and audio results are kept, "
                "and no other program's cache is touched."
            ),
            tr("Delete"),
        ):
            return
        try:
            freed = self._registry.delete(self.model_id, in_use=self._in_use)
        except EchoActError as exc:
            self._set_result(exc.message, "danger")
            self.problem.emit(exc)
            return
        self._failed = False
        self._pinned_state = None
        self._set_result(
            tr("Freed {size}. Documents and audio results were kept.").format(
                size=bytes_size(freed)
            ),
            "muted",
        )
        self.deleted.emit(self.model_id)
        self.reload()

    def _on_cancel(self) -> None:
        if self._task is not None:
            self._task.stop()
            # Disabled rather than hidden: N-09 wants an unavailable control
            # to look unavailable, and the row must not resize while the
            # worker is winding down.
            self.cancel_button.setEnabled(False)

    # ------------------------------------------------------------------
    # The worker
    # ------------------------------------------------------------------

    def _start(self, kind: TaskKind) -> None:
        if self._task is not None:
            return
        self._failed = False
        # The pinned verdict is left alone: it is the best evidence there is
        # until this task produces its own, and dropping it here would show
        # a damaged model as ready for as long as the repair takes.
        self._set_result("", None)
        task = RegistryTask(self._registry, self.model_id, kind, self)
        task.progressed.connect(self._on_progress)
        task.verified.connect(self._on_verified)
        task.prepared.connect(self._on_prepared)
        task.failed.connect(self._on_failed)
        task.finished.connect(self._on_finished)
        self._task = task
        self.progress.setValue(0)
        self.progress_text.setText(tr("Checking files"))
        self.progress_row.setVisible(True)
        self.cancel_button.setEnabled(True)
        self._update_buttons()
        self.busy_changed.emit(True)
        task.start()

    def _on_progress(self, progress: DownloadProgress) -> None:
        if progress.phase is DownloadPhase.CHECKING:
            self.progress_text.setText(tr("Checking {file}").format(file=progress.relative_path))
        elif progress.phase is DownloadPhase.DOWNLOADING:
            self.progress_text.setText(
                tr("Downloading {file}").format(file=progress.relative_path)
                + "  ·  "
                + tr("{done} of {total}").format(
                    done=bytes_size(progress.bytes_done), total=bytes_size(progress.bytes_total)
                )
            )
        self.progress.setValue(int(progress.fraction * 100))

    def _on_verified(self, report: VerifyReport) -> None:
        # A deep pass that ran to the end is the strongest evidence there
        # is, so it outranks whatever the presence-and-size redraw after it
        # would conclude.  A pass that was *stopped* is not that: every file
        # it never reached is reported unchecked, an unchecked file reads as
        # absent, and so a check cancelled over a fully present, digest-sound
        # model collapses to NOT_PRESENT.  Pinning that would be a verdict
        # the card could never recover from -- the cheap pass that follows
        # says READY, and only a cheap pass that *disproves* READY drops a
        # pin -- so "Not downloaded" would outlive every reload, refresh and
        # budget change until some other operation happened to replace it.
        #
        # What a stopped pass can still prove is damage it saw before it
        # stopped, and that is the case the pin exists for at all: a
        # tampered file is the right size, so the cheap pass cannot see it.
        # Damage is therefore pinned and nothing else is, which leaves any
        # earlier verdict -- still the best evidence available -- in place.
        if not report.cancelled or report.damaged:
            self._pinned_state = report.state
        if report.cancelled:
            self._set_result(tr("The check was stopped. Nothing was changed."), "warn")
        elif report.state is ModelState.READY:
            self._set_result(
                tr("All {n} files match the manifest.").format(n=count(len(report.files))), "ok"
            )
        elif report.damaged:
            self._set_result(
                tr("{n} files do not match the manifest.").format(n=count(len(report.damaged))),
                "danger",
            )
        else:
            self._set_result(
                tr("{n} files are missing.").format(n=count(len(report.missing))), "warn"
            )

    def _on_prepared(self, outcome: DownloadOutcome) -> None:
        # Either way the state shown is the one the attempt established.
        # F-64: a cancelled download is never shown as ready, and the
        # interrupted report is what says so -- a fresh presence-and-size
        # pass over files whose digests nobody finished checking would not.
        self._pinned_state = outcome.report.state
        if outcome.cancelled:
            self._failed = True
            self._set_result(tr("The download was canceled. The model is not ready."), "warn")
            return
        if outcome.bytes_downloaded:
            self._set_result(
                tr("Downloaded {size}. {n} files already on disk were reused.").format(
                    size=bytes_size(outcome.bytes_downloaded), n=count(len(outcome.reused))
                )
                + " "
                + tr("The model is ready."),
                "ok",
            )
        else:
            self._set_result(
                tr("Nothing needed downloading.") + " " + tr("The model is ready."), "ok"
            )
        self.prepared.emit(self.model_id)

    def _on_failed(self, exc: EchoActError) -> None:
        self._failed = True
        self._set_result(exc.message, "danger")
        self.problem.emit(exc)

    def _on_finished(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.deleteLater()
        self.progress_row.hide()
        self.reload()
        self.busy_changed.emit(False)

    def shutdown(self) -> None:
        """Stop any work and wait for it, within N-22's five seconds."""
        task = self._task
        if task is None:
            return
        task.stop()
        if not task.wait(SHUTDOWN_WAIT_MS):
            # N-22's deadline passed.  Said out loud rather than waited out:
            # the window is closing, and a silent extra minute looks like a
            # hang to whoever asked for it.
            log.warning("model worker for %s did not stop within the deadline", self.model_id)


class ModelsView(QWidget):
    """F-63's screen: every model in the manifest, and what may be done to it.

    The view holds the registry because the registry is its subject, but it
    holds nothing else: anything needing the job engine or the rest of the
    application leaves as a signal, so there is still one place -- the
    window -- that knows what is running.
    """

    licence_accepted = Signal(str)
    model_prepared = Signal(str)
    model_deleted = Signal(str)
    release_requested = Signal(str)
    problem = Signal(object)  # EchoActError
    busy_changed = Signal(bool)

    def __init__(
        self,
        palette: Palette,
        registry: ModelRegistry,
        budget: Budget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self._registry = registry
        self._cards: dict[str, ModelCard] = {}

        m = METRICS
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad_wide)
        outer.setSpacing(m.gap_wide)

        head = QHBoxLayout()
        head.setSpacing(m.gap)
        head.addWidget(label(tr("Models"), "title"))
        head.addStretch(1)
        self.usage = label("", "muted")
        head.addWidget(self.usage)
        outer.addLayout(head)

        body = QWidget()
        self._list = QVBoxLayout(body)
        self._list.setContentsMargins(0, 0, m.gap, 0)
        self._list.setSpacing(m.gap_wide)
        # Built from the manifest, never from the disk or from what fits:
        # F-04 requires a model that cannot run to be listed with its
        # reason, and a list assembled from what is usable is exactly the
        # list that drops it.
        for entry in registry.manifest:
            card = ModelCard(palette, registry, entry, budget, body)
            card.licence_accepted.connect(self.licence_accepted)
            card.prepared.connect(self._on_prepared)
            card.deleted.connect(self._on_deleted)
            card.release_requested.connect(self.release_requested)
            card.problem.connect(self.problem)
            card.busy_changed.connect(self.busy_changed)
            self._cards[entry.model_id] = card
            self._list.addWidget(card)
        self._list.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        self._refresh_usage()

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    @property
    def cards(self) -> tuple[ModelCard, ...]:
        return tuple(self._cards.values())

    def card(self, model_id: str) -> ModelCard:
        return self._cards[model_id]

    @property
    def busy(self) -> bool:
        return any(card.busy for card in self._cards.values())

    # ------------------------------------------------------------------
    # What the window tells this screen
    # ------------------------------------------------------------------

    def set_budget(self, budget: Budget) -> None:
        for card in self._cards.values():
            card.set_budget(budget)

    def set_in_use(self, model_id: str | None) -> None:
        """F-65: which model, if any, a running job is holding."""
        for card in self._cards.values():
            card.set_in_use(card.model_id == model_id)

    def refresh(self) -> None:
        """Redraw from the registry, leaving any running operation alone."""
        for card in self._cards.values():
            if not card.busy:
                card.reload()
        self._refresh_usage()

    def shutdown(self) -> None:
        for card in self._cards.values():
            card.shutdown()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_prepared(self, model_id: str) -> None:
        self._refresh_usage()
        self.model_prepared.emit(model_id)

    def _on_deleted(self, model_id: str) -> None:
        self._refresh_usage()
        self.model_deleted.emit(model_id)

    def _refresh_usage(self) -> None:
        """F-73's model-cache figure, on the screen that changes it."""
        try:
            total = self._registry.total_disk_usage()
        except EchoActError as exc:
            self.problem.emit(exc)
            return
        self.usage.setText(tr("Model cache uses {size}").format(size=bytes_size(total)))


__all__ = ["ModelCard", "ModelsView", "RegistryTask", "TaskKind"]
