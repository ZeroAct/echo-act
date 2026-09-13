"""Living beside the clock.

Closing the window hides EchoAct rather than ending it, because a job
already accepted (F-47) must run to its end whatever the window is doing;
the tray icon is then the only visible half of the app, so it carries the
three things that still need a hand while nobody is watching: bring the
window back, silence playback, and truly quit.

It also completes F-70's other half.  ``notifications.tray_sink`` was
written for exactly this icon and had never been constructed, so OS
notices had nowhere to go; the icon is built whenever the platform offers
one, whether or not closing hides.  The close button obeys the
``close_to_tray`` setting, and no tray (a headless test run, a session
without one) means the window closes and quits exactly as it always did.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .controls import apply_translations, remember
from .i18n import add_korean, tr

add_korean(
    {
        "Show EchoAct": "EchoAct 열기",
        "Quit EchoAct": "EchoAct 종료",
        # "Stop" is translated in i18n.py already.
    }
)

#: Reasons that mean "the user wants the window back".  ``Context`` is the
#: menu, which handles its own clicks, and the middle click has no meaning
#: here; a press is ``Trigger``, whatever the platform's one-or-two-click
#: convention turns out to be.
_RESTORE = (
    QSystemTrayIcon.ActivationReason.Trigger,
    QSystemTrayIcon.ActivationReason.DoubleClick,
)


class TrayController(QObject):
    """The tray icon and its menu; it emits intent and decides nothing.

    Same contract as the transport bar: the window owns every choice, so
    quitting from here travels the very teardown ``closeEvent`` uses.
    """

    show_requested = Signal()
    stop_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject, icon: QIcon) -> None:
        super().__init__(parent)
        self.icon = QSystemTrayIcon(icon, self)
        # QMenu wants a *widget* parent and a tray controller is not one;
        # setContextMenu does not take ownership either.  The reference here
        # is what keeps the menu's C++ object alive as long as the icon.
        menu = QMenu()
        self._menu = menu
        show = menu.addAction("")
        remember(self, lambda: show.setText(tr("Show EchoAct")))
        show.triggered.connect(self.show_requested.emit)
        menu.addSeparator()
        self._stop = menu.addAction("")
        remember(self, lambda: self._stop.setText(tr("Stop")))
        self._stop.triggered.connect(self.stop_requested.emit)
        menu.addSeparator()
        quit_ = menu.addAction("")
        remember(self, lambda: quit_.setText(tr("Quit EchoAct")))
        quit_.triggered.connect(self.quit_requested.emit)
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self._tooltip = "EchoAct"
        self.icon.setToolTip(self._tooltip)
        self.icon.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:  # noqa: N802
        if reason in _RESTORE:
            self.show_requested.emit()

    def retranslate(self) -> None:
        apply_translations(self)

    def set_tooltip(self, text: str) -> None:
        # Called from the window's 40 ms tick; re-setting identical text
        # would churn the shell's notification area for nothing.
        if text != self._tooltip:
            self._tooltip = text
            self.icon.setToolTip(text)

    def set_stop_enabled(self, enabled: bool) -> None:
        self._stop.setEnabled(enabled)

    def notify(self, title: str, detail: str = "") -> None:
        # A machine with per-app notifications turned off is not a failure;
        # tray_sink makes the same bargain for the same reason.
        try:
            self.icon.showMessage(title, detail, QSystemTrayIcon.MessageIcon.Information, 6000)
        except Exception:  # noqa: BLE001 - the notice still exists in-app
            pass

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()


def build_tray(parent: QObject, app_icon: QIcon) -> TrayController | None:
    """The tray, or ``None`` where there is nowhere to put one.

    ``None`` is a supported outcome, not a degraded one: the caller keeps
    its pre-tray behaviour, including stock quit-on-last-window-closed.
    """
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    return TrayController(parent, app_icon)
