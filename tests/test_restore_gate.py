"""5.3: during a restore, new generation and edits are blocked.

The backup module has the gate and the job engine has the slot, and
neither test suite could show the two connected, because each mocked the
other. This is the connection.
"""

from __future__ import annotations

import pytest

from echoact import paths
from echoact.db.backup import RESTORE_GATE, BackupScheduler
from echoact.domain import (
    Gender,
    JobKind,
    Language,
    RequestPath,
    RetentionMode,
    SpeakingStyle,
    VoiceSettings,
)
from echoact.errors import Code, EchoActError
from echoact.jobs.request import JobRequest
from echoact.models.catalog import SUPERTONIC_3_ID


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    from echoact.app import Application

    application = Application()
    application.registry.accept_license(SUPERTONIC_3_ID)
    try:
        yield application
    finally:
        application.shutdown()
        paths.data_dir.cache_clear()


def _request() -> JobRequest:
    return JobRequest(
        text="에코액트 테스트입니다.",
        settings=VoiceSettings(
            SUPERTONIC_3_ID, Language.AUTO, Gender.FEMALE, "F1", SpeakingStyle.NATURAL, 1.0
        ),
        request_path=RequestPath.GUI,
        owner_client_id="owner",
        idempotency_key="gate-1",
        kind=JobKind.SPEECH,
        retention=RetentionMode.ONE_OFF,
    )


def test_generation_is_refused_while_a_restore_runs(app) -> None:
    with RESTORE_GATE.hold(), pytest.raises(EchoActError) as caught:
        app.engine.submit(_request())
    assert caught.value.code is Code.BUSY
    # Retryable, unlike the other refusals at this point: a restore ends.
    assert caught.value.retryable
    assert caught.value.retry_after_s


def test_the_refusal_consumes_nothing(app) -> None:
    """It must not take the slot or the duplicate-prevention key, or a
    caller that retries after the restore would find its key spent."""
    with RESTORE_GATE.hold():
        with pytest.raises(EchoActError):
            app.engine.submit(_request())
        assert not app.engine.busy
        assert not app.store.list_jobs().items

    # The same key must still be usable once the restore is over.
    job, created = app.engine.submit(_request())
    assert created
    app.engine.cancel(job.job_id)


def test_the_gate_reports_what_is_blocked(app) -> None:
    with RESTORE_GATE.hold(), pytest.raises(EchoActError) as caught:
        app.engine.submit(_request())
    assert "restored" in caught.value.message


def test_the_gate_cannot_be_left_holding(app) -> None:
    """It is a context manager rather than a begin/finish pair, so a
    restore that raises cannot block generation for the rest of the
    session."""
    with pytest.raises(RuntimeError), RESTORE_GATE.hold():
        raise RuntimeError("the restore blew up")
    assert not RESTORE_GATE.active
    job, created = app.engine.submit(_request())
    assert created
    app.engine.cancel(job.job_id)


# --------------------------------------------------------------- F-74 ---


def test_no_scheduled_backup_runs_while_the_setting_is_off(app) -> None:
    """4.1 defaults it off, and off must mean nothing happens."""
    assert app.settings.scheduled_backup is False
    assert app.run_due_backup() is None


def test_a_scheduled_backup_is_deferred_while_a_job_runs(app, tmp_path) -> None:
    """N-28 puts a scheduled backup behind the user's own work."""
    scheduler = BackupScheduler()
    decision = scheduler.decide(
        1_000_000.0,
        enabled=True,
        location=str(tmp_path / "backups"),
        last_run=None,
        generating=True,
    )
    assert not decision.run
    assert "generat" in decision.reason.value.lower()


def test_a_scheduled_backup_is_deferred_during_a_restore(tmp_path) -> None:
    scheduler = BackupScheduler()
    with RESTORE_GATE.hold():
        decision = scheduler.decide(
            1_000_000.0,
            enabled=True,
            location=str(tmp_path / "backups"),
            last_run=None,
            restoring=True,
        )
    assert not decision.run


def test_the_application_owns_a_scheduler_something_can_tick(app) -> None:
    """F-74 produces nothing unless something calls it, and until this
    existed nothing did."""
    assert isinstance(app.scheduler, BackupScheduler)
    assert callable(app.run_due_backup)
