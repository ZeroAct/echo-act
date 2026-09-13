"""The main window's own behaviour, without generating anything.

The real ``Application`` is built, against an isolated data directory, but
nothing here starts a job: what is under test is the window's decisions --
what is enabled, what is locked, what it refuses to start -- and not
synthesis.  N-09 makes "unavailable controls are disabled" a requirement
rather than a nicety, so it is worth a test that would fail if a button
stayed live.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from echoact import paths
from echoact.domain import Gender, Language, SpeakingStyle, VoiceSettings
from echoact.models.catalog import MANIFEST, SUPERTONIC_3_ID
from echoact.ui import i18n, theme


@pytest.fixture(scope="session")
def qt() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def app(tmp_path, monkeypatch, qt):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    from echoact.app import Application

    application = Application()
    try:
        yield application
    finally:
        application.shutdown()
        paths.data_dir.cache_clear()


@pytest.fixture()
def window(app, qt):
    theme.apply(qt, theme.Mode.LIGHT)
    from echoact.ui.main_window import MainWindow

    w = MainWindow(app, theme.Mode.LIGHT)
    w.resize(1100, 720)
    w.ensurePolished()
    w.grab()
    try:
        yield w
    finally:
        w._tick.stop()
        w.bridge.detach()


# --------------------------------------------------------------- N-09 ---


def test_reading_is_unavailable_with_no_text(window) -> None:
    assert not window.transport.read.isEnabled()
    window.reading.set_text("에코액트 테스트입니다.")
    assert window.transport.read.isEnabled()


def test_reading_is_unavailable_with_only_whitespace(window) -> None:
    """F-03 refuses whitespace-only input, so the button must not invite it."""
    window.reading.set_text("   \n\t  ")
    assert not window.transport.read.isEnabled()


def test_saving_and_playing_are_unavailable_before_there_is_audio(window) -> None:
    assert not window.save_button.isEnabled()
    assert not window.transport.pause.isEnabled()
    assert not window.transport.stop.isEnabled()


def test_every_icon_control_has_an_accessible_name(window) -> None:
    """N-30: icons must have accessible names and descriptions."""
    from PySide6.QtWidgets import QToolButton

    for button in window.findChildren(QToolButton):
        assert button.accessibleName(), f"{button.objectName() or button} has no accessible name"


# --------------------------------------------------------------- F-88 ---


def test_the_estimate_appears_as_the_user_types(window) -> None:
    window.reading.set_text("에코액트는 문서를 소리내어 읽어 줍니다. 두 번째 문장입니다.")
    text = window.estimate_label.text()
    assert "segment" in text
    assert "about" in text


def test_the_counter_shows_the_limit(window) -> None:
    window.reading.set_text("가나다")
    assert "3" in window.counter.text()
    assert "50,000" in window.counter.text()


# --------------------------------------------------------------- N-11 ---


def test_reading_does_not_start_while_the_licence_is_unaccepted(window, app) -> None:
    """The window asks before it submits, so the refusal is a screen and
    not an error code in a notice bar."""
    assert app.licence_pending() == SUPERTONIC_3_ID
    window.reading.set_text("에코액트 테스트입니다.")

    asked: list[str] = []
    window._licence_accepted = lambda: (asked.append("asked"), False)[1]
    window._primary_action()
    assert asked == ["asked"]
    assert window._job_id is None, "a job was created despite the licence being unaccepted"


# -------------------------------------------------------- voice panel ---


def test_the_voice_panel_returns_real_enum_members(window) -> None:
    """Qt hands item data back as a plain str even for a StrEnum, and a
    StrEnum compares equal to its value -- so the mistake survives every
    comparison and only surfaces when something asks for .value."""
    settings = window.voice_panel.settings()
    assert isinstance(settings.language, Language)
    assert isinstance(settings.gender, Gender)
    assert isinstance(settings.style, SpeakingStyle)
    assert settings.to_dict()["language"] == "auto"


def test_changing_the_gender_changes_the_voices_offered(window) -> None:
    entry = MANIFEST.get(SUPERTONIC_3_ID)
    female = {v.voice_id for v in entry.voices_for(Gender.FEMALE)}
    male = {v.voice_id for v in entry.voices_for(Gender.MALE)}
    assert female and male and not (female & male)

    panel = window.voice_panel
    panel.gender.setCurrentIndex(panel.gender.findData(Gender.MALE))
    offered = {panel.voice.itemData(i) for i in range(panel.voice.count())}
    assert offered == male


def test_each_voice_carries_its_description_where_a_person_can_see_it(window) -> None:
    """F-53 says the descriptions are shown in the GUI as well."""
    from PySide6.QtCore import Qt

    panel = window.voice_panel
    for i in range(panel.voice.count()):
        tip = panel.voice.itemData(i, Qt.ItemDataRole.ToolTipRole)
        assert tip and len(tip) > 20


def test_applying_remembered_settings_emits_nothing(window) -> None:
    """F-24 restores settings; restoring them must not look like the user
    changing them, or every launch would write the file back."""
    changes: list[object] = []
    window.voice_panel.changed.connect(changes.append)
    window.voice_panel.apply(
        VoiceSettings(SUPERTONIC_3_ID, Language.KO, Gender.MALE, "M2", SpeakingStyle.CALM, 1.2),
        autoplay=False,
        follow=False,
    )
    assert changes == []
    assert window.voice_panel.settings().voice_id == "M2"
    assert window.voice_panel.settings().tempo == pytest.approx(1.2)


def test_the_tempo_slider_covers_exactly_the_range_the_document_fixes(window) -> None:
    slider = window.voice_panel.tempo
    assert slider.minimum() == 70
    assert slider.maximum() == 150


# ------------------------------------------------------------- F-86 ---


def test_the_window_can_be_built_in_korean(app, qt, tmp_path) -> None:
    """A-22: switching the display language changes what is shown and
    nothing else."""
    from echoact.ui.main_window import MainWindow

    try:
        i18n.set_language(i18n.Lang.KO)
        theme.apply(qt, theme.Mode.LIGHT)
        w = MainWindow(app, theme.Mode.LIGHT)
        w.resize(1100, 720)
        w.grab()
        assert "소리내어" in w.transport.read.text()
        w._tick.stop()
        w.bridge.detach()
    finally:
        i18n.set_language(i18n.Lang.EN)


def test_switching_language_rewords_the_window_that_is_already_open(app, qt) -> None:
    """The reported bug: choosing another display language saved the new
    choice, and every line already on the screen kept speaking the old one
    until the next restart (F-86)."""
    from echoact.config.settings import DisplayLanguage
    from echoact.ui.main_window import MainWindow

    theme.apply(qt, theme.Mode.LIGHT)
    w = MainWindow(app, theme.Mode.LIGHT)
    w.grab()
    try:
        assert "Read aloud" in w.transport.read.text()
        assert "Settings" in w.nav["settings"].text()
        assert "Ready to read" in w.status.text()

        # The enum, as the settings screen emits it -- not its string.
        w._apply_display_language(DisplayLanguage.KO)
        assert "소리내어" in w.transport.read.text()
        assert "설정" in w.nav["settings"].text()
        assert "읽을 준비" in w.status.text()

        # Re-wording must re-word, not rebuild: the chosen style is the
        # proof, because a rebuild would reset it silently.
        w.voice_panel.style.setCurrentIndex(2)  # Bright
        w._apply_display_language(DisplayLanguage.EN)
        assert "Read aloud" in w.transport.read.text()
        assert w.voice_panel.style.currentData() == SpeakingStyle.BRIGHT
        assert "밝게" not in w.voice_panel.style.itemText(2)
    finally:
        i18n.set_language(i18n.Lang.EN)
        w._tick.stop()
        w.bridge.detach()


def test_a_status_set_while_generating_survives_the_switch_in_its_state(app, qt) -> None:
    """A status is re-rendered from its source, so a line set mid-job
    re-words to *the thing actually happening* rather than to a stale
    sentence -- and the transport button says *cancel*, not *read*, in
    the new language too (F-86)."""
    from echoact.config.settings import DisplayLanguage
    from echoact.domain import JobState
    from echoact.jobs.engine import Event, EventKind
    from echoact.ui.main_window import MainWindow

    theme.apply(qt, theme.Mode.LIGHT)
    w = MainWindow(app, theme.Mode.LIGHT)
    w.grab()
    try:
        w._job_id = "job-1"
        w._on_state(Event(kind=EventKind.STATE, job_id="job-1", state=JobState.GENERATING))
        assert "Generating" in w.status.text()
        # The engine here is idle, so _update_enabled would keep the button
        # on *read*; the flag is what carries the job's state to the button,
        # so it is set the way a busy engine would set it.
        w.transport.set_generating(True)
        assert "Cancel generation" in w.transport.read.text()
        w._apply_display_language(DisplayLanguage.KO)
        assert "생성 취소" in w.transport.read.text()
        assert "생성 중" in w.status.text()
    finally:
        i18n.set_language(i18n.Lang.EN)
        w._tick.stop()
        w.bridge.detach()


def _external_job(app, text: str = "클라이언트가 요청한 문장입니다.") -> str:
    """A finished one-segment job owned by a client, with real audio."""
    import numpy as np

    from echoact.audio import wav
    from echoact.db.store import Store  # noqa: F401 - documents what owns the rows
    from echoact.domain import (
        Budget,
        Job,
        JobKind,
        JobState,
        RequestPath,
        RetentionMode,
        TimeRange,
    )
    from echoact.jobs.request import JobRequest, plan_segments
    from echoact.util import ids

    rate = MANIFEST.get(SUPERTONIC_3_ID).sample_rate
    settings = VoiceSettings(SUPERTONIC_3_ID, Language.AUTO, Gender.FEMALE, "F1", SpeakingStyle.NATURAL, 1.0)
    request = JobRequest(
        text=text,
        settings=settings,
        request_path=RequestPath.REST,
        owner_client_id="cli_test",
        idempotency_key="k-external",
    )
    job = Job(
        job_id=ids.job_id(),
        kind=JobKind.SPEECH,
        request_path=RequestPath.REST,
        owner_client_id="cli_test",
        state=JobState.ACCEPTED,
        source_text=text,
        settings=settings,
        budget=Budget(cpu_percent=20, memory_bytes=2 << 30, intra_op_threads=2),
        retention=RetentionMode.ONE_OFF,
        created_at=ids.now(),
        client_label="Claude Desktop",
        idempotency_key="k-external",
    )
    stored, _created = app.store.claim_job(
        job, client_id="cli_test", key="k-external", request_digest=request.digest()
    )
    segments = app.store.insert_segments(stored.job_id, plan_segments(text, settings))

    frames = rate // 10
    start = 0
    for segment in segments:
        if not segment.is_spoken:
            continue
        out = paths.temp_dir() / stored.job_id / f"{segment.index:05d}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        t = np.arange(frames, dtype=np.float32) / rate
        wav.write_segment(out, (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), rate)
        app.store.mark_segment_ready(
            stored.job_id,
            segment.index,
            time=TimeRange(
                wav.ms_for_frames(start, rate), wav.ms_for_frames(start + frames, rate)
            ),
            audio_path=str(out),
            frame_count=frames,
        )
        start += frames
    app.store.update_job_state(stored.job_id, JobState.PREPARING_MODEL)
    app.store.update_job_state(stored.job_id, JobState.GENERATING)
    app.store.update_job_state(stored.job_id, JobState.COMPLETE)
    return stored.job_id


@pytest.fixture()
def silent(window, monkeypatch):
    """The window with a player that opens no device.

    ``play`` is the only call that would reach PortAudio, and a test must not
    make a sound or depend on a machine having a speaker.
    """
    started: list[int] = []
    monkeypatch.setattr(window.app.player, "play", lambda: started.append(1))
    return window, started


def test_an_external_request_plays_without_touching_the_text_on_screen(silent, app) -> None:
    """F-89 and F-51: the speaker is all an external request gets.

    The input, its highlight, and the job the window is following are the
    person's own, and a request from an app must leave every one of them
    exactly as it was.
    """
    window, started = silent
    window.reading.set_text("제가 쓰던 본문입니다.")
    job_id = _external_job(app)

    app.play_requests.request(job_id, client_label="Claude Desktop")

    assert started == [1]
    assert window.external.active
    assert window.reading.source_text() == "제가 쓰던 본문입니다."
    assert window._job_id is None
    assert window.reading.state == "none"


def test_the_window_names_the_client_and_keeps_stop_reachable(silent, app) -> None:
    """F-89 leans on F-69: silencing it is reachable from the main screen.

    And the banner names the client for the reason F-70 does -- audio that
    starts on its own is otherwise unexplainable."""
    window, _started = silent
    job_id = _external_job(app)

    app.play_requests.request(job_id, client_label="Claude Desktop")

    assert window.transport.stop.isEnabled()
    assert "Claude Desktop" in window.notice_text.text()


def test_the_owner_takes_the_speaker_back_by_stopping(silent, app) -> None:
    """F-50: the owner reclaims by asking. Afterwards the gate must know the
    external job no longer holds the player."""
    window, _started = silent
    job_id = _external_job(app)
    app.play_requests.request(job_id, client_label="Claude Desktop")

    window._stop()

    assert not window.external.active
    assert app.play_requests.current is None
    assert not window.notice.isVisibleTo(window)


def test_an_external_request_does_not_move_the_owners_highlight(silent, app) -> None:
    """The player's position belongs to text that is not on screen. F-89
    reports the highlight unavailable rather than pointing it at the wrong
    characters, which is what a tick that ran anyway would do."""
    window, _started = silent
    window.reading.set_text("제가 쓰던 본문입니다. 두 번째 문장입니다.")
    job_id = _external_job(app)
    app.play_requests.request(job_id, client_label="Claude Desktop")

    window._on_tick()

    assert window.reading.state == "none"
    assert window.reading.source_text().startswith("제가 쓰던 본문입니다.")
