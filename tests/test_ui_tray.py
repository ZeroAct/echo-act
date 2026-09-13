"""Closing the window hides it; only the tray's Quit ends the app.

The offscreen platform has no tray, so ``build_tray`` is monkeypatched
here: what is under test is the window's branch -- hide or quit, ask or
not ask, tell the user once -- and not Qt's shell integration.  The real
tray is built only where ``QSystemTrayIcon.isSystemTrayAvailable()`` says
there is one, and ``None`` there is a supported outcome the window honours
by closing for real.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from echoact import paths
from echoact.ui import theme


@pytest.fixture(scope="session")
def qt() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def app(tmp_path, monkeypatch, qt):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    from echoact.app import Application

    application = Application()
    calls: list[int] = []
    real = application.shutdown

    def shutdown() -> None:
        calls.append(1)
        if len(calls) == 1:
            real()

    # Once, exactly: the window's own exit path and this fixture's teardown
    # both reach shutdown, and which one runs first depends on fixture
    # finalisation order.
    monkeypatch.setattr(application, "shutdown", shutdown)
    application.shutdown_calls = calls  # type: ignore[attr-defined]
    try:
        yield application
    finally:
        application.shutdown()
        paths.data_dir.cache_clear()


class _IconStub:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def showMessage(self, title: str, detail: str, _icon: object, _ms: int) -> None:  # noqa: N802
        self.messages.append((title, detail))


class _TrayStub(QObject):
    show_requested = Signal()
    stop_requested = Signal()
    quit_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.icon = _IconStub()
        self.hidden = False
        self.notices: list[tuple[str, str]] = []
        self.stop_enabled: bool | None = None

    def notify(self, title: str, detail: str = "") -> None:
        self.notices.append((title, detail))

    def hide(self) -> None:
        self.hidden = True

    def show(self) -> None:
        pass

    def set_tooltip(self, _text: str) -> None:
        pass

    def set_stop_enabled(self, enabled: bool) -> None:
        self.stop_enabled = enabled

    def retranslate(self) -> None:
        pass


@pytest.fixture()
def tray(app, monkeypatch):
    stub = _TrayStub()
    monkeypatch.setattr("echoact.ui.main_window.build_tray", lambda parent, icon: stub)
    return stub


@pytest.fixture()
def window(app, qt, tray):
    theme.apply(qt, theme.Mode.LIGHT)
    from echoact.ui.main_window import MainWindow

    w = MainWindow(app, theme.Mode.LIGHT)
    try:
        yield w
    finally:
        w._tick.stop()
        w.bridge.detach()


def _engine_busy(window, monkeypatch, busy: bool) -> None:
    monkeypatch.setattr(type(window.app.engine), "busy", property(lambda self: busy))


def test_closing_the_window_hides_it_to_the_tray_instead_of_quitting(window, tray) -> None:
    """The feature, in one line: close hides, nothing is torn down."""
    assert window.app.settings.close_to_tray, "tray-close should be the default"
    window.close()
    assert window.isHidden()
    assert window.app.shutdown_calls == []  # type: ignore[attr-defined]


def test_hiding_a_running_job_asks_nothing(window, tray, monkeypatch) -> None:
    """A job continuing while hidden is the point, not an accident to confirm."""
    asked: list[str] = []
    monkeypatch.setattr(
        window,
        "_confirm_quit_with_job",
        lambda: asked.append("asked") or True,
    )
    _engine_busy(window, monkeypatch, True)
    window.close()
    assert window.isHidden()
    assert asked == []
    assert window.app.shutdown_calls == []  # type: ignore[attr-defined]


def test_closing_the_window_quits_when_close_to_tray_is_off(window, tray, monkeypatch) -> None:
    """The opt-out is honoured, and it tears the application down."""
    monkeypatch.setattr(window, "_confirm_quit_with_job", lambda: True)
    window.app.update_settings(close_to_tray=False)
    window.close()
    assert window.app.shutdown_calls == [1]  # type: ignore[attr-defined]
    assert tray.hidden, "the tray icon goes with the application"


def test_closing_the_window_quits_when_there_is_no_tray(app, qt, monkeypatch) -> None:
    """A session without a tray keeps exactly the pre-tray behaviour."""
    monkeypatch.setattr("echoact.ui.main_window.build_tray", lambda parent, icon: None)
    theme.apply(qt, theme.Mode.LIGHT)
    from echoact.ui.main_window import MainWindow

    w = MainWindow(app, theme.Mode.LIGHT)
    monkeypatch.setattr(w, "_confirm_quit_with_job", lambda: True)
    try:
        assert w.tray is None
        w.close()
    finally:
        w._tick.stop()
        w.bridge.detach()
    assert app.shutdown_calls == [1]  # type: ignore[attr-defined]


def test_quitting_from_the_tray_shuts_the_application_down(window, tray) -> None:
    """The tray's Quit is the same teardown the close button used to be."""
    tray.quit_requested.emit()
    assert window.app.shutdown_calls == [1]  # type: ignore[attr-defined]
    assert tray.hidden
    assert not window._tick.isActive()


def test_hiding_to_the_tray_is_explained_only_the_first_time(window, tray) -> None:
    """The taskbar button vanishing must not read as a quit -- but say it once."""
    window.close()
    assert len(tray.notices) == 1
    window.show_from_tray()
    window.close()
    assert len(tray.notices) == 1


def test_os_notifications_are_mirrored_to_the_tray(window, tray) -> None:
    """F-70 completed: the setting finally has a sink to reach."""
    from echoact.ui.notifications import failure_notice

    window.notifications.set_os_notifications(True)
    window.notifications.post(failure_notice("Backup failed"))
    assert tray.icon.messages == [("Backup failed", "")]


def test_the_controller_itself_builds_a_menu_that_works(qt) -> None:
    """The real icon, not the stub: constructed even where no tray exists,
    because ``isSystemTrayAvailable()`` gates only ``build_tray`` -- and a
    bad parent or signal here is a crash at startup, off any test path
    until someone runs the app."""
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QSystemTrayIcon

    from echoact.ui.tray import TrayController

    controller = TrayController(qt, QIcon())
    texts = [a.text() for a in controller.icon.contextMenu().actions() if a.text()]
    assert texts == ["Show EchoAct", "Stop", "Quit EchoAct"]
    seen: list[str] = []
    controller.show_requested.connect(lambda: seen.append("show"))
    controller._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
    controller._on_activated(QSystemTrayIcon.ActivationReason.Context)
    assert seen == ["show"], "a click restores the window; the menu click is its own"


def test_build_tray_defers_to_the_platform(qt, monkeypatch) -> None:
    from PySide6.QtCore import QObject
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QSystemTrayIcon

    from echoact.ui import tray as tray_module

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    assert tray_module.build_tray(QObject(), QIcon()) is None
