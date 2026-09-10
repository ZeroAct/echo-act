"""The library store: F-38 to F-45, N-14, N-16, and the entities in 4.2."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from echoact import paths
from echoact.db.store import ResultIntegrity as Integrity
from echoact.db.store import Store, request_match_digest
from echoact.domain import (
    Budget,
    Capability,
    Gender,
    Job,
    JobKind,
    JobState,
    Language,
    RequestPath,
    Result,
    RetentionMode,
    Segment,
    SpeakingStyle,
    TextRange,
    TimeRange,
    VoiceSettings,
)
from echoact.errors import Code, EchoActError
from echoact.policy import LIST_PAGE_MAX, RETENTION_DEFAULT_BYTES

KOREAN = "안녕하세요 반갑습니다"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Never the real user data directory.  ``paths.data_dir`` is cached, so
    the cache is cleared on the way in and on the way out."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path))
    paths.data_dir.cache_clear()
    paths.ensure_tree()
    yield tmp_path
    paths.data_dir.cache_clear()


@pytest.fixture
def store(data_dir):
    s = Store()
    yield s
    s.close()


def _settings(model_id: str = "supertonic-3", voice: str = "F1") -> VoiceSettings:
    return VoiceSettings(
        model_id=model_id,
        language=Language.KO,
        gender=Gender.FEMALE,
        voice_id=voice,
        style=SpeakingStyle.NATURAL,
        tempo=1.0,
    )


def _job(
    job_id: str = "job_a",
    *,
    state: JobState = JobState.ACCEPTED,
    retention: RetentionMode = RetentionMode.RETAINED,
    text: str = KOREAN,
    model_id: str = "supertonic-3",
    created_at: float = 1_000.0,
    owner: str = "cli_owner",
    path: RequestPath = RequestPath.GUI,
) -> Job:
    return Job(
        job_id=job_id,
        kind=JobKind.SPEECH,
        request_path=path,
        owner_client_id=owner,
        state=state,
        source_text=text,
        settings=_settings(model_id),
        budget=Budget(cpu_percent=20, memory_bytes=2 << 30, intra_op_threads=2),
        retention=retention,
        created_at=created_at,
    )


def _result(job_id: str, *, digest: str = "0" * 64, size: int = 44, path: str = "a.wav") -> Result:
    return Result(
        result_id="",
        job_id=job_id,
        sample_rate=44_100,
        channels=1,
        sample_width_bits=16,
        frame_count=44_100,
        byte_size=size,
        digest=digest,
        created_at=2_000.0,
        expires_at=None,
        relative_path=path,
    )


# ======================================================================
# Documents (F-38, F-40)
# ======================================================================


def test_a_saved_document_is_still_there_after_the_store_is_reopened(data_dir):
    first = Store()
    saved = first.save_document("제목", KOREAN, at=10.0)
    first.close()

    second = Store()
    try:
        again = second.get_document(saved.document_id)
        assert again.title == "제목"
        assert again.body == KOREAN
        assert again.created_at == 10.0
    finally:
        second.close()


def test_a_document_list_leaves_the_body_in_the_database(store):
    store.save_document("t", "x" * 50_000)
    summary = store.list_documents().items[0]
    assert summary.body_codepoints == 50_000
    assert not hasattr(summary, "body")


def test_editing_a_document_bumps_its_version_and_leaves_a_job_snapshot_alone(store):
    doc = store.save_document("draft", "original body", at=1.0)
    store.create_job(_job(text="original body"))

    edited = store.update_document(doc.document_id, body="rewritten", at=2.0)

    assert (edited.version, edited.modified_at) == (2, 2.0)
    assert store.get_document(doc.document_id).body == "rewritten"
    assert store.get_job_text("job_a") == "original body"


def test_search_finds_a_korean_substring_inside_a_word(store):
    store.save_document("회의록", KOREAN)
    store.save_document("other", "nothing relevant here")

    found = store.search_documents("녕하세")

    assert [d.title for d in found.items] == ["회의록"]


def test_search_answers_a_query_too_short_for_the_index_instead_of_returning_nothing(store):
    store.save_document("회의록", KOREAN)

    found = store.search_documents("녕")

    assert [d.title for d in found.items] == ["회의록"]


def test_search_treats_index_operators_in_the_query_as_ordinary_characters(store):
    store.save_document("report", 'a "quoted" AND thing')

    # N-18: the user's words are data.  Unquoted these are FTS5 syntax.
    assert store.search_documents('"quoted"').total == 1
    assert store.search_documents("AND thing").total == 1
    assert store.search_documents("absent OR missing").total == 0


def test_deleting_a_document_leaves_a_jobs_own_snapshot_untouched(store):
    doc = store.save_document("draft", "shared wording")
    store.create_job(_job(text="shared wording"))

    store.delete_document(doc.document_id)

    assert store.get_job_text("job_a") == "shared wording"
    with pytest.raises(EchoActError) as exc:
        store.get_document(doc.document_id)
    assert exc.value.code is Code.NOT_FOUND


# ======================================================================
# History (F-39, F-40, F-41, F-56)
# ======================================================================


def test_history_filters_by_date_model_and_state(store):
    store.create_job(_job("job_1", created_at=100.0, model_id="supertonic-3"))
    store.create_job(_job("job_2", created_at=200.0, model_id="other-model"))
    store.create_job(_job("job_3", created_at=300.0, model_id="supertonic-3"))
    store.update_job_state("job_3", JobState.GENERATING, at=310.0)

    by_date = store.list_jobs(created_from=150.0, created_to=250.0)
    by_model = store.list_jobs(model_id="supertonic-3")
    by_state = store.list_jobs(states=[JobState.GENERATING])

    assert [j.job_id for j in by_date.items] == ["job_2"]
    assert {j.job_id for j in by_model.items} == {"job_1", "job_3"}
    assert [j.job_id for j in by_state.items] == ["job_3"]


def test_history_comes_back_page_by_page_and_never_larger_than_the_ceiling(store):
    for i in range(5):
        store.create_job(_job(f"job_{i}", created_at=float(i)))

    page = store.list_jobs(limit=2, offset=0)
    second = store.list_jobs(limit=2, offset=2)

    assert [j.job_id for j in page.items] == ["job_4", "job_3"]
    assert (page.total, page.has_more) == (5, True)
    assert [j.job_id for j in second.items] == ["job_2", "job_1"]
    assert store.list_jobs(limit=10_000).limit == LIST_PAGE_MAX


def test_a_history_list_never_opens_an_audio_file(store):
    store.create_job(_job("job_1"))
    store.attach_result(_result("job_1", path="missing.wav"))

    def forbidden(*args, **kwargs):
        raise AssertionError("F-40: a list must be answerable without reading audio")

    original = Path.open
    Path.open = forbidden  # type: ignore[method-assign]
    try:
        summary = store.list_jobs().items[0]
    finally:
        Path.open = original  # type: ignore[method-assign]

    assert summary.has_result is True
    assert summary.audio_duration_ms == 1000
    assert not (store.audio_root / "missing.wav").exists()


def test_a_history_summary_withholds_the_source_text_until_it_is_asked_for(store):
    store.create_job(_job("job_1", text="a private sentence"))

    assert store.list_jobs().items[0].source_text is None
    assert store.list_jobs(include_source_text=True).items[0].source_text == "a private sentence"
    assert store.get_job_text("job_1") == "a private sentence"


def test_a_one_off_snapshot_can_be_cleared_while_the_job_and_its_reason_remain(store):
    store.create_job(_job("job_1", retention=RetentionMode.ONE_OFF))
    store.update_job_state("job_1", JobState.GENERATING, at=5.0)
    store.record_job_error("job_1", Code.GENERATION_FAILED, at=6.0)

    store.clear_job_source_text("job_1")

    assert store.get_job_text("job_1") is None
    summary = store.list_jobs().items[0]
    assert (summary.state, summary.error_code) == (JobState.FAILED, "GENERATION_FAILED")


def test_a_complete_job_is_never_overwritten_by_a_late_cancellation(store):
    store.create_job(_job("job_1"))
    store.update_job_state("job_1", JobState.GENERATING, at=2.0)
    store.update_job_state("job_1", JobState.COMPLETE, at=3.0)

    with pytest.raises(AssertionError):
        store.update_job_state("job_1", JobState.CANCELED, at=4.0)

    assert store.get_job("job_1").state is JobState.COMPLETE


def test_repeating_the_state_a_job_already_holds_changes_nothing(store):
    store.create_job(_job("job_1"))
    store.update_job_state("job_1", JobState.CANCELING, at=2.0)
    store.update_job_state("job_1", JobState.CANCELED, at=3.0)

    assert store.update_job_state("job_1", JobState.CANCELED, at=9.0) is JobState.CANCELED
    assert store.get_job("job_1").ended_at == 3.0


def test_a_recorded_error_keeps_its_code_for_later_diagnosis(store):
    store.create_job(_job("job_1"))
    store.update_job_state("job_1", JobState.GENERATING, at=2.0)

    store.record_job_error("job_1", Code.OUT_OF_MEMORY, at=3.0)

    job = store.get_job("job_1")
    assert (job.state, job.error_code, job.ended_at) == (JobState.FAILED, "OUT_OF_MEMORY", 3.0)
    assert job.error_message == EchoActError(Code.OUT_OF_MEMORY).message


def test_regenerating_is_a_new_job_and_does_not_disturb_the_first_result(store):
    store.create_job(_job("job_1", created_at=100.0))
    first = store.attach_result(_result("job_1", digest="a" * 64, path="one.wav"))
    store.update_job_state("job_1", JobState.GENERATING, at=101.0)
    store.update_job_state("job_1", JobState.COMPLETE, at=102.0)

    store.create_job(_job("job_2", created_at=200.0))
    store.attach_result(_result("job_2", digest="b" * 64, path="two.wav"))

    assert store.get_result(first.result_id).digest == "a" * 64
    assert store.get_result_for_job("job_1").result_id == first.result_id
    assert store.get_result_for_job("job_2").relative_path == "two.wav"
    assert store.list_jobs().total == 2


def test_deleting_a_running_job_is_refused_and_the_refusal_is_retryable(store):
    store.create_job(_job("job_1"))
    store.update_job_state("job_1", JobState.GENERATING, at=2.0)

    with pytest.raises(EchoActError) as exc:
        store.delete_job("job_1")

    assert exc.value.code is Code.DELETE_BLOCKED_IN_USE
    assert exc.value.retryable is True
    assert exc.value.retry_after_s is not None
    assert store.list_jobs().total == 1


def test_deleting_a_job_hands_back_its_audio_paths_rather_than_removing_files(store):
    store.create_job(_job("job_1"))
    store.insert_segments(
        "job_1",
        [Segment(index=0, source=TextRange(0, 3), spoken_text="abc", language="ko")],
    )
    store.mark_segment_ready("job_1", 0, time=TimeRange(0, 500), audio_path="seg0.wav")
    store.attach_result(_result("job_1", path="full.wav"))
    (store.audio_root / "full.wav").write_bytes(b"x")

    deleted = store.delete_job("job_1", force=True)

    assert set(deleted.audio_paths) == {"full.wav", "seg0.wav"}
    assert (store.audio_root / "full.wav").exists()
    assert store.list_jobs().total == 0


def test_deleting_all_history_keeps_the_document_library(store):
    store.save_document("kept", "body")
    store.create_job(_job("job_1", state=JobState.COMPLETE))

    removed = store.delete_all_history()

    assert removed.jobs == 1
    assert store.list_jobs().total == 0
    assert store.list_documents().total == 1


def test_a_deletion_preview_reports_the_scope_and_what_blocks_it(store):
    doc = store.save_document("t", "body")
    store.create_job(_job("job_1"))
    store.update_job_state("job_1", JobState.GENERATING, at=2.0)
    store.insert_segments(
        "job_1",
        [Segment(index=0, source=TextRange(0, 3), spoken_text="abc", language="ko")],
    )
    store.attach_result(_result("job_1", size=1234))

    scope = store.preview_deletion(job_ids=["job_1"], document_ids=[doc.document_id])

    assert (scope.jobs, scope.documents, scope.segments, scope.results) == (1, 1, 1, 1)
    assert scope.audio_bytes == 1234
    assert scope.blocked_job_ids == ("job_1",)


# ======================================================================
# Segments (4.2, F-55)
# ======================================================================


def test_segment_offsets_round_trip_as_half_open_code_point_ranges(store):
    store.create_job(_job("job_1", text=KOREAN))
    written = store.insert_segments(
        "job_1",
        [
            Segment(index=0, source=TextRange(0, 5), spoken_text="안녕하세요", language="ko"),
            Segment(index=1, source=TextRange(6, 11), spoken_text="반갑습니다", language="ko"),
        ],
    )

    read_back = store.list_segments("job_1")

    assert [s.source for s in read_back] == [TextRange(0, 5), TextRange(6, 11)]
    assert KOREAN[read_back[0].source.start : read_back[0].source.end] == "안녕하세요"
    assert all(s.segment_id for s in written)
    assert [s.segment_id for s in read_back] == [s.segment_id for s in written]


def test_a_segment_range_that_ends_before_it_starts_is_refused_by_the_schema(store, data_dir):
    store.create_job(_job("job_1"))
    raw = sqlite3.connect(paths.db_path())
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO segments (segment_id, job_id, seq,"
                " source_start_codepoint_inclusive, source_end_codepoint_exclusive,"
                " spoken_text, language) VALUES ('seg_x', 'job_1', 0, 9, 4, 'x', 'ko')"
            )
    finally:
        raw.close()


def test_marking_the_same_segment_ready_twice_does_not_inflate_progress(store):
    store.create_job(_job("job_1"))
    store.insert_segments(
        "job_1",
        [
            Segment(index=0, source=TextRange(0, 3), spoken_text="abc", language="ko"),
            Segment(index=1, source=TextRange(3, 6), spoken_text="def", language="ko"),
        ],
    )

    store.mark_segment_ready("job_1", 0, time=TimeRange(0, 500), frame_count=10)
    store.mark_segment_ready("job_1", 0, time=TimeRange(0, 500), frame_count=10)

    summary = store.list_jobs().items[0]
    assert (summary.generated_segments, summary.total_segments) == (1, 2)


def test_listing_ready_segments_excludes_one_that_has_no_audio_yet(store):
    store.create_job(_job("job_1"))
    store.insert_segments(
        "job_1",
        [
            Segment(index=0, source=TextRange(0, 3), spoken_text="abc", language="ko"),
            Segment(index=1, source=TextRange(3, 6), spoken_text="def", language="ko"),
        ],
    )
    store.mark_segment_ready("job_1", 1, time=TimeRange(0, 500), audio_path="s1.wav")

    ready = store.list_segments("job_1", ready_only=True)

    assert [s.index for s in ready] == [1]
    assert ready[0].time == TimeRange(0, 500)


def test_a_segment_cannot_outlive_the_job_it_belongs_to(store):
    store.create_job(_job("job_1", state=JobState.COMPLETE))
    store.insert_segments(
        "job_1",
        [Segment(index=0, source=TextRange(0, 3), spoken_text="abc", language="ko")],
    )

    store.delete_job("job_1")

    assert store.list_segments("job_1") == []


def test_a_segment_for_a_job_that_does_not_exist_is_rejected(store):
    with pytest.raises(EchoActError) as exc:
        store.insert_segments(
            "job_missing",
            [Segment(index=0, source=TextRange(0, 3), spoken_text="abc", language="ko")],
        )
    assert exc.value.code is Code.INTERNAL
    assert exc.value.retryable is False


# ======================================================================
# Results (4.2, F-45)
# ======================================================================


def test_an_expired_result_keeps_its_job_and_reports_the_expiry(store):
    store.create_job(_job("job_1", retention=RetentionMode.ONE_OFF))
    result = store.attach_result(_result("job_1"))

    store.expire_result(result.result_id, at=5_000.0)

    summary = store.list_jobs().items[0]
    assert summary.has_result is True
    assert summary.result_expired is True
    assert store.get_result(result.result_id).expires_at == 5_000.0
    assert [r.result_id for r in store.due_results(at=5_001.0)] == [result.result_id]


def test_verification_reports_a_truncated_file_as_corrupt(store):
    store.create_job(_job("job_1"))
    payload = b"RIFF" + b"\x00" * 40
    result = store.attach_result(_result("job_1", size=len(payload), path="full.wav"))
    (store.audio_root / "full.wav").write_bytes(payload[:10])

    assert store.verify_result(result.result_id) is Integrity.CORRUPT
    assert store.result_integrity(result.result_id) is Integrity.CORRUPT


def test_deep_verification_detects_bytes_that_changed_without_changing_the_size(store):
    import hashlib

    good = b"RIFF" + b"\x01" * 40
    store.create_job(_job("job_1"))
    result = store.attach_result(
        _result("job_1", size=len(good), digest=hashlib.sha256(good).hexdigest(), path="f.wav")
    )
    (store.audio_root / "f.wav").write_bytes(good)
    assert store.verify_result(result.result_id) is Integrity.OK

    (store.audio_root / "f.wav").write_bytes(b"RIFF" + b"\x02" * 40)

    assert store.verify_result(result.result_id, deep=False) is Integrity.OK
    assert store.verify_result(result.result_id, deep=True) is Integrity.CORRUPT


def test_a_stored_path_that_climbs_out_of_the_audio_directory_is_treated_as_missing(store):
    outside = store.audio_root.parent / "elsewhere.wav"
    outside.write_bytes(b"x" * 44)
    store.create_job(_job("job_1"))
    result = store.attach_result(_result("job_1", size=44, path="../elsewhere.wav"))

    assert store.verify_result(result.result_id) is Integrity.MISSING
    assert outside.exists()


# ======================================================================
# Re-request records (F-49, 4.1, 4.2)
# ======================================================================


def test_the_same_key_with_the_same_content_returns_the_first_job(store):
    digest = request_match_digest(KOREAN, "supertonic-3")

    first, created = store.claim_job(
        _job("job_1"), client_id="cli_1", key="k", request_digest=digest, at=10.0
    )
    second, created_again = store.claim_job(
        _job("job_2"), client_id="cli_1", key="k", request_digest=digest, at=11.0
    )

    assert (created, created_again) == (True, False)
    assert second.job_id == first.job_id == "job_1"
    assert store.list_jobs().total == 1


def test_the_same_key_with_different_content_is_a_conflict(store):
    store.claim_job(
        _job("job_1"),
        client_id="cli_1",
        key="k",
        request_digest=request_match_digest("one"),
        at=10.0,
    )

    with pytest.raises(EchoActError) as exc:
        store.claim_job(
            _job("job_2"),
            client_id="cli_1",
            key="k",
            request_digest=request_match_digest("two"),
            at=11.0,
        )

    assert exc.value.code is Code.IDEMPOTENCY_KEY_CONFLICT
    assert exc.value.retryable is False
    assert store.list_jobs().total == 1


def test_a_key_is_scoped_to_the_client_that_used_it(store):
    digest = request_match_digest(KOREAN)
    store.claim_job(_job("job_1"), client_id="cli_1", key="k", request_digest=digest, at=10.0)

    _, created = store.claim_job(
        _job("job_2", owner="cli_2"), client_id="cli_2", key="k", request_digest=digest, at=11.0
    )

    assert created is True
    assert store.list_jobs().total == 2


def test_a_re_request_record_holds_no_source_text(store, data_dir):
    secret = "차마 저장하면 안 되는 문장"
    store.claim_job(
        _job("job_1", retention=RetentionMode.ONE_OFF, text=secret),
        client_id="cli_1",
        key="k",
        request_digest=request_match_digest(secret),
        at=10.0,
    )
    store.clear_job_source_text("job_1")

    raw = sqlite3.connect(paths.db_path())
    try:
        row = raw.execute("SELECT * FROM idempotency").fetchone()
    finally:
        raw.close()

    assert row is not None
    assert all(secret not in str(value) for value in row)


def test_a_re_request_record_survives_a_restart_within_its_hour(data_dir):
    digest = request_match_digest(KOREAN)
    first = Store()
    first.claim_job(_job("job_1"), client_id="cli_1", key="k", request_digest=digest, at=10.0)
    first.close()

    second = Store()
    try:
        found = second.lookup_idempotency("cli_1", "k", at=10.0 + 3_599.0)
        assert found is not None
        assert found.job_id == "job_1"
        assert second.lookup_idempotency("cli_1", "k", at=10.0 + 3_601.0) is None
    finally:
        second.close()


def test_an_expired_key_may_start_a_new_request(store):
    digest = request_match_digest(KOREAN)
    store.claim_job(_job("job_1"), client_id="cli_1", key="k", request_digest=digest, at=10.0)

    later = 10.0 + 7_200.0
    fresh, created = store.claim_job(
        _job("job_2"), client_id="cli_1", key="k", request_digest=digest, at=later
    )

    assert (created, fresh.job_id) == (True, "job_2")
    assert store.purge_expired_idempotency(at=later + 1.0) == 0


def test_purging_removes_only_records_whose_hour_has_passed(store):
    digest = request_match_digest(KOREAN)
    store.claim_job(_job("job_1"), client_id="cli_1", key="old", request_digest=digest, at=10.0)
    store.claim_job(_job("job_2"), client_id="cli_1", key="new", request_digest=digest, at=9_000.0)

    assert store.purge_expired_idempotency(at=9_100.0) == 1
    assert store.lookup_idempotency("cli_1", "new", at=9_100.0) is not None


# ======================================================================
# Retention accounting (N-16, 4.1)
# ======================================================================


def test_storage_usage_counts_documents_history_and_audio_against_the_ceiling(store):
    store.save_document("t", "12345")
    store.create_job(_job("job_1", text="1234567890"))
    store.attach_result(_result("job_1", size=1_000))

    usage = store.storage_usage()

    assert usage.document_bytes == len("t") + len("12345")
    assert usage.job_text_bytes == 10
    assert usage.audio_bytes == 1_000
    assert usage.limit_bytes == RETENTION_DEFAULT_BYTES
    assert usage.total_bytes == usage.document_bytes + 10 + 1_000
    assert usage.remaining_bytes == RETENTION_DEFAULT_BYTES - usage.total_bytes


def test_a_save_that_would_exceed_the_ceiling_is_refused_and_deletes_nothing(data_dir):
    store = Store(retention_limit_bytes=120)
    try:
        kept = store.save_document("kept", "x" * 100)

        with pytest.raises(EchoActError) as exc:
            store.save_document("too big", "y" * 100)

        assert exc.value.code is Code.RETENTION_LIMIT_REACHED
        assert exc.value.retryable is False
        assert store.list_documents().total == 1
        assert store.get_document(kept.document_id).body == "x" * 100
    finally:
        store.close()


def test_a_retained_job_whose_audio_does_not_fit_fails_rather_than_evicting(data_dir):
    store = Store(retention_limit_bytes=1_000)
    try:
        store.create_job(_job("job_1", text="short"))

        with pytest.raises(EchoActError) as exc:
            store.attach_result(_result("job_1", size=10_000))

        assert exc.value.code is Code.RETENTION_LIMIT_REACHED
        assert store.get_result_for_job("job_1") is None
        assert store.get_job_text("job_1") == "short"
    finally:
        store.close()


def test_a_one_off_job_is_not_refused_by_the_retention_ceiling(data_dir):
    store = Store(retention_limit_bytes=1)
    try:
        store.create_job(_job("job_1", retention=RetentionMode.ONE_OFF, text="x" * 500))
        assert store.list_jobs().total == 1
    finally:
        store.close()


def test_lowering_the_limit_keeps_what_is_stored_and_blocks_what_is_new(store):
    kept = store.save_document("kept", "x" * 500)

    store.set_retention_limit(100)

    assert store.get_document(kept.document_id).body == "x" * 500
    assert store.retention_fits(1) is False
    with pytest.raises(EchoActError) as exc:
        store.save_document("new", "y")
    assert exc.value.code is Code.RETENTION_LIMIT_REACHED


# ======================================================================
# Abnormal termination (F-45)
# ======================================================================


def test_a_job_left_generating_becomes_interrupted_and_never_complete(data_dir):
    first = Store()
    first.create_job(_job("job_1"))
    first.update_job_state("job_1", JobState.GENERATING, at=2.0)
    first.close()  # stands in for the process being killed

    second = Store()
    try:
        report = second.reconcile_on_start(at=99.0)

        assert report.interrupted_job_ids == ("job_1",)
        job = second.get_job("job_1")
        assert job.state is JobState.INTERRUPTED
        assert job.ended_at == 99.0
    finally:
        second.close()


def test_a_job_left_canceling_reaches_a_terminal_state_too(store):
    store.create_job(_job("job_1"))
    store.update_job_state("job_1", JobState.CANCELING, at=2.0)

    report = store.reconcile_on_start(at=99.0)

    assert report.canceled_job_ids == ("job_1",)
    assert store.get_job("job_1").state is JobState.CANCELED


def test_reconciliation_leaves_finished_jobs_and_starts_nothing(store):
    store.create_job(_job("job_1", created_at=1.0))
    store.update_job_state("job_1", JobState.GENERATING, at=2.0)
    store.update_job_state("job_1", JobState.COMPLETE, at=3.0)
    store.create_job(_job("job_2", created_at=4.0, state=JobState.FAILED))

    store.reconcile_on_start(at=99.0)

    assert store.get_job("job_1").state is JobState.COMPLETE
    assert store.get_job("job_1").ended_at == 3.0
    assert store.get_job("job_2").state is JobState.FAILED
    assert store.list_jobs().total == 2


def test_reconciliation_marks_a_missing_result_so_the_gui_can_offer_a_remedy(store):
    store.create_job(_job("job_1", state=JobState.COMPLETE))
    result = store.attach_result(_result("job_1", path="gone.wav"))

    report = store.reconcile_on_start(at=99.0)

    assert report.missing_result_ids == (result.result_id,)
    assert store.result_integrity(result.result_id) is Integrity.MISSING
    assert store.list_jobs().items[0].result_integrity is Integrity.MISSING
    # F-45: the history entry itself remains, so the text and settings are
    # still there to start a new generation from.
    assert store.get_job_text("job_1") == KOREAN


def test_reconciliation_expires_a_one_off_result_left_by_the_previous_run(store):
    store.create_job(_job("job_1", retention=RetentionMode.ONE_OFF, state=JobState.COMPLETE))
    result = store.attach_result(_result("job_1"))

    report = store.reconcile_on_start(at=99.0)

    assert report.expired_result_ids == (result.result_id,)
    assert store.get_result(result.result_id).expires_at == 99.0
    assert store.list_jobs().items[0].result_expired is True


# ======================================================================
# Integrity and failure behaviour (N-14)
# ======================================================================


def test_a_lock_that_outlasts_the_timeout_is_refused_rather_than_waited_out(data_dir):
    store = Store(busy_timeout_s=0.05)
    blocker = sqlite3.connect(paths.db_path(), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(EchoActError) as exc:
            store.save_document("t", "b")
        elapsed = time.monotonic() - started

        assert exc.value.code is Code.DB_LOCKED
        assert exc.value.retryable is True
        assert exc.value.retry_after_s is not None
        assert elapsed < 2.0, "N-14 forbids waiting indefinitely on a lock"
        assert store.list_documents().total == 0, "and forbids reporting a save that did not happen"
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        store.close()


def test_a_save_that_fails_midway_leaves_previously_sound_data_intact(store):
    doc = store.save_document("kept", "sound body", at=1.0)

    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            conn.execute("UPDATE documents SET title = 'clobbered', body = ''")
            conn.execute("DELETE FROM documents WHERE 1 = 1")
            raise RuntimeError("the save failed here")

    survivor = store.get_document(doc.document_id)
    assert (survivor.title, survivor.body) == ("kept", "sound body")


def test_a_nested_unit_of_work_rolls_back_without_losing_the_outer_one(store):
    with store.transaction():
        store.save_document("outer", "kept", at=1.0)
        with pytest.raises(RuntimeError):
            with store.transaction() as conn:
                conn.execute("DELETE FROM documents")
                raise RuntimeError("inner failed")

    assert [d.title for d in store.list_documents().items] == ["outer"]


def test_the_database_runs_in_write_ahead_mode_with_a_bounded_timeout(store, data_dir):
    raw = sqlite3.connect(paths.db_path())
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        raw.close()
    assert 0 < store.busy_timeout_s < 60


def test_a_full_disk_is_reported_as_storage_full_and_not_as_a_lock():
    from echoact.db.migrations import translate_sqlite_error

    full = translate_sqlite_error(sqlite3.OperationalError("database or disk is full"))
    locked = translate_sqlite_error(sqlite3.OperationalError("database is locked"))

    assert full.code is Code.STORAGE_FULL
    assert full.retryable is False
    assert locked.code is Code.DB_LOCKED
    assert locked.retryable is True


def test_every_thread_gets_a_connection_of_its_own(store):
    failures: list[BaseException] = []

    def save(index: int) -> None:
        try:
            store.save_document(f"doc {index}", "body", at=float(index))
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert failures == []
    assert store.list_documents().total == 8


# ======================================================================
# Integration permissions (4.2, F-61, F-71)
# ======================================================================


def test_a_revoked_client_is_recorded_rather_than_forgotten(store):
    store.upsert_client("cli_1", "Editor", [Capability.GENERATE, Capability.READ_RESULTS], at=1.0)
    store.touch_client("cli_1", at=2.0)

    store.revoke_client("cli_1", at=3.0)

    client = store.get_client("cli_1")
    assert client is not None
    assert client.active is False
    assert client.revoked_at == 3.0
    assert client.last_seen_at == 2.0
    assert client.capabilities == frozenset({Capability.GENERATE, Capability.READ_RESULTS})


def test_a_client_record_carries_no_credential_field(store, data_dir):
    store.upsert_client("cli_1", "Editor", [Capability.GENERATE], at=1.0)
    raw = sqlite3.connect(paths.db_path())
    try:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(clients)")}
    finally:
        raw.close()

    assert not {c for c in columns if "token" in c or "secret" in c or "verifier" in c}
