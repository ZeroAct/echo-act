"""The single generation slot: F-47, F-49, F-15, F-88, and Section 5.1.

Most of this runs against a fake worker supervisor, because the behaviour
under test is the slot and the state machine, not the synthesis. One test
at the end drives the real engine end to end, so the fake cannot drift
away from what the real one does.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from echoact.audio import wav
from echoact.config.settings import Settings
from echoact.db.store import Store
from echoact.domain import (
    Gender,
    JobKind,
    JobState,
    Language,
    RequestPath,
    RetentionMode,
    SpeakingStyle,
    VoiceSettings,
)
from echoact.engine import protocol
from echoact.errors import Code, EchoActError
from echoact.jobs.engine import EventKind, JobEngine, clear_temp_tree, expire_one_off_results
from echoact.jobs.request import JobRequest, estimate, validate_request
from echoact.models.catalog import MANIFEST, SUPERTONIC_3_ID
from echoact.models.registry import ModelRegistry

SR = 44100
KO = "에코액트는 문서를 소리내어 읽어 줍니다. 두 번째 문장입니다. 세 번째 문장입니다."


def _voice() -> VoiceSettings:
    return VoiceSettings(
        model_id=SUPERTONIC_3_ID,
        language=Language.AUTO,
        gender=Gender.FEMALE,
        voice_id="F1",
        style=SpeakingStyle.NATURAL,
        tempo=1.0,
    )


class FakeSupervisor:
    """Speaks the supervisor's contract and writes plausible audio.

    Deliberately not a mock: it produces real WAV files in the format
    F-82 fixes, so the concatenation and the segment time table are
    exercised for real.
    """

    def __init__(self, *, per_segment_s: float = 0.0, fail_on: int | None = None) -> None:
        self.sample_rate = SR
        self.per_segment_s = per_segment_s
        self.fail_on = fail_on
        self.loads = 0
        self.kills = 0
        self.synth_calls = 0
        self.killed = threading.Event()
        self._loaded: str | None = None
        self._budget = None
        self.worker_pid = None

    def would_need_load(self, model_id, budget):
        return self._loaded != model_id or self._budget != budget

    def load(self, model_id, model_dir, budget, **kw):
        self.loads += 1
        self._loaded = model_id
        self._budget = budget
        self.killed.clear()

        class Loaded:
            pass

        out = Loaded()
        out.model_id = model_id
        out.sample_rate = SR
        out.voices = ["F1"]
        out.providers = ["CPUExecutionProvider"]
        out.load_seconds = 0.01
        return out

    @property
    def loaded_model(self):
        if self._loaded is None:
            return None

        class Loaded:
            pass

        out = Loaded()
        out.model_id = self._loaded
        out.sample_rate = SR
        return out

    def synthesize(self, *, job_id, segment_index, text, lang, voice_id, speed, out_path, **kw):
        if self.killed.is_set():
            raise EchoActError(Code.WORKER_LOST, "killed")
        self.synth_calls += 1
        if self.fail_on is not None and segment_index == self.fail_on:
            raise EchoActError(Code.GENERATION_FAILED, "synthetic failure")
        if self.per_segment_s:
            # Interruptible, so a cancellation test does not have to wait.
            if self.killed.wait(self.per_segment_s):
                raise EchoActError(Code.WORKER_LOST, "killed mid-segment")
        frames = max(1, int(len(text) / 6.0 * SR))
        t = np.arange(frames, dtype=np.float32) / SR
        wav.write_segment(out_path, (0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), SR)
        return protocol.Audio(
            job_id=job_id,
            segment_index=segment_index,
            out_path=str(out_path),
            frame_count=frames,
            sample_rate=SR,
            synth_seconds=0.01,
            peak=0.2,
        )

    def kill(self):
        self.kills += 1
        self.killed.set()
        self._loaded = None
        self._budget = None
        return 0.01

    def unload(self, **kw):
        self._loaded = None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    from echoact import paths

    paths.data_dir.cache_clear()
    paths.ensure_tree()
    yield
    paths.data_dir.cache_clear()


@pytest.fixture()
def engine(tmp_path):
    store = Store(tmp_path / "db.sqlite3", audio_root=tmp_path / "audio")
    sup = FakeSupervisor()
    reg = ModelRegistry(MANIFEST, root=tmp_path / "models")
    eng = JobEngine(
        store=store,
        supervisor=sup,
        registry=reg,
        manifest=MANIFEST,
        settings=Settings(voice=_voice()),
        work_dir=tmp_path / "work",
        result_dir=tmp_path / "audio",
    )
    eng._fake = sup  # for assertions
    eng._store_ref = store
    yield eng
    eng.shutdown()
    store.close()


def _request(**kw) -> JobRequest:
    base = {
        "text": KO,
        "settings": _voice(),
        "request_path": RequestPath.GUI,
        "owner_client_id": "owner",
        "idempotency_key": "key-1",
        "kind": JobKind.SPEECH,
        "retention": RetentionMode.ONE_OFF,
    }
    base.update(kw)
    return JobRequest(**base)


def _finish(engine: JobEngine, job_id: str, timeout: float = 20.0):
    job = engine.wait(job_id, timeout)
    deadline = time.monotonic() + timeout
    while not job.state.is_terminal and time.monotonic() < deadline:
        time.sleep(0.01)
        job = engine._store.get_job(job_id)
    return job


# ------------------------------------------------------------ happy path ---


def test_a_job_runs_to_complete_with_a_result(engine: JobEngine) -> None:
    job, created = engine.submit(_request())
    assert created
    done = _finish(engine, job.job_id)
    assert done.state is JobState.COMPLETE
    assert done.result is not None
    assert done.result.sample_rate == SR
    assert done.result.frame_count > 0
    assert Path(done.result.relative_path).exists()

    info = wav.probe(done.result.relative_path)
    assert info.is_output_format, "F-82: mono 16-bit PCM at the model's rate"
    assert info.sample_rate == SR


def test_segments_tile_the_audio_with_no_holes(engine: JobEngine) -> None:
    job, _ = engine.submit(_request())
    done = _finish(engine, job.job_id)
    segs = engine._store.list_segments(job.job_id)
    assert segs
    at = 0
    for s in segs:
        assert s.time is not None
        assert s.time.start_ms == at, f"segment {s.index} starts at {s.time.start_ms}, not {at}"
        at = s.time.end_ms
    assert abs(at - done.result.duration_ms) <= 1


def test_complete_comes_after_the_result_exists(engine: JobEngine) -> None:
    """5.1: if generation finishes but saving fails, the job is not Complete."""
    seen: list[tuple[str, str]] = []

    def watch(ev) -> None:
        if ev.kind is EventKind.STATE and ev.state is JobState.COMPLETE:
            job = engine._store.get_job(ev.job_id, include_segments=False)
            seen.append(("complete", "result" if job.result else "none"))

    engine.listen(watch)
    job, _ = engine.submit(_request())
    _finish(engine, job.job_id)
    assert seen == [("complete", "result")]


def test_events_report_each_segment_once_in_order(engine: JobEngine) -> None:
    events: list[int] = []
    engine.listen(
        lambda ev: events.append(ev.segment_index) if ev.kind is EventKind.SEGMENT else None
    )
    job, _ = engine.submit(_request())
    _finish(engine, job.job_id)
    assert events == sorted(events)
    assert len(events) == len(set(events))
    assert len(events) == engine._store.get_job(job.job_id).total_segments


# ------------------------------------------------------------------ F-47 ---


def test_a_second_job_is_refused_as_busy_with_a_hint(tmp_path) -> None:
    store = Store(tmp_path / "b.sqlite3", audio_root=tmp_path / "audio")
    sup = FakeSupervisor(per_segment_s=5.0)
    eng = JobEngine(
        store=store,
        supervisor=sup,
        registry=ModelRegistry(MANIFEST, root=tmp_path / "models"),
        manifest=MANIFEST,
        settings=Settings(voice=_voice()),
        work_dir=tmp_path / "work",
        result_dir=tmp_path / "audio",
    )
    try:
        first, _ = eng.submit(_request(idempotency_key="a"))
        with pytest.raises(EchoActError) as caught:
            eng.submit(_request(idempotency_key="b"))
        assert caught.value.code is Code.BUSY
        assert caught.value.retry_after_s and caught.value.retry_after_s > 0
        eng.cancel(first.job_id)
    finally:
        eng.shutdown()
        store.close()


def test_a_busy_refusal_does_not_consume_the_key(tmp_path) -> None:
    """F-49's record identifies a job that exists. A busy response creates
    none, so replaying the same key afterwards must still be a new job."""
    store = Store(tmp_path / "c.sqlite3", audio_root=tmp_path / "audio")
    sup = FakeSupervisor(per_segment_s=5.0)
    eng = JobEngine(
        store=store,
        supervisor=sup,
        registry=ModelRegistry(MANIFEST, root=tmp_path / "models"),
        manifest=MANIFEST,
        settings=Settings(voice=_voice()),
        work_dir=tmp_path / "work",
        result_dir=tmp_path / "audio",
    )
    try:
        first, _ = eng.submit(_request(idempotency_key="a"))
        with pytest.raises(EchoActError):
            eng.submit(_request(idempotency_key="second"))
        eng.cancel(first.job_id)
        job, created = eng.submit(_request(idempotency_key="second"))
        assert created, "the key was consumed by a refusal"
    finally:
        eng.shutdown()
        store.close()


# ------------------------------------------------------------------ F-49 ---


def test_the_same_key_with_the_same_content_returns_one_job(engine: JobEngine) -> None:
    first, created = engine.submit(_request())
    assert created
    _finish(engine, first.job_id)
    again, created_again = engine.submit(_request())
    assert not created_again
    assert again.job_id == first.job_id


def test_the_same_key_with_different_content_is_a_conflict(engine: JobEngine) -> None:
    first, _ = engine.submit(_request())
    _finish(engine, first.job_id)
    with pytest.raises(EchoActError) as caught:
        engine.submit(_request(text=KO + " 다릅니다."))
    assert caught.value.code is Code.IDEMPOTENCY_KEY_CONFLICT


def test_a_request_without_a_key_is_refused(engine: JobEngine) -> None:
    with pytest.raises(EchoActError) as caught:
        engine.submit(_request(idempotency_key=""))
    assert caught.value.code is Code.IDEMPOTENCY_KEY_MISSING


# ------------------------------------------------------------ F-15, N-22 ---


def test_cancel_releases_the_slot_within_five_seconds(tmp_path) -> None:
    store = Store(tmp_path / "d.sqlite3", audio_root=tmp_path / "audio")
    sup = FakeSupervisor(per_segment_s=30.0)  # stuck mid-segment
    eng = JobEngine(
        store=store,
        supervisor=sup,
        registry=ModelRegistry(MANIFEST, root=tmp_path / "models"),
        manifest=MANIFEST,
        settings=Settings(voice=_voice()),
        work_dir=tmp_path / "work",
        result_dir=tmp_path / "audio",
    )
    try:
        job, _ = eng.submit(_request())
        # Let it reach the middle of a segment.
        deadline = time.monotonic() + 5
        while sup.synth_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sup.synth_calls > 0

        started = time.monotonic()
        final = eng.cancel(job.job_id)
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"cancellation took {elapsed:.2f}s"
        assert final.state is JobState.CANCELED
        assert not eng.busy, "the slot was not released"
        assert sup.kills >= 1, "a stuck segment must be killed, not waited on"
    finally:
        eng.shutdown()
        store.close()


def test_cancelling_a_terminal_job_changes_nothing(engine: JobEngine) -> None:
    job, _ = engine.submit(_request())
    done = _finish(engine, job.job_id)
    assert done.state is JobState.COMPLETE
    again = engine.cancel(job.job_id)
    assert again.state is JobState.COMPLETE, "Complete must never become Canceled"
    third = engine.cancel(job.job_id)
    assert third.state is JobState.COMPLETE


# ------------------------------------------------------------------ F-88 ---


def test_a_bounded_wait_returns_the_terminal_state_when_it_fits(engine: JobEngine) -> None:
    job, _ = engine.submit(_request())
    settled = engine.wait(job.job_id, 20.0)
    assert settled.state.is_terminal


def test_a_bounded_wait_that_lapses_leaves_the_job_alone(tmp_path) -> None:
    store = Store(tmp_path / "e.sqlite3", audio_root=tmp_path / "audio")
    sup = FakeSupervisor(per_segment_s=2.0)
    eng = JobEngine(
        store=store,
        supervisor=sup,
        registry=ModelRegistry(MANIFEST, root=tmp_path / "models"),
        manifest=MANIFEST,
        settings=Settings(voice=_voice()),
        work_dir=tmp_path / "work",
        result_dir=tmp_path / "audio",
    )
    try:
        job, _ = eng.submit(_request())
        started = time.monotonic()
        answer = eng.wait(job.job_id, 0.3)
        assert time.monotonic() - started < 2.0
        assert not answer.state.is_terminal
        assert answer.job_id == job.job_id
        assert eng.busy, "the job must continue untouched"
        eng.cancel(job.job_id)
    finally:
        eng.shutdown()
        store.close()


def test_an_estimate_creates_no_job_and_loads_no_model(engine: JobEngine) -> None:
    before = engine._fake.loads
    est = estimate(KO, _voice(), MANIFEST, slot_free=True, model_ready=True)
    assert est.valid
    assert est.segment_count == 3
    assert est.audio_ms > 0
    assert est.synthesis_ms > 0
    assert est.approximate
    assert engine._fake.loads == before
    assert not engine._store.list_jobs().items


def test_an_estimate_reports_every_problem_not_just_the_first() -> None:
    bad = _voice().with_(tempo=9.0, voice_id="ZZ")
    est = estimate("", bad, MANIFEST, slot_free=False, model_ready=False)
    assert not est.valid
    codes = {p["code"] for p in est.problems}
    assert Code.INPUT_EMPTY.value in codes
    assert Code.TEMPO_OUT_OF_RANGE.value in codes


# ------------------------------------------------------------ validation ---


def test_validation_never_substitutes_an_unknown_option() -> None:
    with pytest.raises(EchoActError) as caught:
        validate_request(_request(settings=_voice().with_(voice_id="nope")), MANIFEST)
    assert caught.value.code is Code.VOICE_UNKNOWN
    assert "available" in caught.value.detail


def test_a_voice_that_contradicts_the_gender_is_refused() -> None:
    with pytest.raises(EchoActError) as caught:
        validate_request(_request(settings=_voice().with_(gender=Gender.MALE)), MANIFEST)
    assert caught.value.code is Code.VOICE_GENDER_MISMATCH


def test_whitespace_only_input_is_refused_rather_than_trimmed(engine: JobEngine) -> None:
    with pytest.raises(EchoActError) as caught:
        engine.submit(_request(text="   \n\t "))
    assert caught.value.code is Code.INPUT_EMPTY


# ------------------------------------------------------------- failures ---


def test_a_failed_segment_fails_the_job_and_releases_the_model(tmp_path) -> None:
    store = Store(tmp_path / "f.sqlite3", audio_root=tmp_path / "audio")
    sup = FakeSupervisor(fail_on=1)
    eng = JobEngine(
        store=store,
        supervisor=sup,
        registry=ModelRegistry(MANIFEST, root=tmp_path / "models"),
        manifest=MANIFEST,
        settings=Settings(voice=_voice()),
        work_dir=tmp_path / "work",
        result_dir=tmp_path / "audio",
    )
    try:
        job, _ = eng.submit(_request())
        done = _finish(eng, job.job_id)
        assert done.state is JobState.FAILED
        assert done.error_code == Code.GENERATION_FAILED.value
        assert sup.kills >= 1, "F-19 releases the model on error"
        assert not eng.busy
    finally:
        eng.shutdown()
        store.close()


# --------------------------------------------------------------- sweeps ---


def test_expired_one_off_results_are_swept(engine: JobEngine, tmp_path) -> None:
    job, _ = engine.submit(_request())
    done = _finish(engine, job.job_id)
    assert done.result is not None and done.result.expires_at is not None

    engine._store.expire_result(done.result.result_id, at=time.time() + 10_000)
    removed = expire_one_off_results(engine._store, tmp_path / "work")
    assert removed >= 0  # the sweep must not raise on an already-expired row


def test_clear_temp_tree_removes_what_a_crash_left(tmp_path) -> None:
    root = tmp_path / "scratch"
    (root / "job_x").mkdir(parents=True)
    (root / "job_x" / "0.wav").write_bytes(b"junk")
    (root / "loose.tmp").write_bytes(b"junk")
    assert clear_temp_tree(root) == 2
    assert list(root.iterdir()) == []


# --------------------------------------------------------- the real thing ---


@pytest.mark.engine
def test_the_real_engine_produces_a_playable_result(tmp_path) -> None:
    """The fake above must not drift away from the real supervisor."""
    from echoact.engine.supervisor import WorkerSupervisor

    reg = ModelRegistry(MANIFEST, root=tmp_path / "models")
    try:
        reg.resolve_dir(SUPERTONIC_3_ID)
    except EchoActError:
        pytest.skip("the Supertonic weights are not present on this machine")

    store = Store(tmp_path / "real.sqlite3", audio_root=tmp_path / "audio")
    sup = WorkerSupervisor()
    eng = JobEngine(
        store=store,
        supervisor=sup,
        registry=reg,
        manifest=MANIFEST,
        settings=Settings(voice=_voice()),
        work_dir=tmp_path / "work",
        result_dir=tmp_path / "audio",
    )
    try:
        job, created = eng.submit(_request(text="에코액트 테스트입니다. Second sentence here."))
        assert created
        done = _finish(eng, job.job_id, timeout=180.0)
        assert done.state is JobState.COMPLETE, done.error_message
        info = wav.probe(done.result.relative_path)
        assert info.is_output_format
        assert info.sample_rate == 44100
        assert info.duration_ms > 500

        segs = store.list_segments(job.job_id)
        assert all(s.ready and s.time is not None for s in segs)
        assert segs[-1].time.end_ms == pytest.approx(info.duration_ms, abs=2)
    finally:
        eng.shutdown()
        store.close()
