"""F-69's screen: everything visible at once, and the two actions on it.

The widget is exercised without a visible window -- ``grab()`` forces a
layout, which is all these assertions need and is what works on a platform
plugin with no font database.  Every action the screen takes runs through
an injected ``run_async``, so a test runs it inline and still proves the
production path is off the GUI thread (there is a test for that too).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from echoact import paths
from echoact.config.settings import Settings
from echoact.db.store import Store
from echoact.domain import (
    Budget,
    Gender,
    Job,
    JobKind,
    JobState,
    Language,
    RequestPath,
    RetentionMode,
    SpeakingStyle,
    VoiceSettings,
)
from echoact.engine.container import ContainerLimits, Enforcement, LimitBasis
from echoact.engine.supervisor import LoadedModel, WorkerUsage
from echoact.errors import Code, EchoActError, Problem
from echoact.models.catalog import MANIFEST, SUPERTONIC_3_ID
from echoact.models.registry import ModelRegistry
from echoact.security.credentials import CredentialStore
from echoact.ui import theme
from echoact.ui.i18n import memory_size
from echoact.ui.status_view import DiagnosticsDialog, StatusView, _default_run_async

T0 = 1_800_000_000.0
BUDGET = Budget(cpu_percent=20, memory_bytes=4 << 30, intra_op_threads=2)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Never touch the real user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    paths.ensure_tree()
    yield tmp_path
    paths.data_dir.cache_clear()


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    """Created lazily and never shown; ``grab()`` is enough to lay out."""
    return QApplication.instance() or QApplication([])


def _voice() -> VoiceSettings:
    return VoiceSettings(
        model_id=SUPERTONIC_3_ID,
        language=Language.KO,
        gender=Gender.FEMALE,
        voice_id=MANIFEST.get(SUPERTONIC_3_ID).voices_for(Gender.FEMALE)[0].voice_id,
        style=SpeakingStyle.NATURAL,
        tempo=1.0,
    )


def _job(
    job_id: str,
    *,
    path: RequestPath = RequestPath.GUI,
    label: str | None = None,
    state: JobState = JobState.GENERATING,
    generated: int = 2,
    total: int = 5,
) -> Job:
    return Job(
        job_id=job_id,
        kind=JobKind.SPEECH,
        request_path=path,
        owner_client_id="owner" if path is RequestPath.GUI else "cli_7",
        state=state,
        source_text="hello",
        settings=_voice(),
        budget=BUDGET,
        retention=RetentionMode.ONE_OFF,
        created_at=T0,
        started_at=T0 + 1,
        client_label=label,
        generated_segments=generated,
        total_segments=total,
    )


class FakeSupervisor:
    def __init__(self) -> None:
        self.limits = ContainerLimits(
            memory=Enforcement.ENFORCED,
            cpu=Enforcement.MONITORED,
            kill_on_close=Enforcement.ENFORCED,
            memory_basis=LimitBasis.COMMIT,
            memory_bytes=BUDGET.memory_bytes,
            cpu_percent=BUDGET.cpu_percent,
            facility="windows job object, hard CPU cap",
        )
        self.budget = BUDGET
        self.loaded_model = LoadedModel(
            model_id=SUPERTONIC_3_ID,
            model_dir="<data>/models",
            sample_rate=44100,
            voices=("F1",),
            providers=("CPUExecutionProvider",),
            load_seconds=0.82,
        )
        self.samples = 0
        self._usage = self._make(41.0, 1_200_000_000)

    def _make(self, cpu: float, rss: int) -> WorkerUsage:
        return WorkerUsage(
            rss_bytes=rss,
            cpu_percent=cpu,
            peak_rss_bytes=rss,
            peak_commit_bytes=rss + 200_000_000,
            limits=self.limits,
            source="job object",
            age_s=0.2,
        )

    def set_usage(self, cpu: float, rss: int) -> None:
        self._usage = self._make(cpu, rss)

    def usage(self) -> WorkerUsage:
        return self._usage

    def sample_usage(self) -> WorkerUsage:
        self.samples += 1
        return self._usage


@dataclass
class FakeEngine:
    job: Job | None = None
    cancelled: list[str] = field(default_factory=list)
    raises: EchoActError | None = None
    #: The app's ordered log, so a test can see whether a cancellation
    #: happened before or after the service began draining (F-52).
    events: list[str] = field(default_factory=list)

    def current(self) -> Job | None:
        return self.job

    def cancel(self, job_id: str) -> Job | None:
        if self.raises is not None:
            raise self.raises
        self.cancelled.append(job_id)
        self.events.append(f"cancel:{job_id}")
        if self.job is not None and self.job.job_id == job_id:
            self.job = None
        return None


@dataclass
class FakeStartup:
    problems: list[Problem] = field(default_factory=list)


class FakeApp:
    """The composition root's surface, as much of it as the screen touches."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(voice=_voice())
        self.store = Store(tmp_path / "db.sqlite3", audio_root=tmp_path / "audio")
        self.registry = ModelRegistry(MANIFEST, root=tmp_path / "models")
        self.credentials = CredentialStore.load()
        self.supervisor = FakeSupervisor()
        self.events: list[str] = []
        self.engine = FakeEngine(events=self.events)
        self.startup = FakeStartup()
        self.service_running = True
        self.start_problem: Problem | None = None
        self.stop_error: EchoActError | None = None

    def update_settings(self, **changes: Any) -> Settings:
        self.events.append("settings:" + ",".join(f"{k}={v}" for k, v in changes.items()))
        self.settings = self.settings.with_(**changes)
        return self.settings

    def stop_service(self) -> None:
        """``ServiceRunner.stop``'s contract, not just a flag.

        The real stop performs F-52's three steps by itself -- drain, cancel
        the jobs this integration started, shut the listener down -- so a
        fake that only flipped a boolean would let a caller that cancels
        first look identical to one that does not.
        """
        if self.stop_error is not None:
            raise self.stop_error
        self.events.append("drain")
        job = self.engine.current()
        if (
            job is not None
            and job.request_path is RequestPath.REST
            and not job.state.is_terminal
        ):
            self.engine.cancel(job.job_id)
        self.events.append("stop_service")
        self.service_running = False

    def start_service(self) -> Problem | None:
        self.events.append("start_service")
        if self.start_problem is not None:
            return self.start_problem
        self.service_running = True
        return None


@pytest.fixture()
def app(tmp_path):
    a = FakeApp(tmp_path)
    yield a
    a.store.close()


@pytest.fixture()
def view(qt_app, app):
    w = StatusView(app, theme.LIGHT, run_async=lambda fn: fn())
    w.resize(720, 900)
    w.ensurePolished()
    w.grab()  # lays out without needing a visible window
    yield w
    w.deleteLater()


def _seed_jobs(app: FakeApp) -> None:
    app.store.create_job(_job("job_gui", state=JobState.ACCEPTED))
    app.store.create_job(
        _job("job_rest", path=RequestPath.REST, label="Claude Desktop", state=JobState.ACCEPTED)
    )


# ------------------------------------------------------------------ F-69 ---


def test_the_screen_shows_everything_f69_lists_at_once(view, app):
    app.engine.job = _job("job_live", path=RequestPath.REST, label="Claude Desktop")
    view.refresh()

    assert view.job_path.text() == "REST"
    assert "Claude Desktop" in view.job_owner.text()
    assert view.job_stage.text() == "Generating"
    assert SUPERTONIC_3_ID in view.job_model.text()
    assert "CPU 20%" in view.job_budget.text()
    assert view.progress.value() == 40
    assert SUPERTONIC_3_ID in view.model_name.text()
    assert "CPUExecutionProvider" in view.model_providers.text()
    assert "CPU 41%" in view.usage_value.text()
    assert "8765" in view.rest_state.text()
    assert view.mcp_state.text() in ("off", "꺼짐")


def test_it_names_the_client_that_is_holding_the_slot(view, app):
    """A.3 and F-50: the owner cannot preempt, so the screen has to say who
    has the slot and that cancelling is the way to get it back."""
    app.engine.job = _job("job_live", path=RequestPath.REST, label="Claude Desktop")
    view.refresh()

    assert view.contention.isVisibleTo(view)
    text = view.contention.text()
    assert "Claude Desktop" in text
    assert "cancel" in text.lower()


def test_the_owner_s_own_job_gets_no_contention_notice(view, app):
    app.engine.job = _job("job_live", path=RequestPath.GUI)
    view.refresh()

    assert not view.contention.isVisibleTo(view)
    assert view.job_owner.text() == "This computer (you)"


def test_the_owner_can_cancel_a_client_s_job_from_here(view, app):
    app.engine.job = _job("job_live", path=RequestPath.REST, label="Tool")
    view.refresh()
    assert view.cancel_button.isEnabled()

    view.cancel_button.click()

    assert app.engine.cancelled == ["job_live"]


def test_the_job_table_lists_jobs_from_every_owner(view, app):
    """F-50: the owner reviews all jobs, not only their own."""
    _seed_jobs(app)
    view.refresh()

    assert view.jobs.rowCount() == 2
    owners = {view.jobs.item(r, 2).text() for r in range(2)}
    assert owners == {"owner", "Claude Desktop"}


def test_a_job_can_be_cancelled_from_the_table(view, app):
    _seed_jobs(app)
    view.refresh()
    row = next(
        r for r in range(view.jobs.rowCount()) if view.jobs.item(r, 2).text() == "Claude Desktop"
    )
    view.jobs.selectRow(row)

    assert view.cancel_selected.isEnabled()
    view.cancel_selected.click()

    assert app.engine.cancelled == ["job_rest"]


def test_a_control_that_cannot_act_is_disabled(view, app):
    """N-09: an unavailable control looks unavailable."""
    app.engine.job = None
    view.refresh()

    assert not view.cancel_button.isEnabled()
    assert not view.cancel_selected.isEnabled()


def test_a_terminal_job_in_the_table_cannot_be_cancelled(view, app):
    app.store.create_job(_job("job_done", state=JobState.ACCEPTED))
    app.store.update_job_state("job_done", JobState.CANCELING, force=True)
    app.store.update_job_state("job_done", JobState.CANCELED)
    view.refresh()
    view.jobs.selectRow(0)

    assert not view.cancel_selected.isEnabled()


def test_a_refused_cancellation_is_reported_rather_than_swallowed(view, app):
    app.engine.job = _job("job_live")
    app.engine.raises = EchoActError(Code.NOT_FOUND, "No such job.")
    view.refresh()

    view.cancel_button.click()

    assert view.message.isVisibleTo(view)
    assert Code.NOT_FOUND.value in view.message.text()


# ------------------------------------------------------- F-22, N-03, N-21 ---


def test_usage_keeps_updating_while_idle_with_a_model_retained(view, app):
    """F-22: the poll does not stop because no job is running."""
    app.engine.job = None
    view._tick()
    first = view.usage_value.text()
    assert "CPU 41%" in first

    app.supervisor.set_usage(7.0, 900_000_000)
    view._tick()

    assert view.usage_value.text() != first
    assert "CPU 7%" in view.usage_value.text()
    assert app.supervisor.samples >= 2


def test_the_memory_figure_says_where_it_came_from(view, app):
    """N-03: the figure on screen and the figure limits are tested against
    may differ, so the screen names the source instead of implying one."""
    view.refresh()

    assert "job object" in view.usage_source.text()
    note = view.usage_note.text()
    assert "commit charge" in note
    assert "resident memory" in note
    # N-21: measured for the generation job, not for the whole application.
    assert "generation job alone" in note
    # The commit peak is reported beside the basis it belongs to, never as
    # the peak of the resident figure shown above it.
    assert memory_size(1_400_000_000) in note
    assert memory_size(1_200_000_000) in view.usage_value.text()


def test_the_three_budgets_are_shown_separately(view, app):
    """F-78's configured value, the worker's, and the running job's."""
    app.engine.job = _job("job_live")
    view.refresh()

    assert "CPU 20%" in view.budget_configured.text()
    assert "CPU 20%" in view.budget_worker.text()
    assert "CPU 20%" in view.budget_job.text()
    assert "enforced" in view.budget_enforcement.text()
    assert "monitored" in view.budget_enforcement.text()
    assert "windows job object" in view.budget_enforcement.text()


def test_an_idle_engine_reports_no_running_job_budget(view, app):
    app.engine.job = None
    view.refresh()
    assert view.budget_job.text() == "—"


# ------------------------------------------------------------ F-52, F-69 ---


def test_stopping_the_service_blocks_cancels_and_then_shuts_down(view, app):
    """F-52's order: no new requests, then that integration's job, then off."""
    app.engine.job = _job("job_rest", path=RequestPath.REST, label="Tool")
    view.refresh()

    view.rest_button.click()

    assert app.events == [
        "settings:rest_enabled=False",
        "drain",
        "cancel:job_rest",
        "stop_service",
    ]
    assert app.engine.cancelled == ["job_rest"]
    assert app.settings.rest_enabled is False
    assert not app.service_running


def test_stopping_the_service_never_cancels_before_the_drain(view, app):
    """F-52's ordering is the service's, and the screen must not pre-empt it.

    ``rest_enabled`` is a startup setting that no request path reads, so
    writing it blocks nothing.  A cancellation issued from here before
    ``stop_service`` therefore frees the single slot while the listener is
    still admitting requests -- exactly the window the ordering exists to
    close -- and costs the owner a second five-second wait when the service
    cancels again on its way down.
    """
    app.engine.job = _job("job_rest", path=RequestPath.REST, label="Tool")
    view.refresh()

    view.rest_button.click()

    # One cancellation, and it happened inside the drain rather than before it.
    assert app.engine.cancelled == ["job_rest"]
    assert app.events.index("drain") < app.events.index("cancel:job_rest")
    assert app.events.index("cancel:job_rest") < app.events.index("stop_service")


def test_stopping_the_service_leaves_a_gui_job_running(view, app):
    """F-79 and F-52: turning an integration off is not a stop-everything."""
    app.engine.job = _job("job_gui", path=RequestPath.GUI)
    view.refresh()

    view.rest_button.click()

    assert app.engine.cancelled == []
    assert app.engine.job is not None


def test_the_service_can_be_started_again_and_a_bind_failure_is_a_notice(view, app):
    app.update_settings(rest_enabled=False)
    app.service_running = False
    app.events.clear()
    app.start_problem = Problem(Code.SERVICE_PORT_UNAVAILABLE, "Port 8765 is in use.")
    view.refresh()

    view.rest_button.click()

    assert app.events == ["settings:rest_enabled=True", "start_service"]
    assert Code.SERVICE_PORT_UNAVAILABLE.value in view.message.text()
    assert view.message.isVisibleTo(view)


def test_a_bind_failure_offers_a_retry_not_a_second_off_switch(view, app):
    """F-79 and N-09: enabled, nothing listening, and the button says start.

    This is the state F-79 names -- the port was taken -- and the only
    action worth offering is another attempt at it.  Labelling the control
    "Stop the service" made the owner turn the integration off first and
    click twice to get to the retry they asked for.
    """
    app.service_running = False  # the bind failed; rest_enabled is still on
    app.events.clear()
    view.refresh()

    assert app.settings.rest_enabled is True
    assert view.rest_state.text() == "Integrations unavailable"
    assert view.rest_button.text() == "Start the service"

    app.start_problem = Problem(Code.SERVICE_PORT_UNAVAILABLE, "Port 8765 is in use.")
    view.rest_button.click()

    assert app.events == ["settings:rest_enabled=True", "start_service"]
    assert app.settings.rest_enabled is True
    assert "stop_service" not in app.events
    assert Code.SERVICE_PORT_UNAVAILABLE.value in view.message.text()
    assert view.message.isVisibleTo(view)


def test_a_retried_bind_that_succeeds_leaves_the_service_listening(view, app):
    app.service_running = False
    app.events.clear()
    view.refresh()

    view.rest_button.click()

    assert app.service_running
    assert "8765" in view.rest_state.text()
    assert view.rest_button.text() == "Stop the service"


def test_disabling_mcp_cancels_an_mcp_job_only(view, app):
    app.update_settings(mcp_enabled=True)
    app.engine.job = _job("job_mcp", path=RequestPath.MCP, label="An MCP client")
    app.events.clear()
    view.refresh()

    view.mcp_button.click()

    assert app.settings.mcp_enabled is False
    assert app.engine.cancelled == ["job_mcp"]


# ------------------------------------------------------------------ F-72 ---


class _StubDialog:
    """Stands in for the review dialog so no modal loop starts in a test."""

    shown: list[str] = []

    def __init__(self, text: str, palette: Any, parent: Any = None) -> None:
        _StubDialog.shown.append(text)

    def exec(self) -> int:
        return 0


def test_the_export_is_reviewed_before_anything_is_written(view, app, tmp_path, monkeypatch):
    """F-72: the export happens after review, so the button writes nothing."""
    from echoact.ui import status_view

    _StubDialog.shown = []
    monkeypatch.setattr(status_view, "DiagnosticsDialog", _StubDialog)
    before = set(tmp_path.rglob("*.txt"))

    view.export_button.click()

    assert len(_StubDialog.shown) == 1
    assert "EchoAct diagnostic report" in _StubDialog.shown[0]
    assert view.last_export == _StubDialog.shown[0]
    assert set(tmp_path.rglob("*.txt")) == before


def test_the_dialog_saves_exactly_the_text_it_showed(qt_app, tmp_path):
    target = tmp_path / "export.txt"
    dialog = DiagnosticsDialog(
        "line one\nline two\n", theme.LIGHT, choose_path=lambda: str(target)
    )
    dialog.grab()

    dialog.save()

    assert target.read_text(encoding="utf-8") == "line one\nline two\n"
    assert dialog.view.toPlainText().startswith("line one")
    assert "export.txt" in dialog.status.text()
    dialog.deleteLater()


def test_a_cancelled_file_dialog_writes_nothing(qt_app, tmp_path):
    dialog = DiagnosticsDialog("text", theme.LIGHT, choose_path=lambda: None)
    dialog.save()
    assert list(tmp_path.glob("*.txt")) == []
    dialog.deleteLater()


def test_a_failed_save_is_reported_in_the_dialog(qt_app, tmp_path):
    directory = tmp_path / "dir"
    directory.mkdir()
    dialog = DiagnosticsDialog("text", theme.LIGHT, choose_path=lambda: str(directory))

    dialog.save()

    reported = dialog.status.text()
    assert Code.FILE_PERMISSION.value in reported or Code.INTERNAL.value in reported
    assert not (directory / "x").exists()
    dialog.deleteLater()


# ------------------------------------------------------------ threading ----


def _wait_until(qt_app: QApplication, condition: Any, timeout: float = 5.0) -> None:
    """Pump the event loop until ``condition`` holds.

    A reply from a worker thread is a queued signal, so it arrives only when
    the GUI thread processes events; nothing in these tests runs an event
    loop of its own.
    """
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    qt_app.processEvents()
    assert condition(), "the background work did not finish in time"


def test_the_default_runner_does_not_use_the_calling_thread():
    """Rule 7: cancelling waits up to N-22's five seconds, and the window
    must stay alive for all of them."""
    done = threading.Event()
    seen: dict[str, int] = {}

    def work() -> None:
        seen["thread"] = threading.get_ident()
        done.set()

    _default_run_async(work)

    assert done.wait(5.0)
    assert seen["thread"] != threading.get_ident()


def test_the_default_runner_reuses_one_thread_rather_than_taking_a_new_one():
    """Off-thread is not enough on its own.

    ``Store`` keeps one SQLite connection per thread and releases none of
    them before exit, so a thread per call is a connection per call.  One
    worker, however many times the screen polls.
    """
    seen: list[int] = []

    for _ in range(20):
        _default_run_async(lambda: seen.append(threading.get_ident()))
    # Not a sentinel task: waiting on one would assume the serialisation
    # this test is here to establish.
    deadline = time.monotonic() + 5.0
    while len(seen) < 20 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert len(seen) == 20
    assert set(seen) == {seen[0]}
    assert seen[0] != threading.get_ident()


def test_polling_the_job_list_does_not_leak_a_connection_per_poll(qt_app, app):
    """Rule 7 / N-21 against the production runner, not the inline one.

    ``Store._conn`` opens a connection per thread and appends it to a list
    that only ``Store.close()`` empties at exit, so a fresh thread per poll
    leaks a connection, a file handle and a WAL reader every five seconds:
    some 5,760 of them over the eight-hour soak A-20 asks for.
    """
    _seed_jobs(app)
    view = StatusView(app, theme.LIGHT)  # no run_async: the real runner
    view._timer.stop()  # this test drives the poll itself
    replies: list[object] = []
    # Counting the replies rather than waiting on the queue keeps the
    # measurement honest whatever the runner does with its threads.
    view._signals.jobs_ready.connect(replies.append)
    try:
        _wait_until(qt_app, lambda: len(replies) >= 1)  # the constructor's own read
        before = len(app.store._connections)

        for _ in range(10):
            view._load_jobs()
        _wait_until(qt_app, lambda: len(replies) >= 11)

        assert len(app.store._connections) == before
    finally:
        view.deleteLater()


# ------------------------------------------------------------------ N-30 ----


def test_every_action_on_the_screen_has_an_accessible_name(view):
    for button in (
        view.refresh_button,
        view.export_button,
        view.cancel_button,
        view.cancel_selected,
        view.rest_button,
        view.mcp_button,
    ):
        assert button.accessibleName()
        assert button.text()
    assert view.jobs.accessibleName()
    assert view.progress.accessibleName()


def test_the_screen_lays_out_at_a_narrow_width(view):
    """N-30: at 1280x720 with 200% scaling this panel is half a screen wide."""
    view.resize(560, 620)
    view.grab()
    assert view.jobs.width() > 0


def test_the_screen_survives_a_supervisor_that_cannot_answer(view, app):
    """A status screen that dies when the engine does is worse than useless."""

    def boom(*_a, **_k):
        raise RuntimeError("worker is gone")

    app.supervisor.sample_usage = boom
    app.supervisor.loaded_model = None

    view.refresh()

    assert view.usage_value.text() == "Not measured yet."
    assert view.model_name.text() == "No model is loaded."
    assert view.rest_state.text()
