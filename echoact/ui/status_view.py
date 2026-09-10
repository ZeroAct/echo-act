"""F-69's job and service status screen, and the one place contention ends.

Everything F-69 lists is on this screen at once: the current job's request
path, owner and progress stage, the loaded model, the budget actually
applied, live usage, and the state of REST and MCP.  The two actions it
names -- cancel a job, stop an integration -- are here rather than behind a
menu, because of what A.3 decided and F-50 now says outright: requests carry
no priority by path, a GUI request does not preempt a client's running job,
and the owner reclaims the single slot by cancelling.  When a client holds
the slot this screen therefore says so in as many words and names the
client, in the spirit of F-70's completion notice, instead of leaving the
owner to infer it from a greyed-out button somewhere else.

Three things the screen refuses to simplify:

*Three budgets, not one.*  F-78 separates the configured value from the one
in force, and N-03 separates both from what the platform actually enforces.

*Two memory numbers.*  N-03 permits the figure on screen and the figure
limit decisions are made against to differ, and the supervisor reports which
source it read.  The screen shows the source rather than implying there is
only one.

*The poll does not stop when the job does.*  F-22 requires usage to keep
updating while idle with a model retained, which is exactly the state a warm
worker sits in between jobs (F-17).

Nothing here blocks the Qt main thread.  Cancelling kills a worker and waits
up to N-22's five seconds, stopping the service joins a server thread, and
the job list is a database read; all three run off the GUI thread and come
back as signals.  They share *one* worker thread rather than taking a new
one each (see :class:`_Background`), because ``Store`` holds a connection
per thread until exit and a five-second poll would otherwise leak one every
time it fired.  ``run_async`` is injectable so a test can run them inline.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import diagnostics
from ..domain import Budget, Job, JobState, RequestPath
from ..errors import Code, EchoActError
from ..policy import REST_HOST
from ..util.logging import get_logger
from . import icons
from .controls import label, separator
from .i18n import add_korean, count, memory_size, tr
from .theme import METRICS, Palette, mono_font

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..app import Application
    from ..engine.supervisor import WorkerUsage

log = get_logger("ui.status")

#: How often the screen re-reads usage.  A second is slower than the
#: container's own sampling interval (``RESOURCE_SAMPLE_INTERVAL_S``), so
#: every tick has something new to show without polling ahead of the data.
POLL_MS = 1000

#: The job list is a database read, so it happens on a slower cadence than
#: the in-memory figures -- and off the GUI thread either way.
JOBS_EVERY_TICKS = 5

#: How many rows F-50's "review and cancel all jobs" shows before the owner
#: has to go to the library for the rest.
JOB_ROWS = 12

add_korean(
    {
        "Status": "상태",
        "Current job": "현재 작업",
        "No job is running.": "실행 중인 작업이 없습니다.",
        "Request path": "요청 경로",
        "Owner": "소유자",
        "Stage": "단계",
        "Progress": "진행",
        "Started": "시작",
        "Budget applied": "적용된 예산",
        "This computer (you)": "이 컴퓨터(사용자)",
        "Accepted": "접수됨",
        "Canceling": "취소하는 중",
        "{client} is holding the generation slot.": "{client}이(가) 생성 슬롯을 사용 중입니다.",
        "Requests are not prioritised by path, so this job keeps the slot until it "
        "finishes or you cancel it here.": "요청 경로에 따른 우선순위는 없으므로, 이 작업은 "
        "끝나거나 여기서 취소할 때까지 슬롯을 차지합니다.",
        "Cancel this job": "이 작업 취소",
        "Cancelling…": "취소하는 중…",
        "All jobs": "모든 작업",
        "You can cancel any job here, including one an integration started.":
            "연동 클라이언트가 시작한 작업을 포함해 여기서 모든 작업을 취소할 수 있습니다.",
        "Cancel selected job": "선택한 작업 취소",
        "Requested by": "요청 주체",
        "When": "시각",
        "No jobs yet.": "아직 작업이 없습니다.",
        "Engine": "엔진",
        "Loaded model": "불러온 모델",
        "No model is loaded.": "불러온 모델이 없습니다.",
        "Selected model": "선택한 모델",
        "Sample rate": "샘플레이트",
        "Execution providers": "실행 공급자",
        "Load time": "불러오기 시간",
        "Budget and usage": "자원 예산과 사용량",
        "Configured": "설정값",
        "In force for the worker": "작업자에 적용 중",
        "For the running job": "실행 중인 작업",
        "Enforcement": "적용 방식",
        "Generation job usage": "생성 작업 사용량",
        "Measured by": "측정 출처",
        "{n} s ago": "{n}초 전",
        "Not measured yet.": "아직 측정되지 않았습니다.",
        "These figures are the generation job alone, not the whole app.":
            "이 수치는 앱 전체가 아니라 생성 작업만의 사용량입니다.",
        "Peak {size}.": "최고 {size}.",
        "Shown here is resident memory; limit decisions use {basis}.":
            "여기 표시된 값은 상주 메모리이며, 한도 판단에는 {basis}을(를) 사용합니다.",
        "enforced": "강제 적용",
        "monitored": "감시",
        "unavailable": "적용 안 됨",
        "commit charge": "커밋 메모리",
        "resident memory": "상주 메모리",
        "address space": "주소 공간",
        "no memory limit": "메모리 한도 없음",
        "Integrations": "연동",
        "Stop the service": "서비스 중지",
        "Start the service": "서비스 시작",
        "Disable MCP": "MCP 사용 안 함",
        "Enable MCP": "MCP 사용",
        "listening on {host}:{port}": "{host}:{port} 에서 수신 중",
        "Stopping an integration cancels the jobs it started; the app, generation and "
        "playback are unaffected.": "연동을 중지하면 해당 연동이 시작한 작업이 취소됩니다. "
        "앱과 생성, 재생에는 영향이 없습니다.",
        "MCP is started by the MCP client, so disabling it only refuses the next "
        "connection.": "MCP 서버는 MCP 클라이언트가 실행하므로, 사용 안 함으로 두면 다음 "
        "연결을 거부합니다.",
        "Refresh": "새로 고침",
        "Export diagnostics…": "진단 정보 내보내기…",
        "Diagnostic export": "진단 정보 내보내기",
        "Review this before saving it. It is written to a file you choose and sent "
        "nowhere.": "저장하기 전에 내용을 확인하세요. 선택한 위치의 파일로 저장되며 어디에도 "
        "전송되지 않습니다.",
        "It contains no document text, no audio, no credentials and no home directory "
        "path.": "본문, 오디오, 자격 증명, 사용자 홈 경로는 포함되지 않습니다.",
        "Save as…": "다른 이름으로 저장…",
        "Saved to {name}": "{name}에 저장했습니다",
        "Preparing the export…": "진단 정보를 모으는 중…",
        "Text files (*.txt)": "텍스트 파일 (*.txt)",
        "threads": "스레드",
        "peak": "최고",
    }
)

_STATE_TEXT: dict[JobState, str] = {
    JobState.ACCEPTED: "Accepted",
    JobState.PREPARING_MODEL: "Preparing the model",
    JobState.GENERATING: "Generating",
    JobState.CANCELING: "Canceling",
    JobState.COMPLETE: "Complete",
    JobState.FAILED: "Failed",
    JobState.CANCELED: "Canceled",
    JobState.INTERRUPTED: "Interrupted",
}

_PATH_TEXT: dict[RequestPath, str] = {
    RequestPath.GUI: "This computer (you)",
    RequestPath.REST: "REST",
    RequestPath.MCP: "MCP",
}

_ENFORCEMENT_TEXT = {
    "enforced": "enforced",
    "monitored": "monitored",
    "unavailable": "unavailable",
}

_BASIS_TEXT = {
    "commit": "commit charge",
    "resident": "resident memory",
    "address_space": "address space",
    "none": "no memory limit",
}


class _Background:
    """One long-lived worker thread for everything this screen does off the
    GUI thread.

    A thread per call would be shorter, and is what this used to be, but the
    threads are not the resource that matters.  :class:`~echoact.db.Store`
    keeps one SQLite connection per thread in a ``threading.local`` and
    appends it to a list that only ``Store.close()`` empties, at exit.  A new
    thread per poll therefore meant a new connection, a new WAL handle and a
    new permanent entry in that list every ``JOBS_EVERY_TICKS`` ticks -- some
    thousands of them over the soak A-20 asks for, none closed and none
    collectable.  One thread means one connection, however long the window
    stays open and whatever the screen is asked to do.

    Serialising the work is the deliberate second effect.  Everything queued
    here is bounded -- a cancel waits out N-22's five seconds, a service stop
    joins the server thread with its own timeout, a job list is one paged
    read -- so a queue cannot grow without limit the way an unbounded fan-out
    of threads can, and the screen's own polling stays behind whatever action
    the owner asked for rather than racing it.

    The thread is a daemon and is never joined: a reply that arrives after
    the window has gone is emitted on the :class:`_Signals` object the task
    itself holds, and Qt has already disconnected the view's slots by then.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._queue.put(fn)

    def _run(self) -> None:
        while True:
            fn = self._queue.get()
            try:
                fn()
            except Exception:  # noqa: BLE001 - one bad task never ends the thread
                # Every task here already reports its own failure through a
                # signal; this is the last resort that keeps the queue -- and
                # with it the one database connection -- alive regardless.
                log.exception("status background task failed")


#: The screen's single worker.  Module level rather than per view so that
#: rebuilding the screen does not start a second thread holding a second
#: connection, which is the leak this class exists to prevent.
_BACKGROUND = _Background("echoact-status")


def _default_run_async(fn: Callable[[], None]) -> None:
    _BACKGROUND.submit(fn)


class _Signals(QObject):
    """The worker thread's way back onto the GUI thread.

    A separate emitter rather than signals on the view, because a cancel
    can still be inside N-22's five seconds when the window closes: the
    worker holds its own reference to this object, so the emit is always
    into something alive, and Qt has already disconnected the view's slots
    by then, which turns a late reply into a no-op instead of a crash.
    """

    action_done = Signal(object)  # EchoActError | None
    jobs_ready = Signal(object)  # tuple[_Row, ...]
    export_ready = Signal(object, object)  # text | None, EchoActError | None


class _Row:
    """One line of the job table, read off the GUI thread."""

    __slots__ = ("job_id", "path", "owner", "state", "when", "active")

    def __init__(
        self, job_id: str, path: str, owner: str, state: JobState, when: float, active: bool
    ) -> None:
        self.job_id = job_id
        self.path = path
        self.owner = owner
        self.state = state
        self.when = when
        self.active = active


class StatusView(QWidget):
    """One screen for F-69, wired to :class:`~echoact.app.Application`."""

    changed = Signal()

    def __init__(
        self,
        app: Application,
        palette: Palette,
        parent: QWidget | None = None,
        *,
        run_async: Callable[[Callable[[], None]], None] = _default_run_async,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.palette_tokens = palette
        self._run_async = run_async
        self._busy = False
        self._ticks = 0
        self._rows: tuple[_Row, ...] = ()
        self._last_usage: WorkerUsage | None = None
        #: The last text a review dialog was opened on, for a caller that
        #: wants to know what was shown without reassembling it.
        self.last_export: str | None = None

        self._signals = _Signals()
        self._signals.action_done.connect(self._on_action_done)
        self._signals.jobs_ready.connect(self._on_jobs_ready)
        self._signals.export_ready.connect(self._on_export_ready)

        self._build()
        self.refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        m = METRICS
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(m.gap)

        head = QHBoxLayout()
        head.setSpacing(m.gap)
        head.addWidget(label(tr("Status"), "title"))
        head.addStretch(1)
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setProperty("variant", "quiet")
        self.refresh_button.setAccessibleName(tr("Refresh"))
        self.export_button = QPushButton(tr("Export diagnostics…"))
        self.export_button.setProperty("variant", "quiet")
        self.export_button.setAccessibleName(tr("Export diagnostics…"))
        head.addWidget(self.refresh_button)
        head.addWidget(self.export_button)
        outer.addLayout(head)

        self.message = label("", "warn")
        self.message.setWordWrap(True)
        self.message.hide()
        outer.addWidget(self.message)

        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, m.gap, 0)
        column.setSpacing(m.gap_wide)
        column.addWidget(self._job_card())
        column.addWidget(self._table_card())
        column.addWidget(self._engine_card())
        column.addWidget(self._budget_card())
        column.addWidget(self._service_card())
        column.addStretch(1)

        # N-30: at 1280x720 with 200% scaling this column is taller than the
        # window, and every part of it has to stay reachable.
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.export_button.clicked.connect(self.export_diagnostics)
        self.cancel_button.clicked.connect(self._cancel_current)
        self.cancel_selected.clicked.connect(self._cancel_selected)
        self.jobs.itemSelectionChanged.connect(self._update_enabled)
        self.rest_button.clicked.connect(self._toggle_rest)
        self.mcp_button.clicked.connect(self._toggle_mcp)

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        m = METRICS
        card = QFrame()
        card.setObjectName("Panel")
        box = QVBoxLayout(card)
        box.setContentsMargins(m.pad, m.pad, m.pad, m.pad)
        box.setSpacing(m.gap)
        box.addWidget(label(title, "section"))
        return card, box

    @staticmethod
    def _grid() -> QGridLayout:
        g = QGridLayout()
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(METRICS.pad)
        g.setVerticalSpacing(METRICS.gap_tight)
        g.setColumnStretch(1, 1)
        return g

    def _field(self, grid: QGridLayout, row: int, name: str) -> QLabel:
        """One ``name: value`` pair.  The name is a label, not a placeholder
        inside the value, so a screen reader reads the pair (N-30)."""
        title = label(name, "secondary")
        value = label("—")
        value.setWordWrap(True)
        value.setAccessibleName(name)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(title, row, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(value, row, 1)
        return value

    def _job_card(self) -> QFrame:
        card, box = self._card(tr("Current job"))

        self.contention = label("", "warn")
        self.contention.setWordWrap(True)
        self.contention.hide()
        box.addWidget(self.contention)

        grid = self._grid()
        self.job_path = self._field(grid, 0, tr("Request path"))
        self.job_owner = self._field(grid, 1, tr("Owner"))
        self.job_stage = self._field(grid, 2, tr("Stage"))
        self.job_started = self._field(grid, 3, tr("Started"))
        self.job_model = self._field(grid, 4, tr("Model"))
        self.job_budget = self._field(grid, 5, tr("Budget applied"))
        box.addLayout(grid)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setAccessibleName(tr("Progress"))
        box.addWidget(self.progress)
        self.progress_text = label("", "muted")
        box.addWidget(self.progress_text)

        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_button = QPushButton(tr("Cancel this job"))
        self.cancel_button.setProperty("variant", "danger")
        self.cancel_button.setAccessibleName(tr("Cancel this job"))
        row.addWidget(self.cancel_button)
        box.addLayout(row)
        return card

    def _table_card(self) -> QFrame:
        card, box = self._card(tr("All jobs"))
        note = label(
            tr("You can cancel any job here, including one an integration started."), "muted"
        )
        note.setWordWrap(True)
        box.addWidget(note)

        self.jobs = QTableWidget(0, 4)
        self.jobs.setHorizontalHeaderLabels(
            [tr("When"), tr("Requested by"), tr("Owner"), tr("Stage")]
        )
        self.jobs.setAccessibleName(tr("All jobs"))
        self.jobs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs.setAlternatingRowColors(True)
        self.jobs.verticalHeader().setVisible(False)
        self.jobs.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.jobs.horizontalHeader().setStretchLastSection(True)
        self.jobs.setMinimumHeight(160)
        box.addWidget(self.jobs)

        row = QHBoxLayout()
        self.jobs_empty = label(tr("No jobs yet."), "muted")
        row.addWidget(self.jobs_empty)
        row.addStretch(1)
        self.cancel_selected = QPushButton(tr("Cancel selected job"))
        self.cancel_selected.setProperty("variant", "danger")
        self.cancel_selected.setAccessibleName(tr("Cancel selected job"))
        row.addWidget(self.cancel_selected)
        box.addLayout(row)
        return card

    def _engine_card(self) -> QFrame:
        card, box = self._card(tr("Engine"))
        grid = self._grid()
        self.model_name = self._field(grid, 0, tr("Loaded model"))
        self.model_rate = self._field(grid, 1, tr("Sample rate"))
        self.model_providers = self._field(grid, 2, tr("Execution providers"))
        self.model_load = self._field(grid, 3, tr("Load time"))
        self.model_selected = self._field(grid, 4, tr("Selected model"))
        box.addLayout(grid)
        return card

    def _budget_card(self) -> QFrame:
        card, box = self._card(tr("Budget and usage"))
        grid = self._grid()
        self.budget_configured = self._field(grid, 0, tr("Configured"))
        self.budget_worker = self._field(grid, 1, tr("In force for the worker"))
        self.budget_job = self._field(grid, 2, tr("For the running job"))
        self.budget_enforcement = self._field(grid, 3, tr("Enforcement"))
        box.addLayout(grid)
        box.addWidget(separator())
        usage = self._grid()
        self.usage_value = self._field(usage, 0, tr("Generation job usage"))
        self.usage_source = self._field(usage, 1, tr("Measured by"))
        box.addLayout(usage)
        self.usage_note = label("", "muted")
        self.usage_note.setWordWrap(True)
        box.addWidget(self.usage_note)
        return card

    def _service_card(self) -> QFrame:
        card, box = self._card(tr("Integrations"))
        grid = self._grid()
        self.rest_state = self._field(grid, 0, tr("Local service"))
        self.mcp_state = self._field(grid, 1, "MCP")
        box.addLayout(grid)

        row = QHBoxLayout()
        row.setSpacing(METRICS.gap)
        row.addStretch(1)
        self.rest_button = QPushButton(tr("Stop the service"))
        self.rest_button.setProperty("variant", "quiet")
        self.mcp_button = QPushButton(tr("Disable MCP"))
        self.mcp_button.setProperty("variant", "quiet")
        row.addWidget(self.rest_button)
        row.addWidget(self.mcp_button)
        box.addLayout(row)

        note = label(
            tr(
                "Stopping an integration cancels the jobs it started; the app, generation "
                "and playback are unaffected."
            ),
            "muted",
        )
        note.setWordWrap(True)
        box.addWidget(note)
        mcp_note = label(
            tr("MCP is started by the MCP client, so disabling it only refuses the next "
               "connection."),
            "muted",
        )
        mcp_note.setWordWrap(True)
        box.addWidget(mcp_note)
        return card

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        self._ticks += 1
        self._refresh_job()
        self._refresh_usage()
        if self._ticks % JOBS_EVERY_TICKS == 0:
            self._load_jobs()

    def refresh(self) -> None:
        """Re-read everything, including the parts that cost a query.

        ``_refresh_usage`` refreshes the budget card as well, because the
        commit peak it reports belongs beside the enforcement basis and
        both have to change in the same paint.
        """
        self._refresh_job()
        self._refresh_engine()
        self._refresh_usage()
        self._refresh_service()
        self._load_jobs()

    def _refresh_job(self) -> None:
        job = self._current_job()
        if job is None:
            self.job_path.setText("—")
            self.job_owner.setText("—")
            self.job_stage.setText(tr("No job is running."))
            self.job_started.setText("—")
            self.job_model.setText("—")
            self.job_budget.setText("—")
            self.progress.setValue(0)
            self.progress_text.setText("")
            self.contention.hide()
            self._update_enabled()
            return

        self.job_path.setText(tr(_PATH_TEXT.get(job.request_path, str(job.request_path))))
        self.job_owner.setText(self._owner_text(job))
        self.job_stage.setText(tr(_STATE_TEXT.get(job.state, str(job.state))))
        self.job_started.setText(_clock(job.started_at or job.created_at))
        self.job_model.setText(job.settings.model_id)
        self.job_budget.setText(_budget_text(job.budget))
        self.progress.setValue(int(round(job.progress * 100)))
        self.progress_text.setText(
            f"{count(job.generated_segments)} / {count(job.total_segments)}"
            if job.total_segments
            else ""
        )
        self._show_contention(job)
        self._update_enabled()

    def _show_contention(self, job: Job) -> None:
        """A.3 and F-50, said plainly rather than implied.

        The owner cannot preempt a client's job and there is one slot, so
        the only useful thing this screen can tell them is who has it and
        that cancelling is the way to take it back.
        """
        if job.request_path is RequestPath.GUI or job.state.is_terminal:
            self.contention.hide()
            return
        who = job.client_label or job.owner_client_id or tr(_PATH_TEXT[job.request_path])
        self.contention.setText(
            tr("{client} is holding the generation slot.").format(client=who)
            + "  "
            + tr(
                "Requests are not prioritised by path, so this job keeps the slot until it "
                "finishes or you cancel it here."
            )
        )
        self.contention.show()

    def _refresh_engine(self) -> None:
        loaded = None
        try:
            loaded = self.app.supervisor.loaded_model
        except Exception:  # noqa: BLE001 - a status read never breaks the screen
            loaded = None
        if loaded is None:
            self.model_name.setText(tr("No model is loaded."))
            self.model_rate.setText("—")
            self.model_providers.setText("—")
            self.model_load.setText("—")
        else:
            self.model_name.setText(loaded.model_id)
            self.model_rate.setText(f"{count(loaded.sample_rate)} Hz")
            self.model_providers.setText(", ".join(loaded.providers) or "—")
            self.model_load.setText(f"{loaded.load_seconds:.2f} s")
        self.model_selected.setText(self.app.settings.voice.model_id)

    def _refresh_budget(self) -> None:
        state = diagnostics.collect_budget(self.app)
        self.budget_configured.setText(state.configured_error or _budget_text(state.configured))
        self.budget_worker.setText(_budget_text(state.in_force))
        self.budget_job.setText(_budget_text(state.running_job))
        memory = tr(_ENFORCEMENT_TEXT.get(state.memory_enforcement, state.memory_enforcement))
        cpu = tr(_ENFORCEMENT_TEXT.get(state.cpu_enforcement, state.cpu_enforcement))
        facility = state.facility or "—"
        self.budget_enforcement.setText(
            f"{tr('Memory')} {memory} · CPU {cpu} · {facility}"
        )
        basis = tr(_BASIS_TEXT.get(state.memory_basis, state.memory_basis))
        note = tr(
            "Shown here is resident memory; limit decisions use {basis}."
        ).format(basis=basis)
        usage = self._last_usage
        if usage is not None and usage.peak_commit_bytes:
            note += " " + tr("Peak {size}.").format(size=memory_size(usage.peak_commit_bytes))
        self.usage_note.setText(
            tr("These figures are the generation job alone, not the whole app.")
            + "  "
            + note
        )

    def _refresh_usage(self) -> None:
        """F-22, N-21 and N-03 in two lines.

        ``sample_usage`` rather than ``usage``: the supervisor offers it for
        exactly this caller, and forcing the sample means the figure on
        screen is this second's rather than the monitor thread's last one.

        The peak beside the current figure is the peak of the *same*
        quantity.  Putting a peak commit charge next to a resident-set
        figure would read as one number's history when it is another's,
        which is the conflation N-03 exists to prevent; the commit peak
        goes in the note, next to the sentence that says what it is for.
        """
        usage = None
        try:
            usage = self.app.supervisor.sample_usage()
        except Exception:  # noqa: BLE001
            usage = None
        self._last_usage = usage
        if usage is None:
            self.usage_value.setText(tr("Not measured yet."))
            self.usage_source.setText("—")
            self._refresh_budget()
            return
        self.usage_value.setText(
            f"CPU {usage.cpu_percent:.0f}%  ·  {tr('Memory')} {memory_size(usage.rss_bytes)}"
            f"  ·  {tr('peak')} {memory_size(usage.peak_rss_bytes)}"
        )
        self.usage_source.setText(
            f"{usage.source} · " + tr("{n} s ago").format(n=f"{usage.age_s:.0f}")
        )
        self._refresh_budget()

    def _service_running(self) -> bool:
        """Whether a listener is actually up, read once for both users.

        The label and the button's action have to come from the same
        reading, or N-09's "the current state is displayed identifiably"
        fails in the worst way: a control that says one thing and does
        another.
        """
        try:
            return bool(self.app.service_running)
        except Exception:  # noqa: BLE001
            return False

    def _refresh_service(self) -> None:
        s = self.app.settings
        running = self._service_running()
        if running:
            self.rest_state.setText(
                tr("listening on {host}:{port}").format(host=REST_HOST, port=s.rest_port)
            )
        elif s.rest_enabled:
            # F-79: enabled but not listening is the port-conflict case, and
            # it is a notice about the integrations, never about the app.
            self.rest_state.setText(tr("Integrations unavailable"))
        else:
            self.rest_state.setText(tr("off"))
        # The button follows the listener, not the setting.  In F-79's state
        # -- enabled, nothing listening -- the useful action is another go at
        # the port, so it offers to start; offering to stop there would turn
        # the integration off on the way to a retry the owner has to ask for
        # twice.
        self.rest_button.setText(
            tr("Stop the service") if running else tr("Start the service")
        )
        self.rest_button.setAccessibleName(self.rest_button.text())

        self.mcp_state.setText(tr("on") if s.mcp_enabled else tr("off"))
        self.mcp_button.setText(tr("Disable MCP") if s.mcp_enabled else tr("Enable MCP"))
        self.mcp_button.setAccessibleName(self.mcp_button.text())

    def _update_enabled(self) -> None:
        """N-09: an action that cannot be taken looks like it cannot."""
        job = self._current_job()
        self.cancel_button.setEnabled(
            not self._busy and job is not None and not job.state.is_terminal
        )
        row = self._selected_row()
        self.cancel_selected.setEnabled(not self._busy and row is not None and row.active)
        self.rest_button.setEnabled(not self._busy)
        self.mcp_button.setEnabled(not self._busy)
        self.export_button.setEnabled(not self._busy)

    def _current_job(self) -> Job | None:
        try:
            return self.app.engine.current()
        except Exception:  # noqa: BLE001
            return None

    def _owner_text(self, job: Job) -> str:
        if job.request_path is RequestPath.GUI:
            return tr("This computer (you)")
        return job.client_label or job.owner_client_id or "—"

    # ------------------------------------------------------------------
    # The job table (F-50)
    # ------------------------------------------------------------------

    def _load_jobs(self) -> None:
        signals = self._signals

        def work() -> None:
            rows: list[_Row] = []
            try:
                page = self.app.store.list_jobs(limit=JOB_ROWS)
                for item in page.items:
                    rows.append(
                        _Row(
                            job_id=item.job_id,
                            path=str(item.request_path),
                            owner=item.client_label or item.owner_client_id,
                            state=item.state,
                            when=item.ended_at or item.started_at or item.created_at,
                            active=item.state.is_active,
                        )
                    )
            except EchoActError:
                rows = []
            except Exception:  # noqa: BLE001 - the screen survives a bad read
                rows = []
            signals.jobs_ready.emit(tuple(rows))

        self._run_async(work)

    def _on_jobs_ready(self, rows: tuple[_Row, ...]) -> None:
        self._rows = rows
        selected = self._selected_row()
        keep = selected.job_id if selected else None
        self.jobs.setRowCount(len(rows))
        for i, row in enumerate(rows):
            path = tr(_PATH_TEXT.get(RequestPath(row.path), row.path)) if row.path else "—"
            values = (
                _clock(row.when),
                path,
                row.owner or "—",
                tr(_STATE_TEXT.get(row.state, str(row.state))),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, row.job_id)
                self.jobs.setItem(i, column, item)
            if row.job_id == keep:
                self.jobs.selectRow(i)
        self.jobs_empty.setVisible(not rows)
        self._update_enabled()

    def _selected_row(self) -> _Row | None:
        items = self.jobs.selectedItems()
        if not items:
            return None
        job_id = items[0].data(Qt.ItemDataRole.UserRole)
        for row in self._rows:
            if row.job_id == job_id:
                return row
        return None

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _cancel_current(self) -> None:
        job = self._current_job()
        if job is not None:
            self.cancel_job(job.job_id)

    def _cancel_selected(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.cancel_job(row.job_id)

    def cancel_job(self, job_id: str) -> None:
        """F-50 and A.3: the owner cancels any job, whoever asked for it.

        Off the GUI thread because :meth:`JobEngine.cancel` kills the worker
        and then waits for N-22's five-second release; on the main thread
        that would freeze the window for the whole of it.
        """
        if self._busy:
            return
        self._begin(tr("Cancelling…"))
        signals = self._signals
        engine = self.app.engine

        def work() -> None:
            error: EchoActError | None = None
            try:
                engine.cancel(job_id)
            except EchoActError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001
                error = EchoActError(Code.INTERNAL, f"Cancelling failed ({type(exc).__name__}).")
            signals.action_done.emit(error)

        self._run_async(work)

    def _toggle_rest(self) -> None:
        """F-69's "stop integrations", and F-79's retry.

        What the button does follows the listener, not ``rest_enabled``:
        with the setting on and nothing listening -- F-79's bind failure --
        this starts the service again and reports the code if the port is
        still taken, which is the one action worth offering in that state.

        Stopping is ``stop_service`` and nothing besides.  F-52's three
        steps are :meth:`ServiceRunner.stop`'s own, in the order the
        requirement gives them, and the step that blocks new requests is the
        ``drain`` it begins with -- ``rest_enabled`` is a startup setting
        that no request path reads.  Cancelling the integration's job from
        here first would therefore run the cancellation *outside* the drain
        and free the slot while the service was still admitting requests,
        which is precisely the window F-52's ordering exists to close, and
        would charge the owner two five-second cancellations for one click.

        The setting is written before the stop because it is about the next
        launch rather than this listener, and a settings write that is going
        to fail should fail before anything has been torn down.
        """
        turning_off = self._service_running()
        self._begin("")
        app = self.app
        signals = self._signals

        def work() -> None:
            error: EchoActError | None = None
            try:
                if turning_off:
                    app.update_settings(rest_enabled=False)
                    app.stop_service()
                else:
                    app.update_settings(rest_enabled=True)
                    problem = app.start_service()
                    if problem is not None:
                        error = EchoActError(problem.code, problem.message)
            except EchoActError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001
                error = EchoActError(
                    Code.INTERNAL, f"The service did not respond ({type(exc).__name__})."
                )
            signals.action_done.emit(error)

        self._run_async(work)

    def _toggle_mcp(self) -> None:
        """MCP has no listener of ours to stop (F-52: the client starts it),
        so turning it off is the setting plus the cancellation F-52 requires
        of the jobs that integration already started."""
        enable = not self.app.settings.mcp_enabled
        self._begin("")
        app = self.app
        signals = self._signals

        def work() -> None:
            error: EchoActError | None = None
            try:
                app.update_settings(mcp_enabled=enable)
                if not enable:
                    _cancel_path(app, RequestPath.MCP)
            except EchoActError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001
                error = EchoActError(
                    Code.INTERNAL, f"The setting was not saved ({type(exc).__name__})."
                )
            signals.action_done.emit(error)

        self._run_async(work)

    def _begin(self, note: str) -> None:
        self._busy = True
        self.message.setVisible(bool(note))
        self.message.setText(note)
        self._update_enabled()

    def _on_action_done(self, error: EchoActError | None) -> None:
        self._busy = False
        if error is None:
            self.message.hide()
            self.message.setText("")
        else:
            self.message.setText(f"{error.code.value} — {error.message}")
            self.message.show()
        self.refresh()
        self.changed.emit()

    # ------------------------------------------------------------------
    # Diagnostics (F-72)
    # ------------------------------------------------------------------

    def export_diagnostics(self) -> None:
        """Collect off the GUI thread, then show it for review.

        F-72 exports only after the user has reviewed the contents, so this
        opens a dialog and writes nothing; :func:`echoact.diagnostics.save`
        runs from the dialog's own button.
        """
        if self._busy:
            return
        self._begin(tr("Preparing the export…"))
        app = self.app
        signals = self._signals

        def work() -> None:
            try:
                text = diagnostics.export_text(app)
            except EchoActError as exc:
                signals.export_ready.emit(None, exc)
                return
            except Exception as exc:  # noqa: BLE001
                signals.export_ready.emit(
                    None,
                    EchoActError(
                        Code.INTERNAL,
                        f"The report could not be assembled ({type(exc).__name__}).",
                    ),
                )
                return
            signals.export_ready.emit(text, None)

        self._run_async(work)

    def _on_export_ready(self, text: str | None, error: EchoActError | None) -> None:
        self._busy = False
        self.message.hide()
        self._update_enabled()
        if error is not None:
            self.message.setText(f"{error.code.value} — {error.message}")
            self.message.show()
            return
        if text is None:
            return
        self.last_export = text
        dialog = DiagnosticsDialog(text, self.palette_tokens, self)
        dialog.exec()

    # ------------------------------------------------------------------
    # Qt lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt override
        # Nothing is on screen to update, and F-22's requirement is about
        # what the user can see.  The container keeps sampling regardless,
        # so the next show is current within one interval.
        self._timer.stop()
        super().hideEvent(event)


class DiagnosticsDialog(QDialog):
    """F-72's review step: the whole export, before anything is written.

    The text is shown in full rather than summarised.  A summary would mean
    the user reviewed one document and saved a different one, which is not
    the review the requirement asks for.
    """

    saved = Signal(str)

    def __init__(
        self,
        text: str,
        palette: Palette,
        parent: QWidget | None = None,
        *,
        choose_path: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._palette = palette
        self._choose_path = choose_path or self._ask_for_path
        m = METRICS

        self.setWindowTitle(tr("Diagnostic export"))
        self.setModal(True)
        self.setMinimumSize(760, 560)

        box = QVBoxLayout(self)
        box.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad_wide)
        box.setSpacing(m.gap)

        head = QHBoxLayout()
        head.setSpacing(m.gap)
        mark = QLabel()
        mark.setPixmap(icons.pixmap("info", palette.accent, 20, self.devicePixelRatioF()))
        mark.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(mark)
        intro = QLabel(
            tr("Review this before saving it. It is written to a file you choose and sent "
               "nowhere.")
        )
        intro.setWordWrap(True)
        head.addWidget(intro, 1)
        box.addLayout(head)
        excluded = QLabel(
            tr("It contains no document text, no audio, no credentials and no home directory "
               "path.")
        )
        excluded.setWordWrap(True)
        excluded.setProperty("role", "muted")
        box.addWidget(excluded)

        self.view = QPlainTextEdit()
        self.view.setPlainText(text)
        # The report is column-aligned; a proportional face turns its
        # tables into ragged prose the reader has to decode twice.
        self.view.setFont(mono_font())
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setAccessibleName(tr("Diagnostic export"))
        box.addWidget(self.view, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setProperty("role", "muted")
        box.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton(
            tr("Save as…"), QDialogButtonBox.ButtonRole.ActionRole
        )
        self.save_button.setProperty("variant", "primary")
        self.close_button = buttons.addButton(tr("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        self.save_button.clicked.connect(self.save)
        box.addWidget(buttons)

    def text(self) -> str:
        return self._text

    def _ask_for_path(self) -> str | None:
        """Offer a destination the user will find again.

        The suggestion starts in the user's own directory rather than the
        app's data tree: F-72 exports so that the file can be sent to
        someone, and a file saved into ``<data>`` is one the sender has to
        be told how to find.
        """
        try:
            start = Path.home() / diagnostics.default_filename()
        except (OSError, RuntimeError):
            start = Path(diagnostics.default_filename())
        chosen, _filter = QFileDialog.getSaveFileName(
            self, tr("Save as…"), str(start), tr("Text files (*.txt)")
        )
        return chosen or None

    def save(self) -> None:
        """Write what is on screen, to a location the user picks.

        The destination is the user's, not the app's: F-72 exports for the
        user to send on, and writing into the data directory by default
        would hide the file in the one place they are least likely to look.
        """
        chosen = self._choose_path()
        if not chosen:
            return
        try:
            path = diagnostics.save(self._text, chosen)
        except EchoActError as exc:
            self.status.setProperty("role", "danger")
            self.status.setText(f"{exc.code.value} — {exc.message}")
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
            return
        self.status.setText(tr("Saved to {name}").format(name=path.name))
        self.saved.emit(str(path))


# ======================================================================
# Helpers
# ======================================================================


def _cancel_path(app: Application, path: RequestPath) -> None:
    """Cancel the running job if that integration owns it (F-52).

    Only the running job: with one slot (F-47) there is at most one, and a
    job that has already finished is not something an integration is still
    doing.
    """
    job = app.engine.current()
    if job is not None and job.request_path is path and not job.state.is_terminal:
        app.engine.cancel(job.job_id)


def _budget_text(budget: Budget | None) -> str:
    if budget is None:
        return "—"
    return (
        f"CPU {budget.cpu_percent}%  ·  {memory_size(budget.memory_bytes)}"
        f"  ·  {budget.intra_op_threads}+{budget.inter_op_threads} {tr('threads')}"
    )


def _clock(value: float | None) -> str:
    """Wall-clock time of day.  A date is noise on a screen showing the
    last few minutes of activity; the export carries the full stamp."""
    if not value:
        return "—"
    from datetime import datetime

    try:
        return datetime.fromtimestamp(value).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


__all__ = ["DiagnosticsDialog", "StatusView"]
