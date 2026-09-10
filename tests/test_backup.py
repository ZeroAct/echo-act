"""Backup and restore: F-44, F-74, N-15, N-27, and 4.1's restore limits."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import pytest

from echoact import paths
from echoact.config.settings import Settings
from echoact.db import backup as bk
from echoact.db.backup import (
    BACKUP_FORMAT_VERSION,
    BACKUP_SUFFIX,
    BackupPhase,
    BackupScheduler,
    BackupSelection,
    CancelToken,
    RestoreGate,
    RestoreMode,
    RestorePhase,
    ScheduleReason,
    create_backup,
    inspect_backup,
    last_scheduled_run,
    prune_scheduled_backups,
    restore_backup,
    verify_backup,
)
from echoact.db.store import Store
from echoact.domain import (
    Budget,
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
from echoact.policy import (
    BACKUP_KEEP_SCHEDULED,
    BACKUP_RESTORE_MAX_BYTES,
    BACKUP_RESTORE_MAX_ITEMS,
    GB,
    SCHEDULED_BACKUP_DEFAULT,
)
from echoact.security.credentials import CREDENTIALS_FILENAME

KOREAN = "안녕하세요 반갑습니다"
OWNER = "cli_owner"
DAY = 24 * 60 * 60


# ======================================================================
# Fixtures and builders
# ======================================================================


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Never the real user data directory.  ``paths.data_dir`` is cached, so
    the cache is cleared on the way in and on the way out."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "appdata"))
    paths.data_dir.cache_clear()
    paths.ensure_tree()
    yield tmp_path
    paths.data_dir.cache_clear()


@pytest.fixture
def store(data_dir):
    s = Store()
    yield s
    s.close()


@pytest.fixture
def out(tmp_path):
    """Where the user chooses to put a backup: outside the data directory."""
    d = tmp_path / "chosen"
    d.mkdir()
    return d


def _settings(model_id: str = "supertonic-3") -> VoiceSettings:
    return VoiceSettings(
        model_id=model_id,
        language=Language.KO,
        gender=Gender.FEMALE,
        voice_id="F1",
        style=SpeakingStyle.NATURAL,
        tempo=1.0,
    )


def _job(
    job_id: str,
    *,
    state: JobState = JobState.COMPLETE,
    retention: RetentionMode = RetentionMode.RETAINED,
    text: str = KOREAN,
    owner: str = OWNER,
    path: RequestPath = RequestPath.GUI,
    created_at: float = 1_000.0,
) -> Job:
    return Job(
        job_id=job_id,
        kind=JobKind.SPEECH,
        request_path=path,
        owner_client_id=owner,
        state=state,
        source_text=text,
        settings=_settings(),
        budget=Budget(cpu_percent=20, memory_bytes=2 << 30, intra_op_threads=2),
        retention=retention,
        created_at=created_at,
    )


def _attach_audio(store: Store, job_id: str, payload: bytes = b"RIFFfake-wav-body") -> Result:
    """Write a real file under the audio root and record a result for it."""
    relative = f"{job_id}.wav"
    target = store.audio_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return store.attach_result(
        Result(
            result_id="",
            job_id=job_id,
            sample_rate=44_100,
            channels=1,
            sample_width_bits=16,
            frame_count=len(payload) // 2,
            byte_size=len(payload),
            digest=hashlib.sha256(payload).hexdigest(),
            created_at=1_000.0,
            expires_at=None,
            relative_path=relative,
        )
    )


def _populate(store: Store, *, audio: bool = True) -> dict[str, str]:
    """One document, one finished job with a segment, and its audio."""
    doc = store.save_document("제목", KOREAN, at=900.0)
    job = store.create_job(_job("job_done"))
    store.insert_segments(
        job.job_id,
        [
            Segment(
                index=0,
                source=TextRange(0, 5),
                spoken_text="안녕하세요",
                language="ko",
                time=TimeRange(0, 1200),
                frame_count=52_920,
                ready=True,
                audio_path=str(paths.temp_dir() / "seg0.wav"),
            )
        ],
    )
    ids = {"document": doc.document_id, "job": job.job_id}
    if audio:
        ids["result"] = _attach_audio(store, job.job_id).result_id
    return ids


def _members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return sorted(i.filename for i in zf.infolist())


def _manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read("manifest.json"))


def _rewrite(source: Path, target: Path, *, drop=(), add=None, manifest_edit=None) -> Path:
    """Rebuild an archive with entries removed, added, or a manifest edited.

    Used to forge the archives N-27 is about; forging them by hand is the only
    way to test a reader against input its own writer would never produce.
    """
    with zipfile.ZipFile(source) as src:
        entries = [(i, src.read(i.filename)) for i in src.infolist()]
    manifest = json.loads(dict((i.filename, b) for i, b in entries)["manifest.json"])
    if manifest_edit is not None:
        manifest_edit(manifest)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for info, payload in entries:
            if info.filename in drop:
                continue
            if info.filename == "manifest.json":
                payload = json.dumps(manifest).encode("utf-8")
            dst.writestr(info.filename, payload)
        for name, payload, attr in add or ():
            entry = zipfile.ZipInfo(name)
            if attr is not None:
                entry.external_attr = attr
            dst.writestr(entry, payload)
    return target


def _forge(source: Path, target: Path, *, members=None, manifest_edit=None, resign=True) -> Path:
    """Rebuild an archive with member payloads replaced.

    ``resign`` re-states the manifest for every replaced member -- its digest,
    its size, its row count, and the archive's decompressed total -- so what
    is left is a forgery only the manifest's *aggregate* claims could catch.
    That is the interesting case: a forger who can rewrite a member can
    rewrite the line about it just as easily.
    """
    with zipfile.ZipFile(source) as src:
        payloads = {i.filename: src.read(i.filename) for i in src.infolist()}
    manifest = json.loads(payloads["manifest.json"])
    for name, blob in (members or {}).items():
        payloads[name] = blob
        if not resign:
            continue
        entry = {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
        if name.endswith(".jsonl"):
            entry["records"] = blob.count(b"\n")
        manifest["members"][name] = entry
    if resign:
        manifest["uncompressed_bytes"] = sum(m["bytes"] for m in manifest["members"].values())
    if manifest_edit is not None:
        manifest_edit(manifest)
    payloads["manifest.json"] = json.dumps(manifest).encode("utf-8")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, blob in payloads.items():
            dst.writestr(name, blob)
    return target


def _document_lines(count: int) -> bytes:
    """A documents member holding ``count`` records a restore would accept."""
    return b"".join(
        json.dumps(
            {
                "document_id": f"doc_{i:05d}",
                "title": "제목",
                "body": KOREAN,
                "created_at": 900.0,
                "modified_at": 900.0,
                "version": 1,
            }
        ).encode("utf-8")
        + b"\n"
        for i in range(count)
    )


_LOCAL_HEADER_FIELD = {"flags": 6, "method": 8}
_CENTRAL_HEADER_FIELD = {"flags": 8, "method": 10}


def _corrupt_header(path: Path, member: str, *, field: str, value: int) -> None:
    """Patch one two-byte header field of one member, in both directories.

    ``zipfile`` will not write an archive like this, which is the point: the
    encryption flag and an unknown compression method are how a hostile file
    makes a reader raise a builtin instead of ``BadZipFile``.
    """
    blob = bytearray(path.read_bytes())
    name = member.encode()
    for signature, name_at, field_at, len_at in (
        (b"PK\x03\x04", 30, _LOCAL_HEADER_FIELD[field], 26),
        (b"PK\x01\x02", 46, _CENTRAL_HEADER_FIELD[field], 28),
    ):
        at = blob.find(signature)
        while at >= 0:
            length = int.from_bytes(blob[at + len_at : at + len_at + 2], "little")
            if bytes(blob[at + name_at : at + name_at + length]) == name:
                blob[at + field_at : at + field_at + 2] = value.to_bytes(2, "little")
                break
            at = blob.find(signature, at + 1)
        else:  # pragma: no cover -- the member is always there
            raise AssertionError(f"{member} not found in the archive")
    path.write_bytes(bytes(blob))


def _lower_the_restore_ceiling(monkeypatch, *, items: int, byte_size: int) -> None:
    """Stand in for a library that has outgrown 4.1's restore ceiling.

    Reaching the real ceiling takes about a hundred long retained jobs and a
    hundred thousand segment rows; lowering the ceiling puts the same lines
    under the same pressure in a moment.  Both the module's own reading of
    the policy value and the ceiling baked into the read functions' defaults
    have to move, because the defect was that the writer used the latter.
    """
    monkeypatch.setattr(bk, "BACKUP_RESTORE_MAX_ITEMS", items)
    monkeypatch.setattr(bk, "BACKUP_RESTORE_MAX_BYTES", byte_size)
    for function in (bk.verify_backup, bk.inspect_backup, bk.restore_backup):
        monkeypatch.setitem(function.__kwdefaults__, "max_items", items)
        monkeypatch.setitem(function.__kwdefaults__, "max_bytes", byte_size)


# ======================================================================
# F-44: what a bundle holds, and what it can never hold
# ======================================================================


def test_a_bundle_holds_documents_history_and_the_selected_audio(store, out):
    _populate(store)
    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    assert outcome.counts.documents == 1
    assert outcome.counts.jobs == 1
    assert outcome.counts.segments == 1
    assert outcome.counts.audio_files == 1
    assert outcome.path.is_file()
    assert "data/documents.jsonl" in _members(outcome.path)


def test_the_bundle_discloses_that_it_contains_body_text_and_audio(store, out):
    _populate(store)
    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    assert outcome.contains_body_text and outcome.contains_audio
    manifest = _manifest(outcome.path)
    assert "text" in manifest["disclosure"] and "audio" in manifest["disclosure"]
    assert inspect_backup(outcome.path).disclosure == bk.DISCLOSURE


def test_no_credential_material_and_no_model_file_can_appear_in_an_archive(store, out):
    """F-44 and 4.2 exclude both, and the writer is what has to guarantee it."""
    secret = "token-abcdef0123456789"
    (paths.data_dir() / CREDENTIALS_FILENAME).write_text(
        json.dumps({"credentials": [{"verifier": secret}]}), encoding="utf-8"
    )
    (paths.model_cache_dir() / "supertonic-3").mkdir(parents=True, exist_ok=True)
    (paths.model_cache_dir() / "supertonic-3" / "model.onnx").write_bytes(b"WEIGHTS" * 100)
    _populate(store)
    store.upsert_client(client_id="cli_other", label="a script", capabilities=())

    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    names = _members(outcome.path)
    assert not any("credential" in n or ".onnx" in n or "model" in n for n in names)
    with zipfile.ZipFile(outcome.path) as zf:
        blob = b"".join(zf.read(n) for n in names)
    assert secret.encode() not in blob
    assert b"WEIGHTS" not in blob
    assert b"cli_other" not in blob


def test_a_stored_audio_path_that_escapes_the_audio_root_writes_no_backup(store, out):
    """N-27: a tampered row is exactly how a credential would get in."""
    (paths.data_dir() / CREDENTIALS_FILENAME).write_text("super secret", encoding="utf-8")
    _populate(store)
    with sqlite3.connect(store.path) as raw:
        raw.execute(
            "UPDATE results SET relative_path = ?", (f"../{CREDENTIALS_FILENAME}",)
        )
    destination = out / f"mine{BACKUP_SUFFIX}"

    with pytest.raises(EchoActError) as caught:
        create_backup(store, destination)

    assert caught.value.code is Code.BACKUP_INVALID
    assert not destination.exists()
    assert not list(out.iterdir())


def test_an_external_clients_permissions_are_not_carried_by_the_bundle(store, out):
    store.upsert_client(client_id="cli_ext", label="script", capabilities=())
    store.create_job(_job("job_ext", owner="cli_ext", path=RequestPath.REST))
    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    with zipfile.ZipFile(outcome.path) as zf:
        job = json.loads(zf.read("data/jobs.jsonl").splitlines()[0])
    assert "owner_client_id" not in job
    assert "client_label" not in job


def test_audio_not_selected_leaves_out_results_and_their_segments(store, out):
    _populate(store)
    outcome = create_backup(
        store, out / f"text{BACKUP_SUFFIX}", selection=BackupSelection(audio=False)
    )

    assert outcome.counts.jobs == 1
    assert outcome.counts.results == 0
    assert outcome.counts.segments == 0
    assert not any(n.startswith("audio/") for n in _members(outcome.path))


def test_a_result_whose_audio_file_is_gone_is_left_out_and_reported(store, out):
    ids = _populate(store)
    (store.audio_root / "job_done.wav").unlink()

    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    assert outcome.missing_results == (ids["result"],)
    assert outcome.counts.audio_files == 0
    assert outcome.counts.results == 0
    assert outcome.counts.jobs == 1


def test_audio_that_drifted_from_its_digest_is_bundled_with_the_truth(store, out):
    """F-45's finding is reported, not silently dropped -- and a bundle whose
    recorded digest disagreed with its own bytes could never be restored."""
    ids = _populate(store)
    drifted = b"different bytes entirely"
    (store.audio_root / "job_done.wav").write_bytes(drifted)

    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    assert outcome.corrupt_results == (ids["result"],)
    assert outcome.counts.audio_files == 1
    restored = restore_backup(store, outcome.path, owner_client_id=OWNER)
    job = store.get_job(restored.restored_id("job_done"))
    result = store.get_result_for_job(job.job_id)
    assert (store.audio_root / result.relative_path).read_bytes() == drifted
    assert result.digest == hashlib.sha256(drifted).hexdigest()


# ======================================================================
# N-27 / 5.3: one point in time
# ======================================================================


def test_a_document_edited_during_the_backup_contributes_its_state_at_the_start(store, out):
    """5.3's last row.  The edit lands after the snapshot is pinned."""
    ids = _populate(store)
    edited = threading.Event()

    def on_progress(tick):
        if tick.phase is BackupPhase.SNAPSHOT and not edited.is_set():
            store.update_document(ids["document"], body="edited mid-backup")
            edited.set()

    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}", progress=on_progress)

    assert edited.is_set()
    with zipfile.ZipFile(outcome.path) as zf:
        doc = json.loads(zf.read("data/documents.jsonl").splitlines()[0])
    assert doc["body"] == KOREAN
    assert store.get_document(ids["document"]).body == "edited mid-backup"


def test_the_temporary_results_of_an_in_progress_job_are_excluded(store, out):
    _populate(store)
    store.create_job(_job("job_running", state=JobState.ACCEPTED))
    store.update_job_state("job_running", JobState.GENERATING)
    _attach_audio(store, "job_running", b"RIFFin-flight-audio")

    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    with zipfile.ZipFile(outcome.path) as zf:
        blob = zf.read("data/jobs.jsonl")
        audio = [n for n in zf.namelist() if n.startswith("audio/")]
    assert b"job_running" not in blob
    assert len(audio) == 1


def test_a_one_off_job_is_not_history_and_is_not_backed_up(store, out):
    _populate(store)
    store.create_job(_job("job_oneoff", retention=RetentionMode.ONE_OFF))

    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    assert outcome.counts.jobs == 1


# ======================================================================
# N-27 / N-15: verification before restore
# ======================================================================


def test_a_backup_is_verified_and_recorded_when_it_is_written(store, out):
    _populate(store)
    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    assert outcome.record is not None and outcome.record.verified
    assert verify_backup(outcome.path, deep=True).format_version == BACKUP_FORMAT_VERSION


def test_a_file_that_is_not_an_archive_is_rejected(out):
    bogus = out / f"nonsense{BACKUP_SUFFIX}"
    bogus.write_bytes(b"not a zip at all")

    with pytest.raises(EchoActError) as caught:
        verify_backup(bogus)
    assert caught.value.code is Code.BACKUP_INVALID
    assert not caught.value.retryable


def test_a_tampered_member_is_caught_by_the_manifest_digest(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    forged = _rewrite(
        good,
        out / f"forged{BACKUP_SUFFIX}",
        drop={"data/documents.jsonl"},
        add=(("data/documents.jsonl", b'{"document_id":"doc_x"}\n', None),),
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(forged)
    assert caught.value.code is Code.BACKUP_INVALID


def test_a_backup_from_a_newer_data_format_is_refused_not_guessed_at(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    newer = _rewrite(
        good,
        out / f"newer{BACKUP_SUFFIX}",
        manifest_edit=lambda m: m.__setitem__("schema_version", 99),
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(newer)
    assert caught.value.code is Code.BACKUP_INCOMPATIBLE
    assert not caught.value.retryable


def test_a_backup_in_an_unknown_bundle_format_is_refused(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    newer = _rewrite(
        good, out / f"v9{BACKUP_SUFFIX}", manifest_edit=lambda m: m.__setitem__("format", 9)
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(newer)
    assert caught.value.code is Code.BACKUP_INCOMPATIBLE


def test_an_archive_with_no_manifest_is_rejected(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    stripped = out / f"stripped{BACKUP_SUFFIX}"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(stripped, "w") as dst:
        for info in src.infolist():
            if info.filename != "manifest.json":
                dst.writestr(info.filename, src.read(info.filename))

    with pytest.raises(EchoActError) as caught:
        verify_backup(stripped)
    assert caught.value.code is Code.BACKUP_INVALID


def test_an_unlisted_extra_member_is_rejected(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    smuggled = _rewrite(
        good, out / f"extra{BACKUP_SUFFIX}", add=(("audio/sneaky.wav", b"payload", None),)
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(smuggled)
    assert caught.value.code is Code.BACKUP_INVALID


# ======================================================================
# N-27: paths inside a backup cannot write outside the restore target
# ======================================================================


_UNIX_REGULAR = (0o100644 << 16)
_UNIX_SYMLINK = (0o120777 << 16)


@pytest.mark.parametrize(
    "entry",
    [
        "../../etc/x",
        "../outside.wav",
        "/etc/passwd",
        "C:/Windows/System32/evil.dll",
        "C:\\Windows\\evil.dll",
        "audio/../../escape.wav",
        "audio/./../../escape.wav",
        "\\\\server\\share\\evil.wav",
        "audio//nested/deep.wav",
    ],
)
def test_a_path_that_could_write_outside_the_target_rejects_the_whole_archive(store, out, entry):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    hostile = _rewrite(
        good, out / f"hostile{BACKUP_SUFFIX}", add=((entry, b"payload", _UNIX_REGULAR),)
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(hostile)
    assert caught.value.code is Code.BACKUP_INVALID


def test_a_symlink_entry_rejects_the_whole_archive(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    hostile = _rewrite(
        good,
        out / f"link{BACKUP_SUFFIX}",
        add=(("audio/link.wav", b"../../../../etc/passwd", _UNIX_SYMLINK),),
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(hostile)
    assert caught.value.code is Code.BACKUP_INVALID
    assert "link" in caught.value.message


def test_a_hostile_archive_writes_nothing_at_all(store, out, tmp_path):
    """Rejecting the whole archive is the answer: nothing lands anywhere."""
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    hostile = _rewrite(
        good, out / f"hostile{BACKUP_SUFFIX}", add=(("../../escape.wav", b"x", _UNIX_REGULAR),)
    )
    before = sorted(p.name for p in store.audio_root.iterdir())

    with pytest.raises(EchoActError):
        restore_backup(store, hostile, owner_client_id=OWNER)

    assert sorted(p.name for p in store.audio_root.iterdir()) == before
    assert not (tmp_path / "escape.wav").exists()
    assert not (tmp_path.parent / "escape.wav").exists()


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_an_archive_naming_the_same_entry_twice_is_rejected(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    doubled = out / f"doubled{BACKUP_SUFFIX}"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(doubled, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info.filename))
        dst.writestr("data/documents.jsonl", b'{"document_id":"doc_evil"}\n')

    with pytest.raises(EchoActError) as caught:
        verify_backup(doubled)
    assert caught.value.code is Code.BACKUP_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("flags", 1, id="entry-claims-to-be-encrypted"),
        pytest.param("method", 99, id="entry-uses-an-unknown-compression-method"),
    ],
)
def test_an_entry_no_reader_can_decode_is_rejected_rather_than_crashing(store, out, field, value):
    """``zipfile`` answers these with ``RuntimeError`` and
    ``NotImplementedError``; neither is a ``BadZipFile``, so a hostile archive
    used to escape as a bare builtin.  Rule 3 and N-27 both say one code."""
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    hostile = out / f"hostile{BACKUP_SUFFIX}"
    hostile.write_bytes(good.read_bytes())
    _corrupt_header(hostile, "data/documents.jsonl", field=field, value=value)

    with pytest.raises(EchoActError) as caught:
        verify_backup(hostile)
    assert caught.value.code is Code.BACKUP_INVALID

    with pytest.raises(EchoActError):
        restore_backup(store, hostile, owner_client_id=OWNER)
    assert len(store.list_documents().items) == 1


def test_a_record_naming_audio_outside_the_bundle_is_rejected(store, out):
    """The audio member name is checked again where it comes out of a record."""
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    forged = out / f"forged{BACKUP_SUFFIX}"
    with zipfile.ZipFile(good) as src:
        entries = {i.filename: src.read(i.filename) for i in src.infolist()}
    record = json.loads(entries["data/results.jsonl"].splitlines()[0])
    record["audio_member"] = "../../../etc/passwd"
    entries["data/results.jsonl"] = (json.dumps(record) + "\n").encode()
    manifest = json.loads(entries["manifest.json"])
    manifest["members"]["data/results.jsonl"] = {
        "sha256": hashlib.sha256(entries["data/results.jsonl"]).hexdigest(),
        "bytes": len(entries["data/results.jsonl"]),
        "records": 1,
    }
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(forged, "w") as dst:
        for name, payload in entries.items():
            dst.writestr(name, payload)

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, forged, owner_client_id=OWNER)
    assert caught.value.code is Code.BACKUP_INVALID


# ======================================================================
# 4.1: decompressed size, item count, retention, free space
# ======================================================================


def test_the_default_limits_are_the_ones_section_4_1_fixes():
    assert bk.restore_backup.__kwdefaults__["max_bytes"] == BACKUP_RESTORE_MAX_BYTES
    assert bk.restore_backup.__kwdefaults__["max_items"] == BACKUP_RESTORE_MAX_ITEMS
    assert BACKUP_RESTORE_MAX_BYTES == 100 * GB
    assert BACKUP_RESTORE_MAX_ITEMS == 100_000


def test_a_zip_bomb_is_refused_on_what_it_decompresses_to_not_what_it_weighs(store, out):
    """A megabyte of zeros compresses to nothing; the cap is on the far side."""
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    bomb = out / f"bomb{BACKUP_SUFFIX}"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info.filename))
        dst.writestr("audio/bomb.wav", b"\0" * (4 << 20))

    assert bomb.stat().st_size < 100_000

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, bomb, owner_client_id=OWNER, max_bytes=1 << 20)
    assert caught.value.code is Code.BACKUP_TOO_LARGE
    assert not caught.value.retryable


def test_a_manifest_claiming_more_than_a_hundred_gigabytes_is_refused(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    huge = _rewrite(
        good,
        out / f"huge{BACKUP_SUFFIX}",
        manifest_edit=lambda m: m.__setitem__("uncompressed_bytes", 200 * GB),
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(huge)
    assert caught.value.code is Code.BACKUP_TOO_LARGE


def test_a_manifest_claiming_more_than_a_hundred_thousand_items_is_refused(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    many = _rewrite(
        good,
        out / f"many{BACKUP_SUFFIX}",
        manifest_edit=lambda m: m.__setitem__("item_count", 200_000),
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(many)
    assert caught.value.code is Code.BACKUP_TOO_LARGE


def test_a_manifest_cannot_declare_fewer_rows_than_its_member_actually_holds(store, out):
    """4.1's ceiling was counted against the manifest's own figures and the
    number of zip entries, never against the rows a restore would insert, so
    an archive that simply lied restored an unbounded number of them."""
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    forged = _forge(
        good,
        out / f"many{BACKUP_SUFFIX}",
        members={"data/documents.jsonl": _document_lines(200)},
        # Both halves of the lie kept consistent: the member list agrees with
        # the counts, and only the member's contents disagree with both.
        manifest_edit=lambda m: m["members"]["data/documents.jsonl"].__setitem__("records", 1),
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(forged)
    assert caught.value.code is Code.BACKUP_INVALID

    with pytest.raises(EchoActError):
        restore_backup(store, forged, owner_client_id=OWNER)
    assert len(store.list_documents().items) == 1


def test_a_manifest_cannot_declare_fewer_items_than_its_own_member_list(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    forged = _forge(
        good,
        out / f"counts{BACKUP_SUFFIX}",
        members={"data/documents.jsonl": _document_lines(200)},
        manifest_edit=lambda m: m.update(item_count=1),
    )

    for read in (verify_backup, inspect_backup):
        with pytest.raises(EchoActError) as caught:
            read(forged)
        assert caught.value.code is Code.BACKUP_INVALID


def test_a_manifest_cannot_understate_what_restoring_it_would_cost(store, out):
    """The free-space and retention guards are run against these two numbers
    before a row is applied, so a bundle may not make itself look smaller."""
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    honest = inspect_backup(good)
    assert honest.uncompressed_bytes > 1 and honest.retention_bytes > 1
    small = _forge(
        good,
        out / f"small{BACKUP_SUFFIX}",
        manifest_edit=lambda m: m.update(uncompressed_bytes=1, retention_bytes=1),
        resign=False,
    )

    with pytest.raises(EchoActError) as caught:
        verify_backup(small)
    assert caught.value.code is Code.BACKUP_INVALID


def test_a_library_past_the_restore_ceiling_can_still_be_backed_up(store, out, monkeypatch):
    """4.1 caps what a restore may apply; nothing caps what a library holds.

    Enforcing the restore ceiling on the writing side makes F-44's manual
    backup impossible, F-74's scheduled backup fail on every tick for ever,
    and the N-15 copy taken before a replace unwritable -- all at once, and
    all at the moment there is most data to lose.
    """
    _populate(store)
    _lower_the_restore_ceiling(monkeypatch, items=2, byte_size=64)

    outcome = create_backup(store, out / f"big{BACKUP_SUFFIX}")

    assert outcome.path.is_file()
    assert outcome.item_count == 5
    assert BackupScheduler().run_due(store, DAY, enabled=True, location=out) is not None

    # ...and the ceiling still holds on the side 4.1 actually governs.
    with pytest.raises(EchoActError) as caught:
        restore_backup(store, outcome.path, owner_client_id=OWNER)
    assert caught.value.code is Code.BACKUP_TOO_LARGE


def test_too_many_entries_is_refused_before_anything_is_read(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path

    with pytest.raises(EchoActError) as caught:
        verify_backup(good, max_items=2)
    assert caught.value.code is Code.BACKUP_TOO_LARGE


def test_a_restore_that_would_pass_the_retention_limit_is_refused_before_applying(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path
    store.set_retention_limit(store.storage_usage().total_bytes + 4)

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, archive, owner_client_id=OWNER)

    assert caught.value.code is Code.RETENTION_LIMIT_REACHED
    assert len(store.list_documents().items) == 1


def test_replacing_is_not_refused_for_the_space_it_is_about_to_free(store, out):
    """The pre-apply retention check ran before the mode was consulted, so it
    added the incoming bundle to data ``_clear_library`` was about to delete:
    a library over half its allowance could not be restored from its own
    backup, which is the one case REPLACE exists for (Section 9)."""
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path
    used = store.storage_usage().total_bytes
    store.set_retention_limit(used + used // 2)

    outcome = restore_backup(store, archive, owner_client_id=OWNER, mode=RestoreMode.REPLACE)

    assert not outcome.cancelled
    assert outcome.counts.documents == 1
    usage = store.storage_usage()
    assert usage.total_bytes <= usage.limit_bytes


def test_a_backup_is_not_started_when_the_disk_is_nearly_full(store, out):
    _populate(store)

    with pytest.raises(EchoActError) as caught:
        create_backup(store, out / f"mine{BACKUP_SUFFIX}", free_space=lambda _p: 10)

    assert caught.value.code is Code.STORAGE_FULL
    assert not (out / f"mine{BACKUP_SUFFIX}").exists()


def test_a_restore_is_refused_when_the_disk_is_nearly_full(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, archive, owner_client_id=OWNER, free_space=lambda _p: 10)
    assert caught.value.code is Code.STORAGE_FULL


# ======================================================================
# F-44: restore adds items, and says which is which
# ======================================================================


def test_restore_adds_new_items_and_distinguishes_the_original_identifiers(store, out):
    original = _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path

    outcome = restore_backup(store, archive, owner_client_id=OWNER)

    assert outcome.mode is RestoreMode.ADD
    assert len(store.list_documents().items) == 2
    assert store.list_jobs().total == 2
    restored_doc = outcome.restored_id(original["document"])
    assert restored_doc is not None and restored_doc != original["document"]
    assert outcome.originals("document") == (original["document"],)
    assert store.get_document(restored_doc).body == KOREAN
    assert store.get_document(original["document"]).body == KOREAN


def test_restored_data_belongs_to_the_gui_owner(store, out):
    store.create_job(_job("job_ext", owner="cli_ext", path=RequestPath.REST))
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path

    outcome = restore_backup(store, archive, owner_client_id="cli_gui_owner")

    restored = store.get_job(outcome.restored_id("job_ext"))
    assert restored.owner_client_id == "cli_gui_owner"
    assert restored.client_label is None


def test_restored_audio_is_readable_and_matches_its_recorded_digest(store, out):
    payload = b"RIFF" + b"sound" * 40
    store.save_document("t", "b", at=900.0)
    store.create_job(_job("job_a"))
    _attach_audio(store, "job_a", payload)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path

    outcome = restore_backup(store, archive, owner_client_id=OWNER)

    job = store.get_job(outcome.restored_id("job_a"))
    result = store.get_result_for_job(job.job_id)
    assert result is not None
    restored_file = store.audio_root / result.relative_path
    assert restored_file.read_bytes() == payload
    assert result.digest == hashlib.sha256(payload).hexdigest()
    assert store.result_integrity(result.result_id).value == "ok"


def test_a_restored_segment_keeps_its_code_point_range_and_its_audio_times(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path

    outcome = restore_backup(store, archive, owner_client_id=OWNER)

    segments = store.list_segments(outcome.restored_id("job_done"))
    assert len(segments) == 1
    assert (segments[0].source.start, segments[0].source.end) == (0, 5)
    assert segments[0].time == TimeRange(0, 1200)
    # Segment scratch audio lives in the tree N-02 clears; it is never bundled.
    assert segments[0].audio_path is None


def test_restore_reports_progress_and_finishes_with_a_complete_phase(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path
    seen: list[RestorePhase] = []

    restore_backup(store, archive, owner_client_id=OWNER, progress=lambda t: seen.append(t.phase))

    assert seen[0] is RestorePhase.CHECKING
    assert RestorePhase.APPLYING in seen
    assert seen[-1] is RestorePhase.COMPLETE


def test_a_cancelled_restore_applies_nothing(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path
    token = CancelToken()

    def cancel_once_applying(tick):
        if tick.phase is RestorePhase.APPLYING:
            token.cancel()

    outcome = restore_backup(
        store, archive, owner_client_id=OWNER, cancel=token, progress=cancel_once_applying
    )

    assert outcome.cancelled
    assert len(store.list_documents().items) == 1
    assert store.list_jobs().total == 1
    assert not (store.audio_root / "restored").exists() or not list(
        (store.audio_root / "restored").iterdir()
    )


def test_a_restore_of_a_job_that_never_finished_is_refused(store, out):
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    forged = out / f"forged{BACKUP_SUFFIX}"
    with zipfile.ZipFile(good) as src:
        entries = {i.filename: src.read(i.filename) for i in src.infolist()}
    job = json.loads(entries["data/jobs.jsonl"].splitlines()[0])
    job["state"] = "generating"
    entries["data/jobs.jsonl"] = (json.dumps(job) + "\n").encode()
    manifest = json.loads(entries["manifest.json"])
    manifest["members"]["data/jobs.jsonl"] = {
        "sha256": hashlib.sha256(entries["data/jobs.jsonl"]).hexdigest(),
        "bytes": len(entries["data/jobs.jsonl"]),
        "records": 1,
    }
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(forged, "w") as dst:
        for name, payload in entries.items():
            dst.writestr(name, payload)

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, forged, owner_client_id=OWNER)
    assert caught.value.code is Code.BACKUP_INVALID
    assert store.list_jobs().total == 1


# ======================================================================
# N-15: existing data is validated before it is overwritten
# ======================================================================


def test_replacing_secures_a_recoverable_copy_before_deleting_anything(store, out):
    original = _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path

    outcome = restore_backup(store, archive, owner_client_id=OWNER, mode=RestoreMode.REPLACE)

    assert outcome.safety_backup is not None and outcome.safety_backup.is_file()
    recovered = verify_backup(outcome.safety_backup, deep=True)
    assert recovered.counts.documents == 1
    assert store.list_jobs().total == 1
    assert outcome.restored_id(original["job"]) is not None
    with pytest.raises(EchoActError):
        store.get_document(original["document"])


def test_a_replace_from_a_bundle_with_rotted_audio_deletes_nothing(store, out):
    """N-15 and Section 9: the audio has to be hashed *before* the library it
    would replace is deleted, not on the way out of the archive afterwards.

    ``_clear_library`` commits outside the apply transaction, so a mismatch
    found during ``_apply`` used to leave nothing behind at all.
    """
    original = _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    member = next(n for n in _members(good) if n.startswith("audio/"))
    with zipfile.ZipFile(good) as src:
        rotted = b"\0" * len(src.read(member))
    forged = _forge(good, out / f"rot{BACKUP_SUFFIX}", members={member: rotted}, resign=False)

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, forged, owner_client_id=OWNER, mode=RestoreMode.REPLACE)

    assert caught.value.code is Code.BACKUP_INVALID
    assert store.get_document(original["document"]).body == KOREAN
    assert store.list_jobs().total == 1
    assert (store.audio_root / "job_done.wav").is_file()


def test_a_replace_that_fails_while_applying_puts_the_existing_data_back(store, out):
    """The apply transaction cannot roll back the deletion that preceded it.

    A record the archive should never have held is discovered row by row, and
    by then the library is already gone; F-44's "existing data is preserved"
    has to survive that, and the error has to say where the copy is.
    """
    _populate(store)
    good = create_backup(store, out / f"good{BACKUP_SUFFIX}").path
    with zipfile.ZipFile(good) as src:
        segment = json.loads(src.read("data/segments.jsonl").splitlines()[0])
    segment["job_id"] = "job_not_in_this_bundle"
    forged = _forge(
        good,
        out / f"orphan{BACKUP_SUFFIX}",
        members={"data/segments.jsonl": (json.dumps(segment) + "\n").encode("utf-8")},
    )

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, forged, owner_client_id=OWNER, mode=RestoreMode.REPLACE)

    assert caught.value.code is Code.BACKUP_INVALID
    assert caught.value.detail["existing_data"] == "restored"
    assert caught.value.detail["safety_backup"]
    assert len(store.list_documents().items) == 1
    assert store.list_jobs().total == 1
    result = store.get_result_for_job(store.list_jobs().items[0].job_id)
    assert result is not None
    assert (store.audio_root / result.relative_path).is_file()


def test_a_replace_that_fails_while_deleting_puts_the_existing_data_back(store, out, monkeypatch):
    """Clearing the library is not one step, and none of it is in the apply
    transaction: a failure after the history has gone but before the documents
    have needs the same answer as a failure while applying.  What survived the
    clear is restored alongside itself, because a duplicate the user can
    delete beats a job they cannot get back."""
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path

    def refuse(*_args, **_kwargs):
        raise EchoActError(Code.DB_UNAVAILABLE, "the database went away")

    monkeypatch.setattr(Store, "delete_document", refuse)

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, archive, owner_client_id=OWNER, mode=RestoreMode.REPLACE)

    assert caught.value.code is Code.DB_UNAVAILABLE
    assert caught.value.detail["existing_data"] == "restored"
    assert store.list_jobs().total == 1
    assert len(store.list_documents().items) >= 1


def test_replacing_refuses_while_a_job_is_still_running(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path
    store.create_job(_job("job_running", state=JobState.ACCEPTED))

    with pytest.raises(EchoActError) as caught:
        restore_backup(store, archive, owner_client_id=OWNER, mode=RestoreMode.REPLACE)

    assert caught.value.code is Code.DELETE_BLOCKED_IN_USE
    assert store.list_jobs().total == 2


# ======================================================================
# 5.3: during restore, new generation and edits are blocked
# ======================================================================


def test_the_gate_blocks_new_work_while_a_restore_runs_and_releases_after(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path
    gate = RestoreGate()
    inside: list[bool] = []

    def watch(_tick):
        if not inside:
            inside.append(gate.active)
            with pytest.raises(EchoActError) as caught:
                gate.require_idle("Generation")
            assert caught.value.code is Code.BUSY
            assert caught.value.retry_after_s is not None

    restore_backup(store, archive, owner_client_id=OWNER, gate=gate, progress=watch)

    assert inside == [True]
    assert not gate.active
    gate.require_idle("Generation")


def test_the_gate_publishes_progress_for_the_screen_to_read(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path
    gate = RestoreGate()
    seen: list[object] = []

    restore_backup(
        store,
        archive,
        owner_client_id=OWNER,
        gate=gate,
        progress=lambda _t: seen.append(gate.progress),
    )

    assert any(p is not None for p in seen)
    assert gate.progress is None


def test_a_backup_cannot_be_taken_while_a_restore_is_running(store, out):
    gate = RestoreGate()
    with gate.hold():
        with pytest.raises(EchoActError) as caught:
            create_backup(store, out / f"mine{BACKUP_SUFFIX}", gate=gate)
    assert caught.value.code is Code.BUSY


def test_two_restores_cannot_run_at_once(store, out):
    _populate(store)
    archive = create_backup(store, out / f"mine{BACKUP_SUFFIX}").path
    gate = RestoreGate()
    with gate.hold():
        with pytest.raises(EchoActError) as caught:
            restore_backup(store, archive, owner_client_id=OWNER, gate=gate)
    assert caught.value.code is Code.BUSY


# ======================================================================
# F-74: the scheduled backup
# ======================================================================


def test_the_scheduler_is_off_by_default(store, tmp_path):
    assert SCHEDULED_BACKUP_DEFAULT is False
    assert Settings().scheduled_backup is SCHEDULED_BACKUP_DEFAULT

    decision = BackupScheduler().decide(
        0.0, enabled=Settings().scheduled_backup, location=tmp_path, last_run=None
    )
    assert not decision
    assert decision.reason is ScheduleReason.DISABLED


def test_the_period_is_one_day(store):
    assert bk.SCHEDULED_BACKUP_INTERVAL_S == DAY
    assert BackupScheduler().interval_s == DAY
    assert BackupScheduler().keep == BACKUP_KEEP_SCHEDULED == 7


def test_it_runs_once_a_day_and_not_before(store, tmp_path):
    scheduler = BackupScheduler()
    early = scheduler.decide(DAY - 1, enabled=True, location=tmp_path, last_run=0.0)
    assert not early.run
    assert early.reason is ScheduleReason.NOT_DUE
    assert early.due_at == DAY

    due = scheduler.decide(DAY + 1, enabled=True, location=tmp_path, last_run=0.0)
    assert due.run
    assert due.reason is ScheduleReason.DUE


def test_a_missed_schedule_is_made_up_only_once(store, out):
    """F-74: five days away is one backup, not five."""
    _populate(store)
    scheduler = BackupScheduler()
    five_days_late = 5 * DAY

    decision = scheduler.decide(
        five_days_late, enabled=True, location=out, last_run=0.0
    )
    assert decision.run and decision.reason is ScheduleReason.MISSED

    first = scheduler.run_due(store, five_days_late, enabled=True, location=out)
    assert first is not None
    again = scheduler.run_due(store, five_days_late + 1, enabled=True, location=out)
    assert again is None
    assert len(store.list_backups(kind="scheduled")) == 1


def test_it_is_deferred_during_generation_and_stays_due_afterwards(store, tmp_path):
    scheduler = BackupScheduler()
    deferred = scheduler.decide(
        DAY + 1, enabled=True, location=tmp_path, last_run=0.0, generating=True
    )
    assert not deferred.run
    assert deferred.reason is ScheduleReason.DEFERRED_GENERATING

    later = scheduler.decide(DAY + 2, enabled=True, location=tmp_path, last_run=0.0)
    assert later.run


def test_it_is_deferred_during_a_restore(store, tmp_path):
    scheduler = BackupScheduler()
    decision = scheduler.decide(
        DAY + 1, enabled=True, location=tmp_path, last_run=0.0, restoring=True
    )
    assert not decision.run
    assert decision.reason is ScheduleReason.DEFERRED_RESTORING


def test_run_due_defers_while_the_restore_gate_is_held(store, out):
    _populate(store)
    gate = RestoreGate()
    with gate.hold():
        assert BackupScheduler().run_due(store, DAY, enabled=True, location=out, gate=gate) is None
    assert not list(out.iterdir())


def test_a_failing_scheduled_attempt_is_not_retried_on_every_tick(store, out):
    _populate(store)
    scheduler = BackupScheduler()
    with pytest.raises(EchoActError):
        scheduler.run_due(store, DAY, enabled=True, location=out, free_space=lambda _p: 10)

    blocked = scheduler.decide(DAY + 60, enabled=True, location=out, last_run=None)
    assert not blocked.run
    assert blocked.reason is ScheduleReason.DEFERRED_AFTER_FAILURE
    assert scheduler.decide(
        DAY + bk.SCHEDULED_BACKUP_RETRY_S + 1, enabled=True, location=out, last_run=None
    ).run


def test_a_scheduled_attempt_that_fails_unexpectedly_is_backed_off_too(store, out, monkeypatch):
    """The backoff was armed only for EchoActError, so a failure arriving any
    other way was retried on every tick -- once a minute, for ever."""
    _populate(store)

    def boom(*_args, **_kwargs):
        raise RuntimeError("the writer fell over")

    monkeypatch.setattr(bk, "_write_archive", boom)
    scheduler = BackupScheduler()
    with pytest.raises(RuntimeError):
        scheduler.run_due(store, DAY, enabled=True, location=out)

    blocked = scheduler.decide(DAY + 60, enabled=True, location=out, last_run=None)
    assert not blocked.run
    assert blocked.reason is ScheduleReason.DEFERRED_AFTER_FAILURE
    assert not list(out.iterdir())


def test_the_seven_most_recent_scheduled_backups_are_kept(store, out):
    _populate(store)
    scheduler = BackupScheduler()
    for day in range(1, 11):
        assert scheduler.run_due(store, day * DAY, enabled=True, location=out) is not None

    records = store.list_backups(kind="scheduled")
    assert len(records) == 7
    assert [round(r.created_at) for r in records] == [d * DAY for d in range(10, 3, -1)]
    assert len(list(out.glob(f"*{BACKUP_SUFFIX}"))) == 7


def test_older_scheduled_backups_go_only_after_the_new_one_verifies(store, out):
    _populate(store)
    scheduler = BackupScheduler(keep=1)
    first = scheduler.run_due(store, DAY, enabled=True, location=out)
    assert first is not None

    # A failed attempt must not take the sound one with it.
    with pytest.raises(EchoActError):
        scheduler.run_due(store, 3 * DAY, enabled=True, location=out, free_space=lambda _p: 10)
    assert first.path.is_file()
    assert len(store.list_backups(kind="scheduled")) == 1

    second = scheduler.run_due(
        store, 3 * DAY + bk.SCHEDULED_BACKUP_RETRY_S + 1, enabled=True, location=out
    )
    assert second is not None
    assert not first.path.exists()
    assert second.path.is_file()


def test_a_manual_backup_is_never_deleted_automatically(store, out):
    _populate(store)
    manual = create_backup(store, out / f"by-hand{BACKUP_SUFFIX}")
    scheduler = BackupScheduler(keep=1)
    for day in (1, 2, 3):
        scheduler.run_due(store, day * DAY, enabled=True, location=out)

    assert manual.path.is_file()
    kinds = {r.kind for r in store.list_backups()}
    assert "manual" in kinds
    assert len([r for r in store.list_backups() if r.kind == "manual"]) == 1


def test_pruning_never_touches_a_pre_migration_copy(store, out):
    _populate(store)
    store.record_backup(location=str(out / "pre.sqlite3"), kind="pre_migration", verified=True)
    scheduler = BackupScheduler(keep=1)
    scheduler.run_due(store, DAY, enabled=True, location=out)
    scheduler.run_due(store, 2 * DAY, enabled=True, location=out)

    assert len(store.list_backups(kind="pre_migration")) == 1


def test_an_unverified_scheduled_record_is_not_counted_as_a_run(store, out):
    _populate(store)
    store.record_backup(
        location=str(out / f"attempt{BACKUP_SUFFIX}"), kind="scheduled", verified=False, at=DAY
    )
    assert last_scheduled_run(store) is None

    outcome = BackupScheduler().run_due(store, DAY + 10, enabled=True, location=out)
    assert outcome is not None
    assert last_scheduled_run(store) == DAY + 10


def test_pruning_removes_the_record_as_well_as_the_file(store, out):
    _populate(store)
    stale = out / f"stale{BACKUP_SUFFIX}"
    stale.write_bytes(b"old")
    store.record_backup(location=str(stale), kind="scheduled", verified=True, at=1.0)
    store.record_backup(location=str(out / "gone.echoactbak"), kind="scheduled", verified=True, at=2.0)

    removed = prune_scheduled_backups(store, keep=1)

    assert len(removed) == 1
    assert not stale.exists()
    assert len(store.list_backups(kind="scheduled")) == 1


def test_a_scheduled_file_name_is_sortable_and_unique():
    first = bk.scheduled_backup_name(1_700_000_000.0)
    second = bk.scheduled_backup_name(1_700_000_000.0)
    assert first != second
    assert first.endswith(BACKUP_SUFFIX)
    assert time.strftime("%Y%m%dT", time.gmtime(1_700_000_000.0)) in first
    assert bk.scheduled_backup_name(1.0) < bk.scheduled_backup_name(2_000_000_000.0)


def test_a_location_that_was_never_chosen_stops_the_schedule(store):
    decision = BackupScheduler().decide(DAY, enabled=True, location=None, last_run=None)
    assert not decision.run
    assert decision.reason is ScheduleReason.NO_LOCATION


# ======================================================================
# Housekeeping
# ======================================================================


def test_a_cancelled_backup_leaves_nothing_where_the_user_chose(store, out):
    _populate(store)
    token = CancelToken()
    token.cancel()
    destination = out / f"mine{BACKUP_SUFFIX}"

    outcome = create_backup(store, destination, cancel=token)

    assert outcome.cancelled
    assert not destination.exists()
    assert not list(out.iterdir())
    assert store.list_backups() == ()


class _FailsAfterOneRead:
    """A file object that stops working part way through, as a failing disk
    does.  Wrapped rather than faked so the first chunk is the real bytes."""

    def __init__(self, handle) -> None:
        self._handle = handle
        self._reads = 0

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads > 1:
            raise OSError(5, "Input/output error")
        return self._handle.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._handle.close()

    def close(self) -> None:
        self._handle.close()


def test_a_read_error_part_way_through_a_wav_reports_it_and_leaves_no_file(
    store, out, monkeypatch
):
    """create_backup promises no plausible-looking file is left behind, and
    rule 3 promises one exception type.  A member writer left open when the
    read failed broke both: ``ZipFile.__exit__`` raised ``ValueError`` first,
    masking the real cause and holding the handle that stops the ``.part-``
    file being unlinked on Windows."""
    store.save_document("제목", KOREAN, at=900.0)
    store.create_job(_job("job_big"))
    _attach_audio(store, "job_big", b"RIFF" + b"\0" * (2 << 20))
    flaky = (store.audio_root / "job_big.wav").resolve()
    real_open = Path.open

    def open_maybe_failing(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        return _FailsAfterOneRead(handle) if self.resolve() == flaky else handle

    monkeypatch.setattr(Path, "open", open_maybe_failing)

    with pytest.raises(EchoActError) as caught:
        create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    assert caught.value.code is Code.BACKUP_INVALID
    assert list(out.iterdir()) == []


def test_an_existing_file_is_not_overwritten_unless_asked(store, out):
    _populate(store)
    destination = out / f"mine{BACKUP_SUFFIX}"
    destination.write_bytes(b"something the user cares about")

    with pytest.raises(EchoActError) as caught:
        create_backup(store, destination)
    assert caught.value.code is Code.BACKUP_INVALID
    assert destination.read_bytes() == b"something the user cares about"

    outcome = create_backup(store, destination, overwrite=True)
    assert outcome.byte_size == destination.stat().st_size


def test_an_empty_library_still_makes_a_sound_bundle(store, out):
    outcome = create_backup(store, out / f"empty{BACKUP_SUFFIX}")

    assert outcome.item_count == 0
    assert not outcome.contains_body_text
    assert verify_backup(outcome.path, deep=True).counts.jobs == 0
    restored = restore_backup(store, outcome.path, owner_client_id=OWNER)
    assert restored.counts.items == 0


def test_a_round_trip_of_a_selected_document_carries_only_that_document(store, out):
    first = store.save_document("하나", "첫번째", at=900.0)
    store.save_document("둘", "두번째", at=901.0)

    outcome = create_backup(
        store,
        out / f"one{BACKUP_SUFFIX}",
        selection=BackupSelection(history=False, document_ids=(first.document_id,)),
    )

    assert outcome.counts.documents == 1
    assert outcome.counts.jobs == 0
    restored = restore_backup(store, outcome.path, owner_client_id=OWNER)
    assert restored.counts.documents == 1
    assert len(store.list_documents().items) == 3


def test_inspect_describes_a_bundle_without_hashing_it(store, out):
    _populate(store)
    outcome = create_backup(store, out / f"mine{BACKUP_SUFFIX}")

    inspection = inspect_backup(outcome.path)

    assert inspection.counts.documents == 1
    assert inspection.contains_audio
    assert not inspection.deep
    assert inspection.compressed_bytes == outcome.path.stat().st_size


def test_a_missing_file_reports_that_rather_than_corruption(out):
    with pytest.raises(EchoActError) as caught:
        verify_backup(out / f"never-existed{BACKUP_SUFFIX}")
    assert caught.value.code is Code.FILE_NOT_FOUND
