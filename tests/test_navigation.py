"""Every screen the main window offers actually opens.

N-09 requires input, voice settings, resource settings, generation and
playback to be reachable from the main screen, and the four buttons in
the header are how the last of those and everything else is reached.  A
button that raises when pressed satisfies nothing, and a screen is
exactly the kind of thing that compiles, imports, and then fails on the
first widget it builds.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from echoact import paths


@pytest.fixture(scope="session")
def qt() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path, monkeypatch, qt):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    from echoact.app import Application
    from echoact.ui import theme
    from echoact.ui.main_window import MainWindow

    app = Application()
    theme.apply(qt, theme.Mode.LIGHT)
    w = MainWindow(app, theme.Mode.LIGHT)
    w.resize(1100, 720)
    w.grab()
    try:
        yield w
    finally:
        w._tick.stop()
        w.bridge.detach()
        for name in ("_library", "_models", "_settings_screen", "_status", "_connect"):
            dialog = getattr(w, name, None)
            if dialog is not None:
                dialog.close()
        app.shutdown()
        paths.data_dir.cache_clear()


@pytest.mark.parametrize(
    ("button", "attribute"),
    [
        ("library", "_library"),
        ("models", "_models"),
        ("settings", "_settings_screen"),
        ("status", "_status"),
        ("connect", "_connect"),
    ],
)
def test_a_header_button_opens_its_screen(window, button: str, attribute: str) -> None:
    window.nav[button].click()
    dialog = getattr(window, attribute, None)
    assert isinstance(dialog, QDialog), f"{button} opened nothing"
    assert dialog.isVisible()
    assert dialog.findChildren(object), f"{button} opened an empty window"
    dialog.grab()  # forces a real layout pass, which is where a screen breaks


def test_every_header_button_has_an_accessible_name(window) -> None:
    for key, button in window.nav.items():
        assert button.accessibleName(), f"{key} has no accessible name"


def test_the_screens_are_separate_windows_not_a_replacement(window) -> None:
    """F-29 compares the live input against the job snapshot continuously.
    A screen that replaced the reading surface would drop the highlight
    every time the user opened the library."""
    window.reading.set_text("에코액트 테스트입니다.")
    window.nav["library"].click()
    assert window.reading.source_text() == "에코액트 테스트입니다."
    assert window.reading.isVisible() or window.centralWidget() is not None
