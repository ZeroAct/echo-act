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
