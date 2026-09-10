"""Telling the user something happened, per F-70.

Generation completion, download failure, resource shortage and backup
failure are notified inside the app.  Three constraints shape this:

* An OS notification only appears if the user turned them on (4.1 defaults
  it off), and it never contains body text or audio (F-70, N-20).
* Clicking a notification does not auto-play audio (F-70).  Nor does it
  for an in-app notice, and F-51 is stricter still: a job an integration
  created never auto-plays at all.
* A completion notice for a job an integration created names the client
  that asked for it and, where the result is one-off, says when it
  expires -- so work produced while nobody was watching can still be found
  and saved before it is cleaned up.

In-app notices are a list the window renders.  The OS notification is a
best-effort extra: on Windows Qt's tray message is the only route that does
not add a dependency, and if there is no tray the notice still exists
in-app, which is where F-70 puts it anyway.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from ..util import ids
from .i18n import add_korean, tr

add_korean(
    {
        "Reading finished": "읽기가 끝났습니다",
        "{client} asked for this": "{client}이(가) 요청했습니다",
        "It will be cleaned up {when}": "{when}에 정리됩니다",
        "in under a minute": "1분 이내",
        "in about {n} minutes": "약 {n}분 후",
        "in about an hour": "약 1시간 후",
        "Generation failed": "생성에 실패했습니다",
        "Download failed": "다운로드에 실패했습니다",
        "Not enough memory": "메모리가 부족합니다",
        "Backup failed": "백업에 실패했습니다",
        "Open this job": "이 작업 열기",
    }
)


class Level(StrEnum):
    INFO = "info"
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Notice:
    """One thing worth telling the user.

    ``job_id`` is what "offers to open that job" needs; the notice itself
    carries no text and no audio, because the same object reaches the OS
    notification, which N-20 keeps clean.
    """

    notice_id: str
    level: Level
    title: str
    detail: str = ""
    job_id: str | None = None
    client_label: str | None = None
    expires_at: float | None = None
    created_at: float = field(default_factory=ids.now)
    #: True when the user may act on it, e.g. open the job it names.
    actionable: bool = False


def _expiry_phrase(expires_at: float, now: float) -> str:
    remaining = max(0.0, expires_at - now)
    if remaining < 60:
        return tr("in under a minute")
    if remaining < 3000:
        return tr("in about {n} minutes").format(n=int(round(remaining / 60)))
    return tr("in about an hour")


def completion_notice(
    *,
    job_id: str,
    client_label: str | None,
    expires_at: float | None,
    now: float | None = None,
) -> Notice:
    """F-70's completion notice, including the two parts easy to leave out.

    A job the desktop user started needs no explanation.  A job an
    integration created does: the user did not ask for it, may not know it
    ran, and if its result is one-off it disappears within the hour 4.1
    allows.  Both facts go in the notice rather than only in a screen the
    user would have to think to open.
    """
    moment = ids.now() if now is None else now
    parts: list[str] = []
    if client_label:
        parts.append(tr("{client} asked for this").format(client=client_label))
    if expires_at is not None:
        parts.append(tr("It will be cleaned up {when}").format(when=_expiry_phrase(expires_at, moment)))
    return Notice(
        notice_id=ids.request_id(),
        level=Level.OK,
        title=tr("Reading finished"),
        detail="  ·  ".join(parts),
        job_id=job_id,
        client_label=client_label,
        expires_at=expires_at,
        created_at=moment,
        actionable=True,
    )


def failure_notice(title: str, detail: str = "", *, job_id: str | None = None) -> Notice:
    return Notice(
        notice_id=ids.request_id(),
        level=Level.ERROR,
        title=title,
        detail=detail,
        job_id=job_id,
        actionable=job_id is not None,
    )


class NotificationCentre:
    """Holds recent notices and, when allowed, mirrors them to the OS.

    Bounded on purpose: N-23's spirit is that nothing accumulates without
    limit, and a client generating in a loop would otherwise grow this
    list for as long as the app runs.
    """

    MAX_NOTICES = 50

    def __init__(
        self,
        *,
        os_notifications: bool = False,
        os_sink: Callable[[Notice], None] | None = None,
    ) -> None:
        self._notices: list[Notice] = []
        self._listeners: list[Callable[[Notice], None]] = []
        self.os_notifications = os_notifications
        self._os_sink = os_sink

    def set_os_notifications(self, enabled: bool) -> None:
        self.os_notifications = enabled

    def listen(self, fn: Callable[[Notice], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def post(self, notice: Notice) -> Notice:
        self._notices.append(notice)
        del self._notices[: max(0, len(self._notices) - self.MAX_NOTICES)]
        for fn in list(self._listeners):
            fn(notice)
        if self.os_notifications and self._os_sink is not None:
            # Title and detail only: neither ever holds body text or audio,
            # which is what makes mirroring them outside the app safe.
            self._os_sink(notice)
        return notice

    @property
    def notices(self) -> tuple[Notice, ...]:
        return tuple(self._notices)

    def unread_count(self) -> int:
        return len(self._notices)

    def clear(self) -> None:
        self._notices.clear()


def tray_sink(tray) -> Callable[[Notice], None]:
    """Mirror a notice to a ``QSystemTrayIcon``.

    Separate from the centre so that the centre stays testable without Qt,
    and so a machine with no tray degrades to in-app notices rather than
    to an exception.
    """
    from PySide6.QtWidgets import QSystemTrayIcon

    icon_for = {
        Level.INFO: QSystemTrayIcon.MessageIcon.Information,
        Level.OK: QSystemTrayIcon.MessageIcon.Information,
        Level.WARNING: QSystemTrayIcon.MessageIcon.Warning,
        Level.ERROR: QSystemTrayIcon.MessageIcon.Critical,
    }

    def send(notice: Notice) -> None:
        try:
            tray.showMessage(notice.title, notice.detail, icon_for[notice.level], 6000)
        except Exception:  # noqa: BLE001 - a missing tray is not a failure
            pass

    return send
