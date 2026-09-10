"""F-70's notices, and the two things they must not do."""

from __future__ import annotations

from echoact.ui import i18n
from echoact.ui.notifications import (
    Level,
    NotificationCentre,
    completion_notice,
    failure_notice,
)

NOW = 1_760_000_000.0


def test_a_job_a_client_created_names_the_client_and_its_expiry() -> None:
    """F-70: work produced while the user was not watching must still be
    findable, and a one-off result says when it goes."""
    notice = completion_notice(
        job_id="job_1", client_label="Reader bot", expires_at=NOW + 3600, now=NOW
    )
    assert notice.level is Level.OK
    assert "Reader bot" in notice.detail
    assert "hour" in notice.detail
    assert notice.job_id == "job_1"
    assert notice.actionable


def test_a_job_the_user_started_needs_no_explanation() -> None:
    notice = completion_notice(job_id="job_1", client_label=None, expires_at=None, now=NOW)
    assert notice.detail == ""
    assert notice.client_label is None


def test_the_expiry_phrase_tracks_how_long_is_left() -> None:
    soon = completion_notice(job_id="j", client_label="c", expires_at=NOW + 30, now=NOW)
    later = completion_notice(job_id="j", client_label="c", expires_at=NOW + 600, now=NOW)
    assert "under a minute" in soon.detail
    assert "10 minutes" in later.detail


def test_a_notice_carries_no_body_text() -> None:
    """N-20 keeps body text out of anything that leaves the app, and an OS
    notification is exactly that. The type has nowhere to put it."""
    notice = completion_notice(job_id="job_1", client_label="bot", expires_at=None, now=NOW)
    fields = notice.__slots__
    assert "text" not in fields
    assert "source_text" not in fields
    assert "audio" not in fields


def test_the_centre_only_reaches_the_os_when_the_user_allowed_it() -> None:
    """4.1 defaults OS notifications off."""
    sent: list[str] = []
    centre = NotificationCentre(os_notifications=False, os_sink=lambda n: sent.append(n.title))
    centre.post(failure_notice("Generation failed"))
    assert sent == []
    assert len(centre.notices) == 1

    centre.set_os_notifications(True)
    centre.post(failure_notice("Download failed"))
    assert sent == ["Download failed"]


def test_the_centre_does_not_grow_without_bound() -> None:
    centre = NotificationCentre()
    for i in range(200):
        centre.post(failure_notice(f"failure {i}"))
    assert len(centre.notices) == NotificationCentre.MAX_NOTICES
    assert centre.notices[-1].title == "failure 199"


def test_listeners_see_every_notice() -> None:
    seen: list[str] = []
    centre = NotificationCentre()
    off = centre.listen(lambda n: seen.append(n.title))
    centre.post(failure_notice("one"))
    off()
    centre.post(failure_notice("two"))
    assert seen == ["one"]


def test_notices_translate_with_the_display_language() -> None:
    try:
        i18n.set_language(i18n.Lang.KO)
        notice = completion_notice(job_id="j", client_label=None, expires_at=None, now=NOW)
        assert notice.title == "읽기가 끝났습니다"
    finally:
        i18n.set_language(i18n.Lang.EN)
