"""The settings screen's buttons reach the application.

The view emits intent and the window does the work, which is the right
split and also the one that fails silently: a signal connected to nothing
looks exactly like a button that works. These press the signals and check
what happened to the application behind them.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtWidgets import QApplication

from echoact import paths
from echoact.domain import Capability


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
    w.grab()
    try:
        yield w
    finally:
        w._tick.stop()
        w.bridge.detach()
        screen = getattr(w, "_settings_screen", None)
        if screen is not None:
            screen.close()
        app.shutdown()
        paths.data_dir.cache_clear()


@pytest.fixture()
def settings(window, monkeypatch):
    """The settings screen, with the show-once dialog suppressed."""
    shown: list[object] = []
    monkeypatch.setattr(window, "_show_credential_once", shown.append)
    window.nav["settings"].click()
    screen = window._settings_screen.layout().itemAt(0).widget()
    return screen, shown


# ------------------------------------------------------------------ F-71 ---


def test_issuing_a_credential_reaches_the_store_and_is_shown_once(window, settings) -> None:
    screen, shown = settings
    before = len(window.app.credentials.list())
    screen.credential_issue_requested.emit("Reader bot", {Capability.GENERATE}, 30)

    assert len(window.app.credentials.list()) == before + 1
    assert len(shown) == 1, "F-71 shows the value once; nothing showed it"
    issued = shown[0]
    assert issued.token
    # The store keeps a verifier, not the value.
    assert issued.token not in repr(window.app.credentials.get(issued.ref).to_record())


def test_revoking_a_credential_cancels_that_clients_running_job(window, settings) -> None:
    """5.3: revocation blocks new calls and cancels that client's jobs.
    Revoking without cancelling would leave the slot held by a client
    that is no longer allowed to use it."""
    screen, _shown = settings
    issued = window.app.credentials.issue(
        name="bot", capabilities={Capability.GENERATE}, days=1
    )
    cancelled: list[str] = []
    window._cancel_jobs_of = cancelled.append

    screen.credential_revoke_requested.emit(issued.ref)
    assert cancelled == [issued.client_id]


def test_narrowing_capabilities_also_cancels(window, settings) -> None:
    screen, _shown = settings
    issued = window.app.credentials.issue(
        name="bot", capabilities={Capability.GENERATE, Capability.READ_RESULTS}, days=1
    )
    cancelled: list[str] = []
    window._cancel_jobs_of = cancelled.append

    screen.credential_capabilities_changed.emit(issued.ref, {Capability.READ_RESULTS})
    assert cancelled == [issued.client_id]


def test_widening_capabilities_cancels_nothing(window, settings) -> None:
    """Only a narrowing takes something away, so only a narrowing can
    invalidate work already under way."""
    screen, _shown = settings
    issued = window.app.credentials.issue(
        name="bot", capabilities={Capability.GENERATE}, days=1
    )
    cancelled: list[str] = []
    window._cancel_jobs_of = cancelled.append

    screen.credential_capabilities_changed.emit(
        issued.ref, {Capability.GENERATE, Capability.READ_RESULTS}
    )
    assert cancelled == []


# ------------------------------------------------------------------ F-76 ---


def test_resetting_integrations_leaves_the_owner_credential(window, settings) -> None:
    """F-76 revokes integration permissions. Revoking the owner's own
    credential would lock the user out of their own service, which is not
    what the scope says."""
    screen, _shown = settings
    window.app.credentials.issue(name="bot", capabilities={Capability.GENERATE}, days=1)
    screen.reset_requested.emit("integrations")

    live = [c for c in window.app.credentials.list() if c.is_usable()]
    assert live, "every credential was revoked, including the owner's"
    assert all(c.is_owner for c in live)


# ------------------------------------------------------------------ F-73 ---


def test_cleanup_reports_what_it_did(window, settings) -> None:
    screen, _shown = settings
    screen.cleanup_requested.emit()
    # isHidden rather than isVisible: the window itself is never shown in
    # this test, and a child of a hidden window is not "visible" however
    # explicitly it was shown.
    assert not window.notice.isHidden()
    assert "cleaned up" in window.notice_text.text()


# ------------------------------------------------------------------ F-75 ---


def _await(reported: list, qt) -> None:
    """Wait for one answer to land on the GUI thread.

    The check queries off the main thread (rule 7) and hands the answer
    back through a queued signal, so ``emit`` returns before anything is
    reported.  This waits for the hand-off, not the network: the module
    under the window is stubbed in every caller here.
    """
    deadline = time.monotonic() + 5
    while not reported and time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(0.01)
    assert reported, "the answer never reached the screen"


def test_the_version_check_carries_the_answer_back_to_the_screen(
    window, settings, monkeypatch, qt
) -> None:
    """F-75 queries only at the user's request and installs nothing."""
    from echoact import update

    screen, _shown = settings
    monkeypatch.setattr(update, "latest_released_version", lambda **kw: "9.9.9")
    reported: list[tuple] = []
    screen.set_released_version = lambda v, **kw: reported.append((v, kw))
    screen.version_check_requested.emit()
    _await(reported, qt)
    assert reported[0][0] == "9.9.9"
    assert not reported[0][1].get("error")


def test_a_failed_version_check_says_why_rather_than_nothing(
    window, settings, monkeypatch, qt
) -> None:
    """Silence would read as 'you are up to date', so a failed check
    arrives as an explanation of the failure instead."""
    from echoact import update
    from echoact.errors import Code, EchoActError

    def refuse(**_kw: object) -> str:
        raise EchoActError(Code.RELEASE_FEED_UNREACHABLE, "the feed refused")

    screen, _shown = settings
    monkeypatch.setattr(update, "latest_released_version", refuse)
    reported: list[tuple] = []
    screen.set_released_version = lambda v, **kw: reported.append((v, kw))
    screen.version_check_requested.emit()
    _await(reported, qt)
    assert reported[0][0] is None
    assert "refused" in reported[0][1].get("error", "")
