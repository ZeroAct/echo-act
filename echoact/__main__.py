"""Launching EchoAct.

F-85 allows one instance per user account, so the very first thing is to
ask whether one is already running -- before Qt is initialised, before the
database is opened, and above all before a second worker or a second REST
listener could exist.

F-79 then decides the shape of the rest: the service is started after the
window, and a failure to start it is a notice on that window rather than a
reason not to have one.
"""

from __future__ import annotations

import sys

from .errors import EchoActError
from .instance import AlreadyRunning, acquire
from .util.logging import configure, get_logger

log = get_logger("main")


def _fatal(message: str, lock) -> None:
    """Report and let go of the single-instance lock.

    Releasing matters: a failed start that keeps the lock makes the next
    attempt look like F-85's "another instance is running", and the user
    would then be told the opposite of what happened.
    """
    try:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "EchoAct", message)
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    configure()
    argv = list(sys.argv if argv is None else argv)

    try:
        lock = acquire()
    except AlreadyRunning:
        # F-85: surface the window that exists.  Nothing is started here,
        # and the exit is a success -- the user asked to see EchoAct and
        # EchoAct is now in front of them.
        log.info("another instance is running; asked it to show itself")
        return 0

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from .app import Application
    from .ui import theme
    from .ui.main_window import MainWindow

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings, True)
    qt = QApplication(argv)
    qt.setApplicationName("EchoAct")
    qt.setOrganizationName("EchoAct")

    # Before anything measures a glyph: a face registered later would not
    # be the one the first layout used, and N-13's stability is about the
    # layout not changing under the reader.
    from .ui import fonts

    report = fonts.load()
    if not report.metrics_are_portable:
        log.info("no bundled typeface; layout measurements are machine-specific")

    try:
        app = Application()
    except EchoActError as exc:
        # Logged before it is shown. A dialog is seen once by whoever is
        # at the machine; F-25 wants a failure reported, and F-72's
        # diagnostic export can only carry what reached the log.
        log.error("startup failed: %s: %s", exc.code.value, exc.message)
        _fatal(exc.message, lock)
        return 1
    except Exception as exc:  # noqa: BLE001 - the last thing between us and silence
        # Anything that is not an EchoActError is a defect rather than a
        # condition, and until now it left the process holding a dialog
        # with nothing in the log to say why.
        log.exception("startup failed unexpectedly")
        _fatal(f"EchoAct could not start ({type(exc).__name__}).", lock)
        return 1

    window = MainWindow(app, theme.Mode.SYSTEM)

    def raise_window() -> None:
        """Called from the instance listener's thread.

        ``QMetaObject.invokeMethod`` with a queued connection is the only
        safe way to touch a widget from there.
        """
        from PySide6.QtCore import QMetaObject

        QMetaObject.invokeMethod(window, "show", Qt.ConnectionType.QueuedConnection)
        QMetaObject.invokeMethod(window, "raise_", Qt.ConnectionType.QueuedConnection)
        QMetaObject.invokeMethod(window, "activateWindow", Qt.ConnectionType.QueuedConnection)

    lock._on_activate = raise_window  # noqa: SLF001 - set once, before any use

    window.show()
    app.start_service()
    window._refresh_service_label()  # noqa: SLF001 - the window is ours

    try:
        return qt.exec()
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
