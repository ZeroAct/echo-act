"""The main screen.

N-09 asks that input, voice settings, resource settings, generation, and
playback all be reachable from here, so this window is wide rather than
deep: the reading surface takes the space, everything else sits around it,
and the other screens are one button away.

The window owns every decision.  The panels below it emit intent and show
state; they never call the engine.  That is what makes "unavailable
controls are disabled" a single method rather than a rule each widget has
to remember.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..app import Application
from ..audio import wav
from ..audio.player import PlayerState, Timeline
from ..domain import JobState, RequestPath, RetentionMode, VoiceSettings
from ..errors import Code, EchoActError
from ..jobs.engine import Event
from ..jobs.request import JobRequest, estimate
from ..models.catalog import MANIFEST
from ..policy import MAX_INPUT_CODEPOINTS
from ..text.loader import load_file
from ..util import ids
from ..util.logging import get_logger
from . import icons
from .bridge import EngineBridge
from .controls import TransportBar, VoicePanel, label, tool_button
from .i18n import add_korean, approximate_duration, count, tr
from .licence import LicenceDialog
from .notifications import (
    Level,
    Notice,
    NotificationCentre,
    completion_notice,
    failure_notice,
)
from .reading import ReadingSurface
from .theme import METRICS, Mode, Palette
from .theme import apply as apply_theme

log = get_logger("ui.window")

#: How often the reading position is repainted.  This is a repaint cadence,
#: not a clock: the position itself comes from the audio callback's frame
#: counter (N-12, A.2), and 40 ms leaves the 300 ms budget almost entirely
#: to the device.
TICK_MS = 40

add_korean(
    {
        "{n} / {max} characters": "{n} / {max}자",
        "Nothing to read yet": "읽을 내용이 없습니다",
        "{n} segments, {duration}": "{n}개 문장, {duration}",
        "Generating {done} of {total}": "{total}개 중 {done}개 생성 중",
        "Ready to read": "읽을 준비가 되었습니다",
        "Reading": "읽는 중",
        "Finished": "끝났습니다",
        "Generation canceled": "생성이 취소되었습니다",
        "Highlighting unavailable while the text differs from the audio":
            "본문이 오디오와 달라 강조 표시를 사용할 수 없습니다",
        "Following paused": "따라가기 일시 중지됨",
        "Open a text file": "텍스트 파일 열기",
        "Activity": "활동",
        "Storage": "저장 공간",
        "{n} items cleaned up": "{n}개 항목을 정리했습니다",
        "Some items could not be deleted": "일부 항목을 삭제하지 못했습니다",
        "EchoAct cannot check for a newer version in this build.":
            "이 빌드에서는 새 버전을 확인할 수 없습니다.",
        "Text files (*.txt *.md);;All files (*)": "텍스트 파일 (*.txt *.md);;모든 파일 (*)",
        "Save audio": "오디오 저장",
        "WAV audio (*.wav)": "WAV 오디오 (*.wav)",
        "Replace the text that is already here?": "이미 입력된 본문을 바꿀까요?",
        "The text you have now has not been saved.": "지금 입력한 본문은 저장되지 않았습니다.",
        "Replace": "바꾸기",
        "Keep": "유지",
        "Too much text to paste": "붙여넣기에 본문이 너무 많습니다",
        "Pasting {n} characters would pass the {max} character limit; there is room for {room}.":
            "{n}자를 붙여넣으면 {max}자 제한을 넘습니다. {room}자만 넣을 수 있습니다.",
        "A job is still running.": "작업이 아직 실행 중입니다.",
        "Leave anyway": "그래도 종료",
        "Stay": "머무르기",
        "partial": "일부",
        "This file covers {percent}% of the text.": "이 파일은 본문의 {percent}%를 담고 있습니다.",
        "Line endings were normalised.": "줄바꿈 문자를 정규화했습니다.",
    }
)


class MainWindow(QMainWindow):
    """One window, one job at a time, one reading position."""

    theme_changed = Signal(object)

    def __init__(self, app: Application, mode: Mode = Mode.SYSTEM) -> None:
        super().__init__()
        self.app = app
        self.palette_tokens: Palette = apply_theme(
            __import__("PySide6.QtWidgets", fromlist=["QApplication"]).QApplication.instance(),
            mode,
        )
        self.setWindowTitle("EchoAct")
        self.resize(1180, 780)
        self.setMinimumSize(900, 560)

        self._job_id: str | None = None
        self._timeline: Timeline | None = None
        self._snapshot: str = ""
        self._started_playback = False
        self._entry = MANIFEST.get(app.settings.voice.model_id)

        self._build()
        self._wire()
        self._apply_settings()
        self._refresh_estimate()
        self._refresh_resources()
        self._update_enabled()
        self._set_status(tr("Ready to read"))

        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self._report_startup()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        m = METRICS
        p = self.palette_tokens
        root = QWidget()
        root.setObjectName("Root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(m.pad_wide, m.pad_wide, m.pad_wide, m.pad)
        outer.setSpacing(m.gap_wide)

        # -- header ----------------------------------------------------
        head = QHBoxLayout()
        head.setSpacing(m.gap)
        head.addWidget(label("EchoAct", "title"))
        self.model_chip = label("", "muted")
        head.addWidget(self.model_chip)
        head.addStretch(1)
        self.open_button = QPushButton("  " + tr("Open file"))
        self.open_button.setProperty("variant", "quiet")
        self.open_button.setIcon(icons.icon("folder", p.text_secondary, p.text_muted))
        self.open_button.setIconSize(icons.icon_size(16))
        self.save_button = QPushButton("  " + tr("Save audio"))
        self.save_button.setProperty("variant", "quiet")
        self.save_button.setIcon(icons.icon("export", p.text_secondary, p.text_muted))
        self.save_button.setIconSize(icons.icon_size(16))
        head.addWidget(self.open_button)
        head.addWidget(self.save_button)

        head.addSpacing(METRICS.gap)
        self.nav: dict[str, QPushButton] = {}
        for key, name, glyph in (
            ("library", tr("Library"), "library"),
            ("models", tr("Models"), "cube"),
            ("status", tr("Activity"), "plug"),
            ("settings", tr("Settings"), "settings"),
        ):
            b = QPushButton("  " + name)
            b.setProperty("variant", "quiet")
            b.setIcon(icons.icon(glyph, p.text_secondary, p.text_muted))
            b.setIconSize(icons.icon_size(16))
            b.setAccessibleName(name)
            head.addWidget(b)
            self.nav[key] = b
        outer.addLayout(head)

        # -- notice banner (F-25, F-70) --------------------------------
        self.notice = QFrame()
        self.notice.setObjectName("Panel")
        notice_row = QHBoxLayout(self.notice)
        notice_row.setContentsMargins(m.pad, m.gap, m.gap, m.gap)
        notice_row.setSpacing(m.gap)
        self.notice_icon = QLabel()
        self.notice_text = label("", "secondary")
        self.notice_text.setWordWrap(True)
        notice_row.addWidget(self.notice_icon)
        notice_row.addWidget(self.notice_text, 1)
        self.notice_close = tool_button("close", p, size=14)
        notice_row.addWidget(self.notice_close)
        self.notice.hide()
        outer.addWidget(self.notice)

        # -- body ------------------------------------------------------
        body = QHBoxLayout()
        body.setSpacing(m.gap_wide)

        column = QVBoxLayout()
        column.setSpacing(m.gap)
        self.reading = ReadingSurface(p)
        column.addWidget(self.reading, 1)

        meta = QHBoxLayout()
        self.counter = label("", "muted")
        self.estimate_label = label("", "muted")
        self.estimate_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        meta.addWidget(self.counter)
        meta.addStretch(1)
        self.follow_button = QPushButton("  " + tr("Return to the reading position"))
        self.follow_button.setProperty("variant", "quiet")
        self.follow_button.setIcon(icons.icon("locate", p.accent))
        self.follow_button.setIconSize(icons.icon_size(14))
        self.follow_button.hide()
        meta.addWidget(self.follow_button)
        meta.addWidget(self.estimate_label)
        column.addLayout(meta)

        self.transport = TransportBar(p)
        column.addWidget(self.transport)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        column.addWidget(self.progress)

        self.status = label("", "muted")
        column.addWidget(self.status)
        body.addLayout(column, 1)

        self.voice_panel = VoicePanel(p, self._entry)
        body.addWidget(self.voice_panel, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(body, 1)

        self.setCentralWidget(root)
        self._build_status_bar()
        self._build_menu()

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self.service_label = label("", "muted")
        self.usage_label = label("", "muted")
        bar.addWidget(self.service_label)
        bar.addPermanentWidget(self.usage_label)
        self._refresh_service_label()

    def _build_menu(self) -> None:
        """Menu entries exist so every action has a keyboard route (N-30),
        not because the window needs a menu."""
        actions = (
            (tr("Open file"), QKeySequence.StandardKey.Open, self._open_file),
            (tr("Save audio"), QKeySequence.StandardKey.Save, self._save_audio),
            (tr("Read aloud"), QKeySequence("Ctrl+Return"), self._primary_action),
            (tr("Play"), QKeySequence("Space"), None),
            (tr("Stop"), QKeySequence("Ctrl+."), self._stop),
            (tr("Return to the reading position"), QKeySequence("Ctrl+J"), self._return_to_position),
        )
        for name, shortcut, slot in actions:
            if slot is None:
                continue
            action = QAction(name, self)
            action.setShortcut(shortcut)
            action.triggered.connect(slot)
            self.addAction(action)

    def _wire(self) -> None:
        self.notifications = NotificationCentre(
            os_notifications=self.app.settings.os_notifications
        )
        self.bridge = EngineBridge(self.app.engine, self)
        self.bridge.accepted.connect(self._on_accepted)
        self.bridge.state_changed.connect(self._on_state)
        self.bridge.segment_ready.connect(self._on_segment)
        self.bridge.finished.connect(self._on_finished)

        self.reading.length_changed.connect(self._on_length)
        self.reading.availability_changed.connect(self._on_availability)
        self.reading.following_changed.connect(self._on_following)
        self.reading.paste_refused.connect(self._on_paste_refused)

        self.transport.read_requested.connect(self._primary_action)
        self.transport.pause_requested.connect(self._toggle_play)
        self.transport.stop_requested.connect(self._stop)
        self.transport.seek_requested.connect(self._seek)

        self.voice_panel.changed.connect(self._on_voice_changed)
        self.voice_panel.autoplay_toggled.connect(
            lambda on: self.app.update_settings(autoplay=on)
        )
        self.voice_panel.follow_toggled.connect(self._on_follow_toggled)

        self.nav["library"].clicked.connect(self._show_library)
        self.nav["models"].clicked.connect(self._show_models)
        self.nav["settings"].clicked.connect(self._show_settings)
        self.nav["status"].clicked.connect(self._show_status)

        self.open_button.clicked.connect(self._open_file)
        self.save_button.clicked.connect(self._save_audio)
        self.follow_button.clicked.connect(self._return_to_position)
        self.notice_close.clicked.connect(self.notice.hide)

    def _apply_settings(self) -> None:
        s = self.app.settings
        self.voice_panel.apply(s.voice, autoplay=s.autoplay, follow=s.follow)
        self.reading.set_follow(s.follow)
        self.app.player.set_volume(s.volume)
        self.app.player.set_muted(s.muted)
        state = self.app.registry.status(s.voice.model_id)
        ready = getattr(state, "state", None)
        self.model_chip.setText(
            f"{self._entry.display_name} · {self._entry.sample_rate // 1000}.{(self._entry.sample_rate // 100) % 10} kHz"
            + ("" if str(ready) == "ready" else " · " + tr("not prepared"))
        )

    # ------------------------------------------------------------------
    # Starting and stopping a job
    # ------------------------------------------------------------------

    def _primary_action(self) -> None:
        """One button: start, or cancel what is running.

        A.3 makes cancelling the owner's way to reclaim the single slot,
        and F-69 asks for it to be reachable in one place.
        """
        if self.app.engine.busy:
            self._cancel()
        else:
            self._start()

    def _start(self) -> None:
        if not self._licence_accepted():
            return
        text = self.reading.source_text()
        settings = self.voice_panel.settings()
        request = JobRequest(
            text=text,
            settings=settings,
            request_path=RequestPath.GUI,
            owner_client_id="owner",
            # A fresh key per press. F-49 exists to stop a retry becoming a
            # second job, and a double-click is a retry.
            idempotency_key=ids.request_id(),
            retention=(
                RetentionMode.RETAINED if self.app.settings.retain_history else RetentionMode.ONE_OFF
            ),
        )
        try:
            job, created = self.app.engine.submit(request)
        except EchoActError as exc:
            self._show_problem(exc)
            return

        self._job_id = job.job_id
        self._snapshot = text
        self._started_playback = False
        self._timeline = Timeline(sample_rate=self._entry.sample_rate)
        self.app.player.load(self._timeline)
        self.reading.attach_job(text, self.app.store.list_segments(job.job_id))
        self.progress.setValue(0)
        self._update_enabled()
        if not created:
            self._set_status(tr("Ready to read"))

    def _licence_accepted(self) -> bool:
        """N-11 and F-80: the terms are presented before first preparation.

        Asked here rather than left to the engine's refusal, because a code
        in a notice bar is not "presented as terms the user accepts".
        Declining is not an error: the user said not now, and the window
        simply does not start a job.
        """
        pending = self.app.licence_pending()
        if pending is None:
            return True
        dialog = LicenceDialog(MANIFEST.get(pending), self.palette_tokens, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.app.accept_licence(pending)
        return True

    def _cancel(self) -> None:
        if not self._job_id:
            return
        try:
            self.app.engine.cancel(self._job_id)
        except EchoActError as exc:
            self._show_problem(exc)

    # ------------------------------------------------------------------
    # Engine events, already on the GUI thread
    # ------------------------------------------------------------------

    def _on_accepted(self, event: Event) -> None:
        self._set_status(tr("Preparing the model"))
        self._update_enabled()

    def _on_state(self, event: Event) -> None:
        if event.job_id != self._job_id:
            return
        text = {
            JobState.PREPARING_MODEL: tr("Preparing the model"),
            JobState.GENERATING: tr("Generating"),
            JobState.COMPLETE: tr("Finished"),
            JobState.CANCELING: tr("Cancel generation"),
            JobState.CANCELED: tr("Generation canceled"),
            JobState.FAILED: tr("Failed"),
        }.get(event.state or JobState.ACCEPTED, "")
        self._set_status(text)
        self._update_enabled()

    def _on_segment(self, event: Event) -> None:
        """A segment is ready: extend the timeline and start if asked to.

        F-12 starts playback at the first ready segment; F-83 lets the user
        turn that off, and F-51 forbids it for a job an integration
        created -- which is why this checks the job is ours as well as the
        setting.
        """
        if event.job_id != self._job_id or self._timeline is None:
            return
        detail = event.detail
        start_ms, end_ms = int(detail.get("start_ms", 0)), int(detail.get("end_ms", 0))
        frames = int(detail.get("frame_count", 0))
        sr = self._timeline.sample_rate
        span_frames = wav.frames_for_ms(end_ms, sr) - wav.frames_for_ms(start_ms, sr)
        self._timeline.append(
            event.segment_index or 0,
            str(detail.get("audio_path", "")),
            frames,
            max(0, span_frames - frames),
        )
        self.app.player.timeline_grew()
        self.reading.update_segments(self.app.store.list_segments(event.job_id))
        self.transport.set_playable(self._timeline.duration_ms)

        if event.total:
            self.progress.setValue(int(100 * event.generated / event.total))
            self._set_status(
                tr("Generating {done} of {total}").format(done=event.generated, total=event.total)
            )
        if not self._started_playback and self.app.settings.autoplay:
            self._started_playback = True
            self._play()

    def _on_finished(self, event: Event) -> None:
        if event.job_id != self._job_id:
            return
        if self._timeline is not None:
            self._timeline.complete = True
        self.progress.setValue(100 if event.state is JobState.COMPLETE else self.progress.value())
        self._update_enabled()
        if event.state is JobState.FAILED and event.error_code:
            self._post(failure_notice(tr("Generation failed"), event.error_code, job_id=event.job_id))
        elif event.state is JobState.COMPLETE:
            # F-70. A job the user started needs no explanation; one an
            # integration created needs two things said, because the user
            # did not ask for it and a one-off result goes within the hour.
            job = self.app.store.get_job(event.job_id, include_segments=False)
            self._post(
                completion_notice(
                    job_id=event.job_id,
                    client_label=event.client_label,
                    expires_at=job.result.expires_at if job.result else None,
                )
            )

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _play(self) -> None:
        try:
            self.app.player.play()
        except EchoActError as exc:
            self._show_problem(exc)
            return
        self.reading.set_playing()
        self.transport.set_playback_state("", playing=True)

    def _toggle_play(self) -> None:
        player = self.app.player
        if player.state is PlayerState.PLAYING:
            player.pause()
            self.reading.set_paused()
            self.transport.set_playback_state("", playing=False)
        else:
            self._play()

    def _stop(self) -> None:
        """F-13: stopping playback does not stop generation."""
        self.app.player.stop()
        self.reading.clear_highlight()
        self.transport.set_playback_state("", playing=False)
        self.transport.set_position(0)

    def _seek(self, ms: int) -> None:
        if not self.app.player.seek_ms(ms):
            # F-14: ungenerated audio cannot be sought to. Snap back rather
            # than pretend the seek happened.
            self.transport.set_position(self.app.player.position_ms())

    def _on_tick(self) -> None:
        player = self.app.player
        if self._timeline is None:
            return
        position = player.position_ms()
        waiting = player.state is PlayerState.WAITING
        if not self.transport.scrubbing:
            self.transport.set_position(position)
        if player.state in (PlayerState.PLAYING, PlayerState.WAITING):
            self.reading.set_playback_ms(position, waiting=waiting)
        if player.state is PlayerState.ENDED:
            self.reading.clear_highlight()
            self.transport.set_playback_state("", playing=False)
        self._refresh_usage()

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _on_length(self, n: int) -> None:
        self.counter.setText(
            tr("{n} / {max} characters").format(n=count(n), max=count(MAX_INPUT_CODEPOINTS))
        )
        self._refresh_estimate()
        self._update_enabled()

    def _refresh_estimate(self) -> None:
        """F-88's estimate, shown to the person as well as to a caller.

        Cheap enough to run on every keystroke -- it is segmentation and
        arithmetic, and it explicitly does not touch the engine.
        """
        text = self.reading.source_text()
        if not text.strip():
            self.estimate_label.setText(tr("Nothing to read yet"))
            return
        est = estimate(
            text,
            self.voice_panel.settings(),
            MANIFEST,
            slot_free=not self.app.engine.busy,
            model_ready=True,
        )
        if not est.valid:
            self.estimate_label.setText(est.problems[0]["message"] if est.problems else "")
            return
        self.estimate_label.setText(
            tr("{n} segments, {duration}").format(
                n=est.segment_count, duration=approximate_duration(est.audio_ms)
            )
        )

    def _on_voice_changed(self, settings: VoiceSettings) -> None:
        self.app.update_settings(voice=settings)
        self._refresh_estimate()
        self._refresh_resources()

    def _on_follow_toggled(self, on: bool) -> None:
        self.app.update_settings(follow=on)
        self.reading.set_follow(on)

    def _on_availability(self, available: bool) -> None:
        if available:
            self.notice.hide()
        elif self._job_id:
            self._show_notice(
                tr("Highlighting unavailable"),
                tr("Highlighting unavailable while the text differs from the audio"),
                kind="info",
            )

    def _on_following(self, following: bool) -> None:
        self.follow_button.setVisible(self.reading.following_suspended)

    def _return_to_position(self) -> None:
        self.reading.return_to_position()
        self.follow_button.hide()

    def _on_paste_refused(self, attempted: int, room: int) -> None:
        QMessageBox.information(
            self,
            tr("Too much text to paste"),
            tr(
                "Pasting {n} characters would pass the {max} character limit; "
                "there is room for {room}."
            ).format(n=count(attempted), max=count(MAX_INPUT_CODEPOINTS), room=count(room)),
        )

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def _open_file(self) -> None:
        # F-36: replacing unsaved input needs confirmation, and a refusal
        # must leave what is there untouched.
        if self.reading.source_text().strip() and not self._confirm_replace():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Open a text file"), "", tr("Text files (*.txt *.md);;All files (*)")
        )
        if not path:
            return
        try:
            loaded = load_file(path)
        except EchoActError as exc:
            # F-32: on any failure the existing input is left as it was.
            self._show_problem(exc)
            return
        self.reading.set_text(loaded.text)
        if loaded.line_endings_normalised:
            # The one edit the loader makes, said out loud: a user counting
            # characters would otherwise be counting something else.
            self._show_notice(loaded.source_name, tr("Line endings were normalised."), kind="info")

    def _save_audio(self) -> None:
        """F-16, including the partial case.

        A job that has not finished can still be saved; the file is named
        and labelled partial and the coverage is reported, so it is never
        mistaken for the whole document.
        """
        if not self._job_id:
            return
        segments = self.app.store.list_segments(self._job_id)
        ready = [s for s in segments if s.ready]
        if not ready:
            return
        complete = len(ready) == len(segments)
        suggested = "reading.wav" if complete else "reading-partial.wav"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Save audio"), suggested, tr("WAV audio (*.wav)")
        )
        if not path:
            return
        try:
            report = wav.export_partial(
                segments,
                Path(path),
                sample_rate=self._entry.sample_rate,
                total_codepoints=len(self._snapshot),
            )
        except EchoActError as exc:
            self._show_problem(exc)
            return
        if not report.complete:
            self._show_notice(
                tr("partial"),
                tr("This file covers {percent}% of the text.").format(
                    percent=int(report.coverage * 100)
                ),
                kind="info",
            )

    # ------------------------------------------------------------------
    # The other screens
    # ------------------------------------------------------------------

    def _open_screen(self, title: str, widget: QWidget, *, width: int, height: int) -> QDialog:
        """Show a screen in its own window.

        A separate window rather than a stacked page, so the reading
        surface and the job it is following are never replaced by
        something else: F-29 compares the live input against the job
        snapshot continuously, and a screen that swapped the input out
        would drop the highlight every time the user opened the library.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        dialog.resize(width, height)
        dialog.show()
        return dialog

    def _show_library(self) -> None:
        from .library import LibraryScreen

        screen = LibraryScreen(self.app.store, self.palette_tokens, manifest=MANIFEST)
        if hasattr(screen, "open_document"):
            screen.open_document.connect(self._open_document)
        if hasattr(screen, "problem"):
            screen.problem.connect(self._show_problem)
        self._library = self._open_screen(tr("Library"), screen, width=1000, height=680)

    def _show_models(self) -> None:
        from ..config.budget import resolve_budget_from_system
        from .models_view import ModelsView

        try:
            budget = resolve_budget_from_system(self.app.settings)
        except EchoActError as exc:
            self._show_problem(exc)
            return
        screen = ModelsView(self.palette_tokens, self.app.registry, budget)
        if hasattr(screen, "problem"):
            screen.problem.connect(self._show_problem)
        self._models = self._open_screen(tr("Models"), screen, width=780, height=680)

    def _show_settings(self) -> None:
        from .settings_view import SettingsView

        screen = SettingsView(self.palette_tokens, self.app.settings)
        self._connect_settings(screen)
        self._refresh_settings_screen(screen)
        # Wide enough that the section text and the controls beside it do
        # not need the horizontal scrollbar. N-30 allows scrolling as the
        # fallback; needing it at the default size is still a bad default.
        self._settings_screen = self._open_screen(
            tr("Settings"), screen, width=900, height=780
        )

    def _refresh_settings_screen(self, screen: QWidget) -> None:
        """Fill in what the view cannot measure for itself.

        F-78's applied budget comes from the *running job*, not from a
        fresh resolution: resolving again would show the same number
        twice and quietly turn the distinction the requirement draws
        into two views of one value.
        """
        job = self.app.engine.current()
        screen.set_resource_state(
            job.budget if job is not None else None,
            job_running=job is not None,
        )
        screen.set_credentials(self.app.credentials.list())
        self._refresh_storage(screen)

    def _refresh_storage(self, screen: QWidget) -> None:
        """F-73's five figures.

        Off the main thread: it walks the model cache, which is 385 MB of
        files on an ordinary install and can be far more.
        """
        from .settings_view import storage_snapshot

        def measure() -> None:
            try:
                sizes = storage_snapshot(self.app.store, self.app.registry)
            except (EchoActError, OSError):
                return
            QTimer.singleShot(0, lambda: screen.set_storage(sizes))

        threading.Thread(target=measure, name="echoact-storage", daemon=True).start()

    def _connect_settings_actions(self, screen: QWidget) -> None:
        """The buttons that do work rather than change a value.

        Each one is the window's to perform: F-76 requires the deletion
        scopes to be offered separately and to report what failed, and
        F-71 requires a revocation to reach that client's running job as
        well as its credential (5.3).
        """
        handlers = {
            "apply_now_requested": lambda: self._apply_budget_now(screen),
            "devices_refresh_requested": lambda: self._refresh_devices(screen),
            "storage_refresh_requested": lambda: self._refresh_storage(screen),
            "cleanup_requested": lambda: self._clean_up(screen),
            "credential_issue_requested": lambda *a: self._issue_credential(screen, *a),
            "credential_reissue_requested": lambda ref: self._reissue_credential(screen, ref),
            "credential_revoke_requested": lambda ref: self._revoke_credential(screen, ref),
            "credential_capabilities_changed": (
                lambda ref, caps: self._set_capabilities(screen, ref, caps)
            ),
            "reset_requested": lambda scope: self._reset(screen, scope),
            "version_check_requested": lambda: self._check_version(screen),
        }
        for name, handler in handlers.items():
            signal = getattr(screen, name, None)
            if signal is not None:
                signal.connect(handler)

    def _apply_budget_now(self, screen: QWidget) -> None:
        """F-78: applying immediately means cancelling the current job,
        and the view has already confirmed that with the user."""
        job = self.app.engine.current()
        if job is not None:
            try:
                self.app.engine.cancel(job.job_id)
            except EchoActError as exc:
                self._show_problem(exc)
                return
        self._refresh_settings_screen(screen)
        self._refresh_resources()

    def _refresh_devices(self, screen: QWidget) -> None:
        """F-68: after a resume or a device change, PortAudio still holds
        the list it built at start-up until it is reinitialised."""
        from ..audio import devices

        devices.refresh()
        if hasattr(screen, "reload_devices"):
            screen.reload_devices()

    def _clean_up(self, screen: QWidget) -> None:
        """F-73's cleanup: expired data only, and never anything in use."""
        from ..jobs.engine import clear_temp_tree, expire_one_off_results
        from ..paths import temp_dir
        from ..util.logging import prune_old_logs

        try:
            removed = expire_one_off_results(self.app.store, temp_dir())
        except EchoActError as exc:
            self._show_problem(exc)
            return
        if not self.app.engine.busy:
            # N-28 and F-73: data in use by a running job is excluded, and
            # the scratch tree is exactly what a running job is using.
            removed += clear_temp_tree(temp_dir())
        removed += prune_old_logs(ids.now())
        self._show_notice(tr("Storage"), tr("{n} items cleaned up").format(n=removed))
        self._refresh_storage(screen)

    def _issue_credential(self, screen: QWidget, name: str, capabilities, days: int) -> None:
        """F-71: shown once, and only a verifier is kept."""
        try:
            issued = self.app.credentials.issue(
                name=name, capabilities=set(capabilities), days=int(days)
            )
        except EchoActError as exc:
            self._show_problem(exc)
            return
        self._show_credential_once(issued)
        screen.set_credentials(self.app.credentials.list())

    def _reissue_credential(self, screen: QWidget, ref: str) -> None:
        try:
            issued = self.app.credentials.reissue(ref)
        except EchoActError as exc:
            self._show_problem(exc)
            return
        self._show_credential_once(issued)
        screen.set_credentials(self.app.credentials.list())

    def _revoke_credential(self, screen: QWidget, ref: str) -> None:
        """5.3: revocation cancels that client's in-progress jobs too."""
        try:
            credential = self.app.credentials.get(ref)
            self.app.credentials.revoke(ref)
        except EchoActError as exc:
            self._show_problem(exc)
            return
        self._cancel_jobs_of(credential.client_id)
        screen.set_credentials(self.app.credentials.list())

    def _set_capabilities(self, screen: QWidget, ref: str, capabilities) -> None:
        try:
            change = self.app.credentials.set_capabilities(ref, set(capabilities))
        except EchoActError as exc:
            self._show_problem(exc)
            return
        if change.cancels_jobs:
            # 5.3: narrowing a client's permissions cancels its running job.
            self._cancel_jobs_of(change.client_id)
        screen.set_credentials(self.app.credentials.list())

    def _cancel_jobs_of(self, client_id: str) -> None:
        job = self.app.engine.current()
        if job is not None and job.owner_client_id == client_id:
            try:
                self.app.engine.cancel(job.job_id)
            except EchoActError as exc:
                self._show_problem(exc)

    def _show_credential_once(self, issued) -> None:
        from .credential_dialog import show_credential

        show_credential(self, issued, self.palette_tokens)

    def _reset(self, screen: QWidget, scope: str) -> None:
        """F-76's four scopes, each on its own, each reporting failures."""
        from .settings_view import ResetScope

        failures: list[str] = []
        try:
            if scope == ResetScope.VOICE_AND_DISPLAY:
                self.app.update_settings(
                    voice=self.app.settings_store.current.voice.with_(tempo=1.0),
                    volume=1.0,
                    muted=False,
                )
            elif scope == ResetScope.INTEGRATIONS:
                for credential in list(self.app.credentials.list()):
                    if not credential.is_owner:
                        self.app.credentials.revoke(credential.ref)
                        self._cancel_jobs_of(credential.client_id)
            elif scope == ResetScope.RETAINED_DATA:
                deletion = self.app.store.delete_all_history()
                # N-16 and F-43: a job that is running is not deleted, and
                # the ones that were skipped are reported rather than
                # counted as done.
                failures = list(deletion.blocked_job_ids)
            elif scope == ResetScope.MODEL_CACHE:
                self.app.registry.delete(self.app.settings.voice.model_id)
        except EchoActError as exc:
            self._show_problem(exc)
            return
        if failures:
            self._show_notice(
                tr("Some items could not be deleted"), ", ".join(failures[:5]), kind="error"
            )
        self._refresh_settings_screen(screen)

    def _check_version(self, screen: QWidget) -> None:
        """F-75: queried only at the user's request, and nothing is
        downloaded or installed.  There is no updater in this build, so
        the honest answer is that the check is unavailable rather than a
        silent nothing."""
        screen.set_released_version(
            None, error=tr("EchoAct cannot check for a newer version in this build.")
        )

    def _show_status(self) -> None:
        from .status_view import StatusView

        screen = StatusView(self.app, self.palette_tokens)
        self._status = self._open_screen(tr("Activity"), screen, width=740, height=560)

    def _connect_settings(self, screen: QWidget) -> None:
        """One place where a settings change becomes an application change.

        Each signal carries the new value and nothing else, so the screen
        never holds the Application, and F-78's rule -- a change during a
        job applies to the next job -- is enforced once inside
        Application.update_settings rather than per control.
        """
        pairs = (
            ("cpu_changed", "cpu_percent"),
            ("memory_changed", "memory_bytes"),
            ("volume_changed", "volume"),
            ("muted_changed", "muted"),
            ("autoplay_changed", "autoplay"),
            ("follow_changed", "follow"),
            ("output_device_changed", "output_device"),
            ("rest_enabled_changed", "rest_enabled"),
            ("rest_port_changed", "rest_port"),
            ("mcp_enabled_changed", "mcp_enabled"),
            ("credential_days_changed", "credential_days"),
            ("retention_changed", "retention_bytes"),
            ("os_notifications_changed", "os_notifications"),
            ("autosave_documents_changed", "autosave_documents"),
            ("retain_history_changed", "retain_history"),
        )
        for signal_name, field in pairs:
            signal = getattr(screen, signal_name, None)
            if signal is not None:
                signal.connect(lambda value, f=field: self._apply_setting(f, value))

        language = getattr(screen, "display_language_changed", None)
        if language is not None:
            language.connect(self._apply_display_language)

        self._connect_settings_actions(screen)

    def _apply_setting(self, field: str, value: object) -> None:
        try:
            self.app.update_settings(**{field: value})
        except EchoActError as exc:
            self._show_problem(exc)
            return
        if field in {"volume", "muted"}:
            self.app.player.set_volume(self.app.settings.volume)
            self.app.player.set_muted(self.app.settings.muted)
        elif field == "follow":
            self.reading.set_follow(bool(value))
        elif field in {"cpu_percent", "memory_bytes"}:
            self._refresh_resources()
        elif field in {"rest_enabled", "rest_port", "mcp_enabled"}:
            self._restart_service()
        elif field == "os_notifications":
            self.notifications.set_os_notifications(bool(value))

    def _apply_display_language(self, value: str) -> None:
        """F-86.  Only what is shown changes: A-22 requires documents, job
        snapshots and API responses to be untouched."""
        from .i18n import Lang, set_language

        self.app.update_settings(display_language=value)
        set_language(Lang(value) if value in {"ko", "en"} else Lang.SYSTEM)

    def _restart_service(self) -> None:
        """F-79: a port change restarts the service, not the app, and a
        failure is an actionable notice rather than a stoppage."""
        self.app.stop_service()
        problem = self.app.start_service()
        self._refresh_service_label()
        if problem is not None:
            self._show_notice(problem.code.value, problem.message, kind="error")

    def _open_document(self, document_id: str) -> None:
        """F-36 still applies here: opening replaces the input, so it asks."""
        try:
            document = self.app.store.get_document(document_id)
        except EchoActError as exc:
            self._show_problem(exc)
            return
        if self.reading.source_text().strip() and not self._confirm_replace():
            return
        self.reading.set_text(document.body)

    def _confirm_replace(self) -> bool:
        answer = QMessageBox.question(
            self,
            tr("Replace the text that is already here?"),
            tr("The text you have now has not been saved."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        return answer is QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def _update_enabled(self) -> None:
        busy = self.app.engine.busy
        has_text = bool(self.reading.source_text().strip())
        over = len(self.reading.source_text()) > MAX_INPUT_CODEPOINTS
        self.transport.read.setEnabled(busy or (has_text and not over))
        self.transport.set_generating(busy)
        self.voice_panel.set_locked(busy)
        playable = self._timeline is not None and self._timeline.total_frames > 0
        self.transport.pause.setEnabled(playable)
        self.transport.stop.setEnabled(playable)
        self.save_button.setEnabled(playable)
        self.open_button.setEnabled(not busy)

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _refresh_resources(self) -> None:
        """F-78 shows the configured value; the job shows what it ran under.

        This is the configured one, because no job is running when the user
        is reading it -- and when one is, the status bar carries the live
        figures instead.
        """
        from ..config.budget import resolve_budget_from_system
        from .i18n import memory_size

        try:
            budget = resolve_budget_from_system(self.app.settings)
        except EchoActError as exc:
            self.voice_panel.set_resource_summary(exc.message)
            return
        self.voice_panel.set_resource_summary(
            f"{tr('CPU')} {budget.cpu_percent}%  ·  {memory_size(budget.memory_bytes)}"
        )

    def _refresh_service_label(self) -> None:
        s = self.app.settings
        if self.app.service_running:
            state = f"{tr('Local service')} {tr('on')} · 127.0.0.1:{s.rest_port}"
        elif s.rest_enabled:
            state = tr("Integrations unavailable")
        else:
            state = f"{tr('Local service')} {tr('off')}"
        mcp = f" · MCP {tr('on') if s.mcp_enabled else tr('off')}"
        self.service_label.setText(state + mcp)

    def _refresh_usage(self) -> None:
        """F-22: usage while generating, and while idle with a model held."""
        usage = None
        try:
            usage = self.app.supervisor.usage()
        except Exception:  # noqa: BLE001 - a usage read must never break a tick
            usage = None
        if usage is None:
            self.usage_label.setText("")
            return
        from .i18n import memory_size

        self.usage_label.setText(
            f"{tr('CPU')} {usage.cpu_percent:.0f}%  ·  "
            f"{tr('Memory')} {memory_size(usage.rss_bytes)}"
        )

    def _post(self, notice: Notice) -> None:
        """Record a notice and show it.  Only notices go to the OS, and
        only when the user turned that on -- F-70 and 4.1."""
        self.notifications.post(notice)
        kind = {Level.OK: "ok", Level.ERROR: "error"}.get(notice.level, "info")
        self._show_notice(notice.title, notice.detail, kind=kind)

    def _show_notice(self, title: str, detail: str, *, kind: str = "info") -> None:
        p = self.palette_tokens
        colour = {"info": p.accent, "ok": p.ok, "error": p.danger}.get(kind, p.accent)
        name = {"info": "info", "ok": "check-circle", "error": "error"}.get(kind, "info")
        self.notice_icon.setPixmap(icons.pixmap(name, colour, 18, self.devicePixelRatioF()))
        self.notice_text.setText(f"{title} — {detail}" if detail else title)
        self.notice.show()

    def _show_problem(self, exc: EchoActError) -> None:
        """F-25: a failure is reported with a reason, not a shrug."""
        if exc.code is Code.BUSY:
            self._show_notice(tr("A job is still running."), exc.message, kind="info")
            return
        self._show_notice(exc.code.value, exc.message, kind="error")
        log.info("reported to user: %s", exc.code.value)

    def _report_startup(self) -> None:
        st = self.app.startup
        if st.interrupted_jobs:
            self._show_notice(
                tr("Interrupted"),
                f"{len(st.interrupted_jobs)}",
                kind="info",
            )
        elif st.problems:
            first = st.problems[0]
            self._show_notice(first.code.value, first.message, kind="error")

    # ------------------------------------------------------------------
    # Exit (F-77, F-52)
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self.app.engine.busy:
            answer = QMessageBox.question(
                self,
                tr("A job is still running."),
                tr("The text you have now has not been saved."),
                QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
            )
            if answer is not QMessageBox.StandardButton.Close:
                event.ignore()
                return
        self._tick.stop()
        self.bridge.detach()
        self.app.shutdown()
        event.accept()
