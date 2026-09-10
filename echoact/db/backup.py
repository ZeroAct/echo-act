"""Backup and restore: F-44, N-15, N-27, F-74, and 4.1's restore limits.

A backup is one file the user chooses the location of.  Inside it is a ZIP
holding a manifest, the library's rows as JSON lines, and the full-result WAV
files the user asked for.  What is *not* inside it is the point of F-44 and
4.2: no model weights, no credentials, no client permissions.  That exclusion
is a property of the writer rather than a note in a document -- this module
never walks the data directory, it emits exactly the members it names, and it
refuses outright if a stored audio path resolves anywhere but inside the audio
directory.

Three shapes of the problem, and the decision each one forced:

* **Consistency (N-27, 5.3).**  The bundle is read through one deferred read
  transaction held open for its whole life, so a document edited half way
  through contributes the state it had when the backup started.  A file copy
  of a live write-ahead-log database would miss whatever is still in the log,
  and re-reading per table would splice two points in time together.
* **Restore is the dangerous direction (N-27).**  Everything about an archive
  is hostile input: member names, declared sizes, item counts, digests.  Every
  one of them is checked *before* anything is applied, and a single bad member
  rejects the whole archive rather than being skipped -- a partially applied
  restore is exactly the outcome N-15 forbids.  As a second layer, an archive
  member name is never used as a destination path; restored audio is written
  to a name this module mints.
* **Identity (F-44).**  A restore adds items.  Every row gets a freshly minted
  identifier and the original is reported back in :class:`RestoreOutcome`, so
  "which of these came from the backup, and what was it called before?" has an
  answer.  Reusing the original identifiers would either collide with live data
  or silently overwrite it, and F-44 asks for neither.

The cancel token and the progress callback are the same shape
``echoact.models.registry`` uses for a download, and :class:`CancelToken` is
literally that class: a GUI that already owns one for model preparation should
not have to learn a second cancellation vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .. import __version__
from ..domain import JobState, RetentionMode
from ..errors import Code, EchoActError
from ..models.registry import CancelToken
from ..paths import redact
from ..policy import (
    BACKUP_KEEP_SCHEDULED,
    BACKUP_RESTORE_MAX_BYTES,
    BACKUP_RESTORE_MAX_ITEMS,
    LOW_SPACE_WARNING_BYTES,
    WORKER_RELEASE_DEADLINE_S,
)
from ..util import ids
from ..util.logging import get_logger
from .migrations import SUPPORTED_SCHEMA_VERSION, default_backup_dir, translate_sqlite_error
from .store import BackupRecord, Store

log = get_logger("db.backup")

__all__ = [
    "BACKUP_FORMAT_VERSION",
    "BACKUP_SUFFIX",
    "DISCLOSURE",
    "EXCLUDED_FROM_BACKUP",
    "SCHEDULED_BACKUP_INTERVAL_S",
    "BackupInspection",
    "BackupOutcome",
    "BackupPhase",
    "BackupProgress",
    "BackupScheduler",
    "BackupSelection",
    "CancelToken",
    "ProgressCallback",
    "RESTORE_GATE",
    "RestoreGate",
    "RestoreMode",
    "RestoreOutcome",
    "RestorePhase",
    "RestoredItem",
    "ScheduleDecision",
    "ScheduleReason",
    "create_backup",
    "inspect_backup",
    "last_scheduled_run",
    "prune_scheduled_backups",
    "restore_backup",
    "scheduled_backup_name",
    "verify_backup",
]


# ======================================================================
# The archive's own contract
# ======================================================================

#: The bundle layout.  Bumped when a member's meaning changes; a reader that
#: does not recognise the number refuses rather than guessing (N-15).
BACKUP_FORMAT_VERSION: Final = 1

BACKUP_SUFFIX: Final = ".echoactbak"

MANIFEST_NAME: Final = "manifest.json"
DOCUMENTS_MEMBER: Final = "data/documents.jsonl"
JOBS_MEMBER: Final = "data/jobs.jsonl"
SEGMENTS_MEMBER: Final = "data/segments.jsonl"
RESULTS_MEMBER: Final = "data/results.jsonl"
AUDIO_PREFIX: Final = "audio/"

_TABLE_MEMBERS: Final = (DOCUMENTS_MEMBER, JOBS_MEMBER, SEGMENTS_MEMBER, RESULTS_MEMBER)

#: Which field of :class:`BackupCounts` each table member has to agree with.
#: 4.1's item ceiling is counted in these, so the mapping is what ties a
#: declared count to a member whose rows can be counted.
_COUNTED_MEMBERS: Final = {
    DOCUMENTS_MEMBER: "documents",
    JOBS_MEMBER: "jobs",
    SEGMENTS_MEMBER: "segments",
    RESULTS_MEMBER: "results",
}

#: F-44's disclosure, in English.  The GUI localises it through
#: ``echoact.ui.i18n``; it is stated here as well because it is written into
#: the manifest, so a bundle discloses its own contents wherever it is opened.
DISCLOSURE: Final = (
    "This backup contains the text of your saved documents, the text of your "
    "retained job history, and the generated audio you selected. Keep it "
    "somewhere you would keep the documents themselves. It is not encrypted."
)

#: What a bundle never contains, recorded in the manifest so the exclusion is
#: legible from the file itself (F-44, 4.2).
EXCLUDED_FROM_BACKUP: Final = (
    "model_weights",
    "credentials",
    "client_permissions",
    "idempotency_records",
    "in_progress_results",
    "one_off_results",
    "segment_scratch_audio",
)

#: F-74's "once daily".  ``echoact.policy`` fixes how many scheduled backups
#: are kept but not the period, so it is named here rather than spelled 86400
#: at the two places that need it.
SCHEDULED_BACKUP_INTERVAL_S: Final = 24 * 60 * 60

#: How long a failed scheduled attempt is left alone before another is tried.
#: Without it a one-minute tick would retry a failing backup sixty times an
#: hour; with it a transient failure still recovers well inside F-74's day.
SCHEDULED_BACKUP_RETRY_S: Final = 60 * 60

#: Read granularity.  Not a policy limit: it is how often cancellation and
#: progress are observable while a large WAV is copied.
_CHUNK_BYTES: Final = 1 << 20

#: A manifest is a few hundred bytes per table.  Anything larger is either a
#: bomb or not our manifest, and it is parsed before any limit is known.
_MAX_MANIFEST_BYTES: Final = 1 << 20

#: One JSON line holds at most one document body or one job snapshot, and F-03
#: caps either at 50,000 code points -- 150 KB of UTF-8 at the worst.  The cap
#: is far above that and exists only so a line that never ends cannot be read
#: into memory forever.
_MAX_RECORD_BYTES: Final = 8 << 20

_SAFE_AUDIO_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DRIVE_LETTER: Final = re.compile(r"^[A-Za-z]:")

_TERMINAL_STATE_VALUES: Final = tuple(
    s.value for s in JobState if s.is_terminal
)


# ======================================================================
# Progress and cancellation (the shape models.registry already uses)
# ======================================================================


class BackupPhase(StrEnum):
    SNAPSHOT = "snapshot"
    WRITING = "writing"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class RestorePhase(StrEnum):
    CHECKING = "checking"
    PREPARING = "preparing"
    APPLYING = "applying"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class BackupProgress:
    """One progress tick, for either direction.

    5.3 requires progress state during a restore, and F-64's download report
    already taught the GUI to read a phase, a label, and two running totals;
    this is the same shape so the same widget can show it.
    """

    phase: BackupPhase | RestorePhase
    item: str
    items_done: int
    items_total: int
    bytes_done: int
    bytes_total: int

    @property
    def fraction(self) -> float:
        if self.bytes_total > 0:
            return min(1.0, self.bytes_done / self.bytes_total)
        if self.items_total > 0:
            return min(1.0, self.items_done / self.items_total)
        return 1.0


ProgressCallback = Callable[[BackupProgress], None]
FreeSpace = Callable[[Path], int]


def _cancelled(token: CancelToken | None) -> bool:
    return token is not None and token.cancelled


# ======================================================================
# 5.3: while a restore runs, new generation and edits are blocked
# ======================================================================


class RestoreGate:
    """The flag the rest of the app checks before it writes anything.

    5.3 blocks new generation and edits for the duration of a restore.  The
    block lives here rather than in the job engine because the restore is what
    knows when it starts and ends, and because the same answer has to serve the
    engine, the GUI, and the REST surface.  It is deliberately a plain object
    with an explicit lifetime: a module-level boolean nobody clears on an
    exception is how an app ends up permanently refusing to generate.
    """

    __slots__ = ("_lock", "_active", "_progress")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._progress: BackupProgress | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def progress(self) -> BackupProgress | None:
        """5.3's "progress state is provided", readable from another thread."""
        with self._lock:
            return self._progress

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._lock:
            if self._active:
                raise EchoActError(
                    Code.BUSY,
                    "A backup is already being restored.",
                    retry_after_s=WORKER_RELEASE_DEADLINE_S,
                )
            self._active = True
            self._progress = None
        try:
            yield
        finally:
            with self._lock:
                self._active = False
                self._progress = None

    def note(self, progress: BackupProgress) -> None:
        with self._lock:
            if self._active:
                self._progress = progress

    def require_idle(self, what: str = "This action") -> None:
        """Raise if a restore is running.  Retryable: it will finish."""
        if self.active:
            raise EchoActError(
                Code.BUSY,
                f"{what} is not possible while a backup is being restored.",
                retry_after_s=WORKER_RELEASE_DEADLINE_S,
            )


#: The process-wide gate.  One restore at a time, one flag for everyone.
RESTORE_GATE: Final = RestoreGate()


# ======================================================================
# What goes in, what came out
# ======================================================================


@dataclass(frozen=True, slots=True)
class BackupSelection:
    """F-44's "documents, history, and audio selected by the user".

    ``audio=False`` drops results *and* segments, not just the WAV files: a
    segment without audio is a row F-45 would immediately report as a missing
    result, so a bundle that kept them would restore into a library full of
    findings the user never caused.
    """

    documents: bool = True
    history: bool = True
    audio: bool = True
    document_ids: tuple[str, ...] | None = None
    job_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class BackupCounts:
    documents: int = 0
    jobs: int = 0
    segments: int = 0
    results: int = 0
    audio_files: int = 0

    @property
    def items(self) -> int:
        """4.1 counts items after decompression; this is what a restore adds."""
        return self.documents + self.jobs + self.segments + self.results + self.audio_files


@dataclass(frozen=True, slots=True)
class BackupOutcome:
    path: Path
    kind: str
    created_at: float
    counts: BackupCounts
    byte_size: int
    uncompressed_bytes: int
    #: Results whose audio file was no longer on disk.  There is nothing to
    #: bundle, so neither the file nor the row is in the archive.
    missing_results: tuple[str, ...]
    #: Results whose file no longer matched 4.2's stored digest.  The bytes on
    #: disk are what the user has, so they are bundled and the archive records
    #: their true digest; F-45's finding is reported rather than acted on here.
    corrupt_results: tuple[str, ...]
    cancelled: bool
    record: BackupRecord | None

    @property
    def item_count(self) -> int:
        return self.counts.items

    @property
    def contains_body_text(self) -> bool:
        """F-44's disclosure, as a fact about this file."""
        return bool(self.counts.documents or self.counts.jobs)

    @property
    def contains_audio(self) -> bool:
        return bool(self.counts.audio_files)


@dataclass(frozen=True, slots=True)
class BackupInspection:
    """What verification learned, before a single row is applied (N-27)."""

    path: Path
    format_version: int
    app_version: str
    schema_version: int
    created_at: float
    kind: str
    counts: BackupCounts
    uncompressed_bytes: int
    compressed_bytes: int
    retention_bytes: int
    disclosure: str
    deep: bool

    @property
    def item_count(self) -> int:
        return self.counts.items

    @property
    def contains_body_text(self) -> bool:
        return bool(self.counts.documents or self.counts.jobs)

    @property
    def contains_audio(self) -> bool:
        return bool(self.counts.audio_files)


class RestoreMode(StrEnum):
    """F-44: restore adds new items *by default*."""

    ADD = "add"
    #: Everything the app manages is deleted first.  N-15 governs this path:
    #: the existing data is read and bundled into a recoverable backup, which
    #: is what "validated before being overwritten" means in practice.
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class RestoredItem:
    """F-44's "original identifiers are distinguished from post-restore ones"."""

    kind: str
    original_id: str
    restored_id: str


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    source: Path
    mode: RestoreMode
    counts: BackupCounts
    items: tuple[RestoredItem, ...]
    owner_client_id: str
    safety_backup: Path | None
    cancelled: bool

    def restored_id(self, original_id: str) -> str | None:
        for item in self.items:
            if item.original_id == original_id:
                return item.restored_id
        return None

    def originals(self, kind: str) -> tuple[str, ...]:
        return tuple(i.original_id for i in self.items if i.kind == kind)


# ======================================================================
# Small helpers
# ======================================================================


def _moment(at: float | None) -> float:
    return ids.now() if at is None else at


def _disk_free(target: Path) -> int:
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        # An unreadable device is not evidence of a full one; the write will
        # report the truth.  4.1's warning is a guard, not the only check.
        return 1 << 62


def _require_space(target: Path, needed: int, free_space: FreeSpace) -> None:
    """4.1: below 1 GB free, backups are not started."""
    free = free_space(target)
    if free < needed + LOW_SPACE_WARNING_BYTES:
        raise EchoActError(
            Code.STORAGE_FULL,
            "There is not enough free disk space for this backup.",
            detail={"needed_bytes": needed, "free_bytes": free},
        )


def _chunks[T](values: Sequence[T], size: int = 400) -> Iterator[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _json_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _as_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EchoActError(Code.BACKUP_INVALID, f"The backup's {what} is not an object.")
    return value


def scheduled_backup_name(at: float, *, kind: str = "scheduled") -> str:
    """A sortable, collision-free file name for an automatic backup (F-74)."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(at))
    return f"echoact-{kind}-{stamp}-{ids.backup_id()}{BACKUP_SUFFIX}"


# ======================================================================
# The point-in-time snapshot (N-27, 5.3)
# ======================================================================


class _Snapshot:
    """One read transaction, held open for the life of the bundle.

    Opened on a connection of its own rather than borrowing the store's:
    ``Store`` hands out one connection per thread and other work on this thread
    would join -- and eventually commit -- the transaction the snapshot depends
    on.  In write-ahead mode this reader sees the database as it was when the
    transaction began and never blocks a writer, which is precisely 5.3's
    "a document changed during backup contributes its state at the start".
    """

    __slots__ = ("_conn", "_path")

    def __init__(self, path: Path, *, busy_timeout_s: float) -> None:
        self._path = path
        try:
            conn = sqlite3.connect(
                path, timeout=busy_timeout_s, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_s * 1000)}")
            conn.execute("PRAGMA query_only = 1")
            conn.execute("BEGIN DEFERRED")
            # A deferred transaction takes its read snapshot at the first read,
            # not at BEGIN, so the snapshot has to be pinned here rather than
            # at whichever table happens to be queried first.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc
        self._conn = conn

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        rows = self.query(sql, params)
        return rows[0][0] if rows else None

    def close(self) -> None:
        with suppress(sqlite3.Error):
            self._conn.execute("ROLLBACK")
        with suppress(sqlite3.Error):
            self._conn.close()

    def __enter__(self) -> _Snapshot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ======================================================================
# Writing a bundle (F-44)
# ======================================================================


class _MemberWriter:
    """A zip member that hashes what it is given as it goes.

    Always used as a context manager, and that is load-bearing rather than
    tidiness.  ``ZipFile.close`` refuses outright -- with a ``ValueError``,
    raised *before* it releases the file object -- while a writing handle is
    still open on it.  So a read error part way through a WAV, or an
    ``EchoActError`` from the snapshot part way through a table, would unwind
    through ``__exit__`` and come out as that ``ValueError`` instead: the real
    cause replaced by a builtin no caller has code for (rule 3), the archive's
    handle leaked, and -- on Windows, where an open file cannot be unlinked --
    the half-written ``.part-`` file left in the directory the user chose,
    which is exactly what :func:`create_backup` promises never to do.
    """

    __slots__ = ("_raw", "_hash", "bytes_written")

    def __init__(self, zf: zipfile.ZipFile, name: str, *, force_zip64: bool = False) -> None:
        self._raw = zf.open(name, "w", force_zip64=force_zip64)
        self._hash = hashlib.sha256()
        self.bytes_written = 0

    def write(self, data: bytes) -> None:
        self._raw.write(data)
        self._hash.update(data)
        self.bytes_written += len(data)

    def close(self) -> tuple[str, int]:
        self._raw.close()
        return self._hash.hexdigest(), self.bytes_written

    def __enter__(self) -> _MemberWriter:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        if exc_type is None:
            return
        # Closing a member whose write has just failed can fail in turn, and
        # the failure already unwinding is the one worth reporting.  The
        # handle is released either way: zipfile clears its writing flag in a
        # finally, so the archive can still be closed and the file unlinked.
        with suppress(Exception):
            self._raw.close()


def _resolve_inside(root: Path, relative_path: str) -> Path | None:
    """Resolve a stored relative path, or ``None`` if it leaves the root.

    The same rule ``Store`` applies when it reads a result, repeated here
    because this is the module that would otherwise copy the escaping file
    into a bundle.  N-27 and 4.2 together mean a stored path must never be
    able to pull a credential or a model file into a backup.
    """
    if not relative_path:
        return None
    try:
        candidate = (root / relative_path).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _own_limits(counts: BackupCounts, uncompressed: int) -> tuple[int, int]:
    """Ceilings for reading back a bundle this module has just written.

    4.1 caps what a *restore* may decompress and apply.  It says nothing about
    what a library may hold, and F-44 and F-74 promise a backup of whatever it
    does hold.  Holding a freshly written bundle to the restore ceiling
    enforces 4.1 on the side it does not govern while leaving the side it does
    unguarded: about a hundred long retained jobs is 100,000 segments, and past
    that line every manual backup fails, the scheduled backup fails on every
    tick for ever, and the N-15 copy a replace takes of the live library cannot
    be written either -- so the data is unbackupable exactly when there is most
    of it.  A file written from the live database seconds ago is not hostile
    input; the self-check is looking for a bad write, so it is run against what
    was actually written.
    """
    # The archive holds one entry per audio file, one per table, and the
    # manifest; ``items`` counts audio and result rows separately, so this is
    # never the binding figure, but it is stated rather than assumed.
    entries = counts.audio_files + len(_TABLE_MEMBERS) + 1
    return (
        max(BACKUP_RESTORE_MAX_BYTES, uncompressed + _MAX_MANIFEST_BYTES),
        max(BACKUP_RESTORE_MAX_ITEMS, counts.items, entries),
    )


def create_backup(
    store: Store,
    destination: str | os.PathLike[str],
    *,
    selection: BackupSelection | None = None,
    kind: str = "manual",
    at: float | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    free_space: FreeSpace = _disk_free,
    overwrite: bool = False,
    record: bool = True,
    deep_verify: bool = True,
    gate: RestoreGate | None = None,
) -> BackupOutcome:
    """Write one bundle from one point in time (F-44, N-27, 5.3).

    The bundle is built at a temporary name beside the destination and moved
    into place only once it has been verified, so a cancelled or failed backup
    never leaves a plausible-looking file where the user asked for a good one --
    and, for F-74, never leaves one that rotation would later count as sound.
    """
    sel = selection or BackupSelection()
    (gate or RESTORE_GATE).require_idle("Taking a backup")
    moment = _moment(at)
    target = Path(destination)
    if target.exists() and not overwrite:
        raise EchoActError(
            Code.BACKUP_INVALID,
            "A file already exists at that location.",
            detail={"path": redact(target)},
        )

    with _Snapshot(store.path, busy_timeout_s=store.busy_timeout_s) as snap:
        plan = _plan(snap, sel)
        _require_space(target.parent, plan.audio_bytes + plan.text_bytes, free_space)
        _emit(
            progress,
            BackupPhase.SNAPSHOT,
            "snapshot",
            0,
            plan.counts.items,
            0,
            plan.audio_bytes,
        )
        temp = target.with_name(target.name + f".part-{ids.backup_id()}")
        try:
            counts, missing, corrupt, uncompressed = _write_archive(
                snap, store, temp, plan, moment, kind, progress, cancel
            )
        except BaseException:
            _discard(temp)
            raise
        if _cancelled(cancel):
            _discard(temp)
            return BackupOutcome(
                path=target,
                kind=kind,
                created_at=moment,
                counts=BackupCounts(),
                byte_size=0,
                uncompressed_bytes=0,
                missing_results=missing,
                corrupt_results=corrupt,
                cancelled=True,
                record=None,
            )

    _emit(progress, BackupPhase.VERIFYING, target.name, counts.items, counts.items, 0, 0)
    self_max_bytes, self_max_items = _own_limits(counts, uncompressed)
    try:
        verify_backup(temp, deep=deep_verify, max_bytes=self_max_bytes, max_items=self_max_items)
    except EchoActError:
        _discard(temp)
        raise
    try:
        os.replace(temp, target)
    except OSError as exc:
        _discard(temp)
        raise EchoActError(
            Code.STORAGE_FULL if getattr(exc, "errno", None) == 28 else Code.BACKUP_INVALID,
            "The backup could not be moved into place.",
            detail={"path": redact(target)},
            cause=exc,
        ) from exc

    size = target.stat().st_size
    written: BackupRecord | None = None
    if record:
        written = store.record_backup(
            location=str(target),
            kind=kind,
            byte_size=size,
            item_count=counts.items,
            verified=True,
            note=json.dumps({"format": BACKUP_FORMAT_VERSION, "audio": counts.audio_files}),
            at=moment,
        )
    log.info(
        "backup written kind=%s items=%d bytes=%d path=%s",
        kind,
        counts.items,
        size,
        redact(target),
    )
    _emit(progress, BackupPhase.COMPLETE, target.name, counts.items, counts.items, size, size)
    return BackupOutcome(
        path=target,
        kind=kind,
        created_at=moment,
        counts=counts,
        byte_size=size,
        uncompressed_bytes=uncompressed,
        missing_results=missing,
        corrupt_results=corrupt,
        cancelled=False,
        record=written,
    )


@dataclass(frozen=True, slots=True)
class _Plan:
    document_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    #: Empty when audio was not selected: a segment's meaning is the audio it
    #: points at, so the two travel together or not at all.
    segment_job_ids: tuple[str, ...]
    result_rows: tuple[sqlite3.Row, ...]
    counts: BackupCounts
    audio_bytes: int
    text_bytes: int


def _plan(snap: _Snapshot, sel: BackupSelection) -> _Plan:
    """Decide what is in the bundle, entirely from one snapshot.

    Two exclusions are requirements rather than choices.  5.3 keeps the
    temporary results of in-progress jobs out, and an in-progress job restored
    without them would sit in a state 5.1 says can never be advanced -- so the
    whole job is left out, not merely its result.  4.1 makes one-off jobs
    temporary by definition, so "history" means retained history.
    """
    documents: tuple[str, ...] = ()
    if sel.documents:
        rows = snap.query("SELECT document_id FROM documents ORDER BY created_at")
        documents = tuple(r["document_id"] for r in rows)
        if sel.document_ids is not None:
            wanted = set(sel.document_ids)
            documents = tuple(d for d in documents if d in wanted)

    jobs: tuple[str, ...] = ()
    if sel.history:
        marks = ",".join("?" * len(_TERMINAL_STATE_VALUES))
        rows = snap.query(
            f"SELECT job_id FROM jobs WHERE retention = ? AND state IN ({marks})"
            " ORDER BY created_at",
            (RetentionMode.RETAINED.value, *_TERMINAL_STATE_VALUES),
        )
        jobs = tuple(r["job_id"] for r in rows)
        if sel.job_ids is not None:
            wanted = set(sel.job_ids)
            jobs = tuple(j for j in jobs if j in wanted)

    segments = 0
    results: list[sqlite3.Row] = []
    if jobs and sel.audio:
        for chunk in _chunks(jobs):
            marks = ",".join("?" * len(chunk))
            segments += int(
                snap.scalar(
                    f"SELECT count(*) FROM segments WHERE job_id IN ({marks})", tuple(chunk)
                )
                or 0
            )
            results.extend(
                snap.query(
                    "SELECT result_id, job_id, sample_rate, channels, sample_width_bits,"
                    " frame_count, byte_size, digest, relative_path, created_at, expires_at"
                    f" FROM results WHERE job_id IN ({marks}) AND expires_at IS NULL",
                    tuple(chunk),
                )
            )

    text_bytes = 0
    if documents:
        for chunk in _chunks(documents):
            marks = ",".join("?" * len(chunk))
            text_bytes += int(
                snap.scalar(
                    "SELECT coalesce(sum(length(CAST(title AS BLOB))"
                    f" + length(CAST(body AS BLOB))), 0) FROM documents WHERE document_id IN ({marks})",
                    tuple(chunk),
                )
                or 0
            )
    if jobs:
        for chunk in _chunks(jobs):
            marks = ",".join("?" * len(chunk))
            text_bytes += int(
                snap.scalar(
                    "SELECT coalesce(sum(length(CAST(source_text AS BLOB))), 0) FROM jobs"
                    f" WHERE job_id IN ({marks}) AND source_text IS NOT NULL",
                    tuple(chunk),
                )
                or 0
            )

    audio_bytes = sum(int(r["byte_size"]) for r in results)
    return _Plan(
        document_ids=documents,
        job_ids=jobs,
        segment_job_ids=jobs if sel.audio else (),
        result_rows=tuple(results),
        counts=BackupCounts(
            documents=len(documents),
            jobs=len(jobs),
            segments=segments,
            results=len(results),
            audio_files=len(results),
        ),
        audio_bytes=audio_bytes,
        text_bytes=text_bytes,
    )


def _write_archive(
    snap: _Snapshot,
    store: Store,
    temp: Path,
    plan: _Plan,
    moment: float,
    kind: str,
    progress: ProgressCallback | None,
    cancel: CancelToken | None,
) -> tuple[BackupCounts, tuple[str, ...], tuple[str, ...], int]:
    """Members in dependency order: audio, then tables, then the manifest.

    Audio goes first because a zip member cannot be withdrawn once written:
    which results are in the bundle is only known after their files have been
    copied, and the results table has to agree with that.  The manifest goes
    last because it carries the digests of everything before it.

    Each result row is exported with the digest of the bytes that went into
    the archive rather than the one the database held.  Normally they are the
    same.  When they are not, the file is what the user actually has, and an
    archive whose recorded digest disagreed with its own contents could never
    be restored -- so the bundle stays internally consistent and the
    disagreement is reported instead.
    """
    members: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    corrupt: list[str] = []
    audio_root = store.audio_root
    done_bytes = 0
    done_items = 0
    total_items = plan.counts.items
    good_results: list[tuple[sqlite3.Row, str, int]] = []
    uncompressed = 0

    temp.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for row in plan.result_rows:
                if _cancelled(cancel):
                    return BackupCounts(), tuple(missing), tuple(corrupt), 0
                result_id = str(row["result_id"])
                if not _SAFE_ID.match(result_id):
                    raise EchoActError(
                        Code.BACKUP_INVALID,
                        "A stored result identifier is not a safe file name.",
                        detail={"result_id": result_id[:32]},
                    )
                source = _resolve_inside(audio_root, str(row["relative_path"] or ""))
                if source is None:
                    # A row pointing outside the audio directory is how a
                    # credential or a model file would end up in a bundle
                    # (N-27, 4.2).  It is a corrupt database, not a missing
                    # file, and the safe answer is to write nothing at all.
                    raise EchoActError(
                        Code.BACKUP_INVALID,
                        "A stored audio path points outside the audio directory; "
                        "no backup was written.",
                        detail={"result_id": result_id},
                    )
                name = f"{AUDIO_PREFIX}{result_id}.wav"
                digest, size = _copy_into(zf, name, source, cancel)
                if _cancelled(cancel):
                    return BackupCounts(), tuple(missing), tuple(corrupt), 0
                if digest is None:
                    missing.append(result_id)
                    continue
                if digest != str(row["digest"]):
                    corrupt.append(result_id)
                members[name] = {"sha256": digest, "bytes": size}
                good_results.append((row, digest, size))
                uncompressed += size
                done_bytes += size
                done_items += 1
                _emit(
                    progress,
                    BackupPhase.WRITING,
                    name,
                    done_items,
                    total_items,
                    done_bytes,
                    plan.audio_bytes,
                )

            for name, rows in (
                (DOCUMENTS_MEMBER, _document_records(snap, plan.document_ids)),
                (JOBS_MEMBER, _job_records(snap, plan.job_ids)),
                (SEGMENTS_MEMBER, _segment_records(snap, plan.segment_job_ids)),
                (RESULTS_MEMBER, _result_records(good_results)),
            ):
                with _MemberWriter(zf, name) as writer:
                    count = 0
                    for payload in rows:
                        writer.write(_json_line(payload))
                        count += 1
                    digest, size = writer.close()
                members[name] = {"sha256": digest, "bytes": size, "records": count}
                uncompressed += size
                done_items += count
                _emit(
                    progress,
                    BackupPhase.WRITING,
                    name,
                    min(done_items, total_items),
                    total_items,
                    done_bytes,
                    plan.audio_bytes,
                )

            counts = BackupCounts(
                documents=int(members[DOCUMENTS_MEMBER]["records"]),
                jobs=int(members[JOBS_MEMBER]["records"]),
                segments=int(members[SEGMENTS_MEMBER]["records"]),
                results=int(members[RESULTS_MEMBER]["records"]),
                audio_files=len(good_results),
            )
            manifest = {
                "format": BACKUP_FORMAT_VERSION,
                "app_version": __version__,
                "schema_version": store.schema_version,
                "created_at": moment,
                "kind": kind,
                "counts": {
                    "documents": counts.documents,
                    "jobs": counts.jobs,
                    "segments": counts.segments,
                    "results": counts.results,
                    "audio_files": counts.audio_files,
                },
                "item_count": counts.items,
                "uncompressed_bytes": uncompressed,
                "retention_bytes": plan.text_bytes + sum(size for _row, _d, size in good_results),
                "members": members,
                "disclosure": DISCLOSURE,
                "excluded": list(EXCLUDED_FROM_BACKUP),
            }
            with _MemberWriter(zf, MANIFEST_NAME) as writer:
                writer.write(json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
                writer.close()
    except OSError as exc:
        raise EchoActError(
            Code.STORAGE_FULL if getattr(exc, "errno", None) == 28 else Code.BACKUP_INVALID,
            "The backup could not be written.",
            detail={"path": redact(temp)},
            cause=exc,
        ) from exc
    return counts, tuple(missing), tuple(corrupt), uncompressed


def _copy_into(
    zf: zipfile.ZipFile, name: str, source: Path, cancel: CancelToken | None
) -> tuple[str | None, int]:
    """Stream one WAV in, hashing it.  ``None`` means the file is gone."""
    try:
        handle = source.open("rb")
    except FileNotFoundError:
        return None, 0
    except OSError as exc:
        raise EchoActError(
            Code.BACKUP_INVALID,
            "An audio file could not be read for the backup.",
            detail={"path": redact(source)},
            cause=exc,
        ) from exc
    with handle, _MemberWriter(zf, name, force_zip64=True) as writer:
        while True:
            if _cancelled(cancel):
                writer.close()
                return None, 0
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
        return writer.close()


def _document_records(snap: _Snapshot, ids_: Sequence[str]) -> Iterator[dict[str, Any]]:
    for chunk in _chunks(list(ids_)):
        marks = ",".join("?" * len(chunk))
        for row in snap.query(
            "SELECT document_id, title, body, created_at, modified_at, version"
            f" FROM documents WHERE document_id IN ({marks}) ORDER BY created_at",
            tuple(chunk),
        ):
            yield dict(row)


def _job_records(snap: _Snapshot, ids_: Sequence[str]) -> Iterator[dict[str, Any]]:
    for chunk in _chunks(list(ids_)):
        marks = ",".join("?" * len(chunk))
        for row in snap.query(
            "SELECT job_id, kind, request_path, state, retention, source_text, model_id,"
            " settings_json, budget_json, created_at, started_at, ended_at, error_code,"
            " error_message, generated_segments, total_segments"
            f" FROM jobs WHERE job_id IN ({marks}) ORDER BY created_at",
            tuple(chunk),
        ):
            # owner_client_id, client_label and idempotency_key are absent by
            # design: F-44 gives restored data to the GUI owner and restores no
            # external client's permissions, and 4.2 keeps re-request records
            # per client with their own expiry.
            yield dict(row)


def _segment_records(snap: _Snapshot, ids_: Sequence[str]) -> Iterator[dict[str, Any]]:
    for chunk in _chunks(list(ids_)):
        marks = ",".join("?" * len(chunk))
        for row in snap.query(
            "SELECT segment_id, job_id, seq, source_start_codepoint_inclusive,"
            " source_end_codepoint_exclusive, spoken_text, language, audio_start_ms,"
            " audio_end_ms, trailing_silence_ms, frame_count, ready"
            f" FROM segments WHERE job_id IN ({marks}) ORDER BY job_id, seq",
            tuple(chunk),
        ):
            # audio_path is not exported: per-segment audio lives in the
            # scratch tree that N-02 clears on relaunch, so it is never part
            # of a durable bundle.  The full result WAV carries the audio.
            yield dict(row)


def _result_records(
    rows: Iterable[tuple[sqlite3.Row, str, int]],
) -> Iterator[dict[str, Any]]:
    for row, digest, size in rows:
        payload = dict(row)
        # The path inside the bundle is derived, never stored: a restore picks
        # its own destination and must not be steered by a recorded path.
        payload.pop("relative_path", None)
        payload["digest"] = digest
        payload["byte_size"] = size
        payload["audio_member"] = f"{AUDIO_PREFIX}{row['result_id']}.wav"
        yield payload


def _emit(
    cb: ProgressCallback | None,
    phase: BackupPhase | RestorePhase,
    item: str,
    items_done: int,
    items_total: int,
    bytes_done: int,
    bytes_total: int,
) -> BackupProgress:
    tick = BackupProgress(
        phase=phase,
        item=item,
        items_done=items_done,
        items_total=items_total,
        bytes_done=bytes_done,
        bytes_total=bytes_total,
    )
    if cb is not None:
        cb(tick)
    return tick


def _discard(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


# ======================================================================
# Reading a bundle: every check happens before anything is applied (N-27)
# ======================================================================


def _reject(message: str, **detail: Any) -> EchoActError:
    return EchoActError(Code.BACKUP_INVALID, message, detail=detail)


def _check_member_name(name: str) -> None:
    """Refuse any name that could address a file outside the restore target.

    Each clause is a real attack and not a restatement of the next: a relative
    escape, an absolute POSIX path, a Windows drive-qualified path, a UNC
    share, a backslash separator that only Windows honours, and a name that
    normalises out of the tree.  The allow-list at the end would reject all of
    them on its own; they are checked separately so that a bad archive is
    reported for the reason it is bad, and so that widening the allow-list
    later cannot quietly widen the escape surface too.
    """
    if not name or "\x00" in name:
        raise _reject("The backup contains an unnamed entry.")
    if "\\" in name:
        raise _reject("The backup contains a Windows path separator.", entry=name[:80])
    if name.startswith("/"):
        raise _reject("The backup contains an absolute path.", entry=name[:80])
    if _DRIVE_LETTER.match(name):
        raise _reject("The backup contains a drive-qualified path.", entry=name[:80])
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _reject("The backup contains a relative path escape.", entry=name[:80])
    if PurePosixPath(name).as_posix() != name:
        raise _reject("The backup contains a path that does not normalise.", entry=name[:80])
    if name == MANIFEST_NAME or name in _TABLE_MEMBERS:
        return
    if name.startswith(AUDIO_PREFIX) and _SAFE_AUDIO_NAME.match(name[len(AUDIO_PREFIX) :]):
        return
    raise _reject("The backup contains an unexpected entry.", entry=name[:80])


def _check_member_kind(info: zipfile.ZipInfo) -> None:
    """Refuse anything that is not a plain file.

    A zip entry carries a unix mode in the high half of ``external_attr``; a
    symlink entry is the classic way to make an extractor write through to a
    path the archive never names.  Directory entries are refused too: this
    format writes none, and creating one is not something a restore needs.

    Only the file-type bits are consulted.  An entry written on a system that
    records permissions but no type -- which includes Python's own writer, so
    it includes every archive this module produces -- leaves them zero, and
    reading that as "not a regular file" would reject our own bundles.
    """
    if info.is_dir():
        raise _reject("The backup contains a directory entry.", entry=info.filename[:80])
    kind_bits = (info.external_attr >> 16) & 0o170000
    if kind_bits and kind_bits != stat.S_IFREG:
        kind = "symbolic link" if kind_bits == stat.S_IFLNK else "special file"
        raise _reject(f"The backup contains a {kind}.", entry=info.filename[:80])


@contextmanager
def _member_stream(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Iterator[Any]:
    """Open one member, or reject the archive -- with no third outcome.

    ``zipfile`` answers a hostile header with a builtin, not with
    ``BadZipFile``: ``RuntimeError`` for an entry whose encryption flag is
    set, ``NotImplementedError`` for a compression method this build does not
    have, and whatever the decompressor for a *supported* method raises for a
    stream that is not one.  Catching those by name is catching the ones we
    happened to think of, and every one of them escaping as a builtin means a
    caller that handles :class:`EchoActError` -- the chooser, the service --
    sees an unhandled crash instead of "this backup is corrupt" (rule 3,
    N-27).  Every way an archive can fail to decode means one thing here, so
    the whole decode is funnelled through this one place and answered with one
    code.  ``EchoActError`` is let through: our own rejections are already the
    answer, and re-wrapping one would lose the code that says why.
    """
    try:
        raw = zf.open(info, "r")
    except EchoActError:
        raise
    except Exception as exc:
        raise _reject(
            "The backup contains an entry that cannot be read.", entry=info.filename[:80]
        ) from exc
    try:
        yield raw
    finally:
        with suppress(Exception):
            raw.close()


def _bounded_read(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> Iterator[bytes]:
    """Stream a member, refusing to keep going past ``limit``.

    The declared size in the header is not evidence.  4.1 caps what a restore
    may decompress, so the cap is enforced against bytes actually produced.
    """
    read = 0
    with _member_stream(zf, info) as raw:
        while True:
            try:
                chunk = raw.read(_CHUNK_BYTES)
            except EchoActError:
                raise
            except Exception as exc:
                raise _reject(
                    "The backup contains an entry that cannot be read.", entry=info.filename[:80]
                ) from exc
            if not chunk:
                return
            read += len(chunk)
            if read > limit:
                raise EchoActError(
                    Code.BACKUP_TOO_LARGE,
                    "The backup decompresses to more than the restore limit allows.",
                    detail={"limit_bytes": limit, "entry": info.filename[:80]},
                )
            yield chunk


def _member_digest(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> tuple[str, int, int]:
    """The member's digest, its true size, and how many lines it really holds.

    The line count is what finally binds a table's declared row count to its
    contents.  It counts newlines instead of parsing, and rounds an
    unterminated final line up, so it can only ever over-count against
    :func:`_iter_records` -- and an over-count is a rejected archive, never a
    row applied past 4.1's ceiling.
    """
    h = hashlib.sha256()
    size = 0
    lines = 0
    terminated = True
    for chunk in _bounded_read(zf, info, limit):
        h.update(chunk)
        size += len(chunk)
        lines += chunk.count(b"\n")
        terminated = chunk.endswith(b"\n")
    if size and not terminated:
        lines += 1
    return h.hexdigest(), size, lines


def _iter_records(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int
) -> Iterator[dict[str, Any]]:
    buffer = b""
    for chunk in _bounded_read(zf, info, limit):
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line, buffer = buffer[:newline], buffer[newline + 1 :]
            if line.strip():
                yield _decode_record(line, info.filename)
        if len(buffer) > _MAX_RECORD_BYTES:
            raise _reject("The backup contains an unterminated record.", entry=info.filename[:80])
    if buffer.strip():
        yield _decode_record(buffer, info.filename)


def _decode_record(line: bytes, member: str) -> dict[str, Any]:
    try:
        return _as_object(json.loads(line.decode("utf-8")), f"{member} record")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _reject("The backup contains a record that is not valid JSON.", entry=member) from exc


def inspect_backup(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = BACKUP_RESTORE_MAX_BYTES,
    max_items: int = BACKUP_RESTORE_MAX_ITEMS,
) -> BackupInspection:
    """Structure, compatibility, and 4.1's limits -- without reading a member.

    Separate from :func:`verify_backup` so a chooser dialog can describe a file
    (F-44's disclosure, its size, its age) without paying to hash it.
    """
    return _inspect(Path(path), max_bytes=max_bytes, max_items=max_items, deep=False, digest=False)


def verify_backup(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = BACKUP_RESTORE_MAX_BYTES,
    max_items: int = BACKUP_RESTORE_MAX_ITEMS,
    deep: bool = False,
    cancel: CancelToken | None = None,
) -> BackupInspection:
    """N-27's "integrity and compatibility are verified before restore".

    Shallow verification reads and hashes the table members; ``deep`` also
    reads every audio member and checks it against 4.2's stored digest, which
    is what F-74 means by a *sound* backup before it rotates older ones away.
    """
    return _inspect(
        Path(path), max_bytes=max_bytes, max_items=max_items, deep=deep, digest=True, cancel=cancel
    )


def _inspect(
    path: Path,
    *,
    max_bytes: int,
    max_items: int,
    deep: bool,
    digest: bool,
    cancel: CancelToken | None = None,
) -> BackupInspection:
    if not path.is_file():
        raise EchoActError(
            Code.FILE_NOT_FOUND, "That backup file does not exist.", detail={"path": redact(path)}
        )
    compressed = path.stat().st_size
    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
            names = [i.filename for i in infos]
            if len(set(names)) != len(names):
                raise _reject("The backup names the same entry twice.")
            if len(infos) > max_items:
                raise EchoActError(
                    Code.BACKUP_TOO_LARGE,
                    "The backup holds more entries than a restore may apply.",
                    detail={"entries": len(infos), "limit": max_items},
                )
            declared = 0
            for info in infos:
                _check_member_name(info.filename)
                _check_member_kind(info)
                declared += max(0, int(info.file_size))
            if declared > max_bytes:
                raise EchoActError(
                    Code.BACKUP_TOO_LARGE,
                    "The backup decompresses to more than the restore limit allows.",
                    detail={"uncompressed_bytes": declared, "limit_bytes": max_bytes},
                )
            manifest = _read_manifest(zf, names)
            inspection = _check_manifest(
                path, manifest, zf, compressed, max_bytes=max_bytes, max_items=max_items
            )
            if digest:
                _verify_members(zf, manifest, max_bytes=max_bytes, deep=deep, cancel=cancel)
    except EchoActError:
        # Already the answer, and carrying the code that says why.
        raise
    except zipfile.BadZipFile as exc:
        raise _reject("The backup is not a readable archive.", path=redact(path)) from exc
    except Exception as exc:
        # Opening the directory of a crafted archive fails in as many ways as
        # reading a member does; the reasoning in _member_stream applies here.
        raise _reject("The backup could not be read.", path=redact(path)) from exc
    return replace(inspection, deep=deep and digest)


def _read_manifest(zf: zipfile.ZipFile, names: Sequence[str]) -> dict[str, Any]:
    if MANIFEST_NAME not in names:
        raise _reject("The backup has no manifest.")
    info = zf.getinfo(MANIFEST_NAME)
    if info.file_size > _MAX_MANIFEST_BYTES:
        raise _reject("The backup's manifest is implausibly large.")
    raw = b"".join(_bounded_read(zf, info, _MAX_MANIFEST_BYTES))
    try:
        return _as_object(json.loads(raw.decode("utf-8")), "manifest")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _reject("The backup's manifest is not valid JSON.") from exc


def _check_manifest(
    path: Path,
    manifest: dict[str, Any],
    zf: zipfile.ZipFile,
    compressed: int,
    *,
    max_bytes: int,
    max_items: int,
) -> BackupInspection:
    fmt = manifest.get("format")
    if not isinstance(fmt, int) or fmt != BACKUP_FORMAT_VERSION:
        raise EchoActError(
            Code.BACKUP_INCOMPATIBLE,
            "That backup was written in a format this version does not read.",
            detail={"found": fmt, "supported": BACKUP_FORMAT_VERSION},
        )
    schema = manifest.get("schema_version")
    if not isinstance(schema, int) or schema > SUPPORTED_SCHEMA_VERSION:
        # N-15: an older build must not touch a newer format at all.
        raise EchoActError(
            Code.BACKUP_INCOMPATIBLE,
            "That backup holds a newer data format than this version understands.",
            detail={"found": schema, "supported": SUPPORTED_SCHEMA_VERSION},
        )
    counts_raw = _as_object(manifest.get("counts", {}), "counts")
    counts = BackupCounts(
        documents=_non_negative(counts_raw.get("documents", 0), "documents"),
        jobs=_non_negative(counts_raw.get("jobs", 0), "jobs"),
        segments=_non_negative(counts_raw.get("segments", 0), "segments"),
        results=_non_negative(counts_raw.get("results", 0), "results"),
        audio_files=_non_negative(counts_raw.get("audio_files", 0), "audio_files"),
    )
    declared_items = _non_negative(manifest.get("item_count", counts.items), "item_count")
    if max(declared_items, counts.items) > max_items:
        raise EchoActError(
            Code.BACKUP_TOO_LARGE,
            "The backup holds more items than a restore may apply.",
            detail={"items": max(declared_items, counts.items), "limit": max_items},
        )
    uncompressed = _non_negative(manifest.get("uncompressed_bytes", 0), "uncompressed_bytes")
    retention = _non_negative(manifest.get("retention_bytes", 0), "retention_bytes")
    if uncompressed > max_bytes:
        raise EchoActError(
            Code.BACKUP_TOO_LARGE,
            "The backup decompresses to more than the restore limit allows.",
            detail={"uncompressed_bytes": uncompressed, "limit_bytes": max_bytes},
        )
    members = _as_object(manifest.get("members", {}), "member list")
    present = {i.filename for i in zf.infolist()} - {MANIFEST_NAME}
    listed = set(members)
    if present != listed:
        raise _reject(
            "The backup's manifest does not match its contents.",
            unlisted=sorted(present - listed)[:5],
            missing=sorted(listed - present)[:5],
        )
    audio_members = {n for n in present if n.startswith(AUDIO_PREFIX)}
    if len(audio_members) != counts.audio_files:
        raise _reject(
            "The backup declares a different number of audio files than it holds.",
            declared=counts.audio_files,
            found=len(audio_members),
        )
    _check_declarations(zf, members, counts, uncompressed, retention)
    return BackupInspection(
        path=path,
        format_version=fmt,
        app_version=str(manifest.get("app_version", "")),
        schema_version=schema,
        created_at=float(manifest.get("created_at", 0.0) or 0.0),
        kind=str(manifest.get("kind", "manual")),
        counts=counts,
        uncompressed_bytes=uncompressed,
        compressed_bytes=compressed,
        retention_bytes=retention,
        disclosure=str(manifest.get("disclosure", DISCLOSURE)),
        deep=False,
    )


def _check_declarations(
    zf: zipfile.ZipFile,
    members: dict[str, Any],
    counts: BackupCounts,
    uncompressed: int,
    retention: int,
) -> None:
    """Tie every figure 4.1 caps to something the archive cannot simply assert.

    The manifest is the one part of a bundle a forger writes freely, and until
    this existed it was the *only* source for the item count, the decompressed
    size and the retention cost: an archive holding a quarter of a million
    documents could declare one item and one byte, pass verification, and be
    applied in full -- with the free-space and retention guards run against the
    lie and 4.1's 100,000-item ceiling never touching a row that was actually
    inserted.  Only the audio file count was bound to reality.

    So each per-member figure has to match the size the archive's own directory
    records, which is also the hard ceiling on how much ``zipfile`` will ever
    hand back for that entry; each aggregate has to be at least the sum of its
    parts; and each table's declared row count has to match the ``records``
    figure that :func:`_verify_members` then holds to the lines that really
    come out.  Understatement is what is refused, in both directions: a
    manifest may not make a bundle look smaller than it is.
    """
    total = 0
    audio_bytes = 0
    for name, raw_meta in members.items():
        meta = _as_object(raw_meta, "member entry")
        size = _non_negative(meta.get("bytes"), f"recorded size for {name[:80]}")
        if size != zf.getinfo(name).file_size:
            raise _reject("A backup entry is not the size the manifest records.", entry=name[:80])
        total += size
        if name.startswith(AUDIO_PREFIX):
            audio_bytes += size
        field_name = _COUNTED_MEMBERS.get(name)
        if field_name is None:
            continue
        rows = _non_negative(meta.get("records", 0), f"record count for {field_name}")
        if rows != getattr(counts, field_name):
            raise _reject(
                "The backup declares a different number of rows than its member holds.",
                entry=name[:80],
                declared=getattr(counts, field_name),
                listed=rows,
            )
    for name, field_name in _COUNTED_MEMBERS.items():
        if name not in members and getattr(counts, field_name):
            raise _reject("The backup declares rows in a member it does not contain.", entry=name)
    if uncompressed < total:
        raise _reject(
            "The backup understates what it decompresses to.", declared=uncompressed, found=total
        )
    if retention < audio_bytes:
        # Audio is stored verbatim, so its retention cost is exactly these
        # bytes; the text on top of it is what the manifest may still add.
        raise _reject(
            "The backup understates what restoring it would occupy.",
            declared=retention,
            found=audio_bytes,
        )


def _non_negative(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _reject(f"The backup's {what} is not a count.", value=str(value)[:40])
    return value


def _verify_members(
    zf: zipfile.ZipFile,
    manifest: dict[str, Any],
    *,
    max_bytes: int,
    deep: bool,
    cancel: CancelToken | None,
) -> None:
    members = _as_object(manifest.get("members", {}), "member list")
    for name, raw_meta in members.items():
        if _cancelled(cancel):
            return
        if name.startswith(AUDIO_PREFIX) and not deep:
            continue
        meta = _as_object(raw_meta, "member entry")
        found, size, lines = _member_digest(zf, zf.getinfo(name), max_bytes)
        if found != meta.get("sha256"):
            raise _reject("The backup's contents do not match its manifest.", entry=name[:80])
        recorded = meta.get("bytes")
        if isinstance(recorded, int) and recorded != size:
            raise _reject("A backup entry is not the size the manifest records.", entry=name[:80])
        if name in _COUNTED_MEMBERS and lines != meta.get("records"):
            # A digest only says the member is the one the manifest's author
            # meant to ship.  This says the manifest's row count -- which
            # _check_manifest has already tied to the counts 4.1 caps -- is
            # the number of rows a restore would actually insert.
            raise _reject(
                "A backup entry does not hold the number of rows the manifest records.",
                entry=name[:80],
                recorded=meta.get("records"),
                found=lines,
            )


# ======================================================================
# Restore (F-44, N-15, N-27, 5.3)
# ======================================================================


class _Cancelled(Exception):
    """Raised inside the apply transaction so SQLite rolls it back.

    Returning a "cancelled" outcome from inside ``store.transaction()`` would
    leave the context manager to commit everything written so far, which is the
    half-applied restore N-15 forbids.  Cancellation has to unwind.
    """


@dataclass(slots=True)
class _Applied:
    """Files written outside the transaction, so a rollback can undo them."""

    paths: list[Path] = field(default_factory=list)

    def undo(self) -> None:
        for path in self.paths:
            _discard(path)
        self.paths.clear()


@dataclass(slots=True)
class _ItemBudget:
    """4.1's item ceiling, counted against rows that are actually inserted.

    Verification refuses an over-large archive before a row is applied, and
    that is where this is normally decided.  This counts what really goes in,
    so the ceiling holds against a member whose contents a future check fails
    to bind to the manifest -- the transaction has not committed while it is
    counting, so exceeding it is still a refusal and not a partial restore.
    """

    limit: int
    used: int = 0

    def take(self, n: int = 1) -> None:
        self.used += n
        if self.used > self.limit:
            raise EchoActError(
                Code.BACKUP_TOO_LARGE,
                "The backup holds more items than a restore may apply.",
                detail={"items": self.used, "limit": self.limit},
            )


def restore_backup(
    store: Store,
    source: str | os.PathLike[str],
    *,
    owner_client_id: str,
    mode: RestoreMode = RestoreMode.ADD,
    max_bytes: int = BACKUP_RESTORE_MAX_BYTES,
    max_items: int = BACKUP_RESTORE_MAX_ITEMS,
    at: float | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    free_space: FreeSpace = _disk_free,
    gate: RestoreGate | None = None,
    safety_backup: bool = True,
) -> RestoreOutcome:
    """Apply a bundle, or apply none of it.

    The order is the requirement: verify (N-27), then check what applying would
    cost against 4.1's decompressed-size, item-count, retention and free-space
    limits, then -- only for :attr:`RestoreMode.REPLACE` -- secure a recoverable
    copy of the data about to be overwritten (N-15), and only then write.  Rows
    go in inside one transaction and audio files are tracked so that a failure
    or a cancellation anywhere leaves the library exactly as it was.

    Two things stand between REPLACE and the empty library that would be the
    worst possible answer to "restore from a corrupt database".  Verification
    is always deep, whatever it costs to hash the audio a second time: a
    shallow check leaves audio unread until ``_apply`` extracts it, which on
    this path is *after* ``_clear_library`` has committed, so a rotted bundle
    would be discovered only once the library it was to replace no longer
    existed.  And because the clear commits outside the transaction and some
    failures -- a bad record, a failing disk -- can only be met while applying,
    :func:`_put_back` restores the copy taken moments before, and the error
    says what became of the data either way.
    """
    the_gate = gate or RESTORE_GATE
    path = Path(source)
    moment = _moment(at)

    with the_gate.hold():
        tick = _emit(progress, RestorePhase.CHECKING, path.name, 0, 0, 0, 0)
        the_gate.note(tick)
        inspection = verify_backup(
            path, max_bytes=max_bytes, max_items=max_items, deep=True, cancel=cancel
        )
        if _cancelled(cancel):
            return _cancelled_restore(path, mode, owner_client_id, None)

        _require_space(store.audio_root, inspection.uncompressed_bytes, free_space)
        usage = store.storage_usage(at=moment)
        # What survives the restore, which for REPLACE is nothing: everything
        # this figure counts is deleted before a row of the bundle lands.
        # Adding the incoming bundle to it would refuse a restore because of
        # data that will not be there, so a library over half its allowance
        # could not be recovered from its own backup -- the one case REPLACE
        # exists for (Section 9, F-44).
        kept = 0 if mode is RestoreMode.REPLACE else usage.total_bytes
        if kept + inspection.retention_bytes > usage.limit_bytes:
            raise EchoActError(
                Code.RETENTION_LIMIT_REACHED,
                "Restoring this backup would exceed the retention limit.",
                detail={
                    "used_bytes": kept,
                    "limit_bytes": usage.limit_bytes,
                    "requested_bytes": inspection.retention_bytes,
                },
            )

        rescue: BackupOutcome | None = None
        cleared = False

        def past_recall() -> None:
            """``_clear_library`` has deleted something; see :func:`_put_back`."""
            nonlocal cleared
            cleared = True

        written = _Applied()
        try:
            if mode is RestoreMode.REPLACE:
                tick = _emit(progress, RestorePhase.PREPARING, "existing data", 0, 0, 0, 0)
                the_gate.note(tick)
                rescue = _secure_existing(store, moment, free_space) if safety_backup else None
                _clear_library(store, past_recall)
            return _apply(
                store,
                path,
                inspection,
                owner_client_id=owner_client_id,
                mode=mode,
                moment=moment,
                progress=progress,
                cancel=cancel,
                gate=the_gate,
                written=written,
                safety=_rescue_path(rescue),
                max_bytes=max_bytes,
                max_items=max_items,
            )
        except _Cancelled:
            written.undo()
            if cleared:
                _put_back(store, rescue, owner_client_id, moment, the_gate)
            _emit(progress, RestorePhase.CANCELLED, path.name, 0, inspection.item_count, 0, 0)
            return _cancelled_restore(path, mode, owner_client_id, _rescue_path(rescue))
        except EchoActError as exc:
            written.undo()
            if not cleared:
                raise
            raise _replace_failed(
                exc,
                _rescue_path(rescue),
                _put_back(store, rescue, owner_client_id, moment, the_gate),
            ) from exc
        except BaseException:
            written.undo()
            if cleared:
                _put_back(store, rescue, owner_client_id, moment, the_gate)
            raise


def _rescue_path(rescue: BackupOutcome | None) -> Path | None:
    return None if rescue is None else rescue.path


def _cancelled_restore(
    path: Path, mode: RestoreMode, owner: str, safety: Path | None
) -> RestoreOutcome:
    return RestoreOutcome(
        source=path,
        mode=mode,
        counts=BackupCounts(),
        items=(),
        owner_client_id=owner,
        safety_backup=safety,
        cancelled=True,
    )


def _secure_existing(store: Store, moment: float, free_space: FreeSpace) -> BackupOutcome:
    """N-15: the data about to be overwritten is read, bundled, and verified.

    Producing a full bundle rather than copying the database file is what makes
    this a *validation*: every row is read through one snapshot and every audio
    file is hashed against 4.2's stored digest on the way in, so a corrupt
    library is discovered before it is destroyed rather than after.

    The whole outcome is returned, not just the path, because it is also the
    input to :func:`_put_back`: how many rows and bytes it holds is what lets
    that read the file back without holding a bundle of the user's own library
    to 4.1's restore ceiling.
    """
    directory = default_backup_dir()
    destination = directory / scheduled_backup_name(moment, kind="pre-restore")
    return create_backup(
        store,
        destination,
        kind="pre_migration",
        at=moment,
        free_space=free_space,
        gate=_NULL_GATE,
        deep_verify=True,
    )


def _put_back(
    store: Store,
    rescue: BackupOutcome | None,
    owner_client_id: str,
    moment: float,
    gate: RestoreGate,
) -> bool:
    """Undo the one step of a REPLACE that the transaction cannot (N-15).

    ``_clear_library`` commits.  A row deletion and an unlinked WAV are not
    enrolled in the apply transaction and are not rolled back with it, so
    without this, *any* failure after that point -- a rotted audio member, a
    write error, a record the archive should never have contained -- leaves an
    empty library, which is precisely the outcome F-44, N-15 and Section 9 all
    forbid.  The bundle taken from the live library seconds earlier is applied
    back into what the clear left, which is normally nothing.

    Identifiers are minted afresh, as they are for any restore (F-44), and if
    the clear itself failed part way then whatever survived it is restored
    alongside itself: a duplicate the user can delete beats a job they cannot
    get back.  The whole of it is best effort by
    construction: it runs while another failure is unwinding and must never
    replace it, so what it managed is reported to the caller instead, which
    tells the user -- along with where the copy still on disk is.
    """
    if rescue is None or not rescue.path.is_file():
        return False
    own_bytes, own_items = _own_limits(rescue.counts, rescue.uncompressed_bytes)
    recovered = _Applied()
    try:
        inspection = verify_backup(rescue.path, max_bytes=own_bytes, max_items=own_items, deep=True)
        _apply(
            store,
            rescue.path,
            inspection,
            owner_client_id=owner_client_id,
            mode=RestoreMode.ADD,
            moment=moment,
            progress=None,
            cancel=None,
            gate=gate,
            written=recovered,
            safety=None,
            max_bytes=own_bytes,
            max_items=own_items,
        )
    except Exception:
        recovered.undo()
        log.error("restore rollback failed source=%s", redact(rescue.path))
        return False
    log.info(
        "restore rolled back documents=%d jobs=%d source=%s",
        rescue.counts.documents,
        rescue.counts.jobs,
        redact(rescue.path),
    )
    return True


def _replace_failed(original: EchoActError, safety: Path | None, put_back: bool) -> EchoActError:
    """Say what became of the existing data, not only why the restore failed.

    Section 9 ends at "corrupted data is never overwritten automatically", and
    a user left with an empty library and an error about a digest has no way to
    know a copy of everything is sitting in the data directory.  The original
    code is kept -- it is still why the restore was refused, and F-57 makes the
    code the contract -- while the message and detail gain the fate of what was
    there before and the path of the copy that still holds it.
    """
    if put_back:
        note = "The data that was there has been put back."
        fate = "restored"
    elif safety is not None:
        note = "The data that was there is in the copy taken before the restore started."
        fate = "in_safety_backup"
    else:
        note = "No copy of the data that was there was taken."
        fate = "lost"
    detail = dict(original.detail)
    detail["existing_data"] = fate
    if safety is not None:
        detail["safety_backup"] = redact(safety)
    return EchoActError(
        original.code,
        f"{original.message} {note}",
        detail=detail,
        retry_after_s=original.retry_after_s,
        cause=original,
    )


def _clear_library(store: Store, committed: Callable[[], None]) -> None:
    """Delete what a REPLACE restore is about to supersede.

    ``force`` is never passed: 5.3 requires a running job to be cancelled
    first, and ``delete_all_history`` refusing is exactly that rule -- and it
    refuses *before* it deletes anything, so a refusal costs the user nothing
    and needs no undoing.

    ``committed`` is called at the moment that stops being true.  Everything
    after it is outside the caller's apply transaction and outside any other,
    so a failure part way through here is as unrecoverable on its own as a
    failure during the apply: the caller has to put the data back itself
    (N-15), and this is how it is told that it must.
    """
    deletion = store.delete_all_history()
    committed()
    for relative in deletion.audio_paths:
        resolved = _resolve_inside(store.audio_root, relative)
        if resolved is not None:
            _discard(resolved)
    while True:
        page = store.list_documents(limit=100)
        if not page.items:
            return
        for summary in page.items:
            store.delete_document(summary.document_id)


def _apply(
    store: Store,
    path: Path,
    inspection: BackupInspection,
    *,
    owner_client_id: str,
    mode: RestoreMode,
    moment: float,
    progress: ProgressCallback | None,
    cancel: CancelToken | None,
    gate: RestoreGate,
    written: _Applied,
    safety: Path | None,
    max_bytes: int,
    max_items: int,
) -> RestoreOutcome:
    items: list[RestoredItem] = []
    job_map: dict[str, str] = {}
    counts = {"documents": 0, "jobs": 0, "segments": 0, "results": 0, "audio": 0}
    total = inspection.item_count
    budget = _ItemBudget(max_items)
    done = 0
    audio_root = store.audio_root
    restored_dir = audio_root / "restored"

    with zipfile.ZipFile(path, "r") as zf, store.transaction() as conn:
        names = {i.filename for i in zf.infolist()}

        for record in _records(zf, names, DOCUMENTS_MEMBER, max_bytes):
            if _cancelled(cancel):
                raise _Cancelled
            original = _text(record, "document_id")
            new_id = ids.document_id()
            conn.execute(
                "INSERT INTO documents (document_id, title, body, created_at, modified_at,"
                " version) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    _text(record, "title"),
                    _text(record, "body"),
                    _number(record, "created_at", moment),
                    _number(record, "modified_at", moment),
                    max(1, int(record.get("version") or 1)),
                ),
            )
            items.append(RestoredItem("document", original, new_id))
            counts["documents"] += 1
            budget.take()
            done += 1
            gate.note(_emit(progress, RestorePhase.APPLYING, "documents", done, total, 0, 0))

        for record in _records(zf, names, JOBS_MEMBER, max_bytes):
            if _cancelled(cancel):
                raise _Cancelled
            original = _text(record, "job_id")
            new_id = ids.job_id()
            job_map[original] = new_id
            conn.execute(
                "INSERT INTO jobs (job_id, kind, request_path, owner_client_id, client_label,"
                " state, retention, source_text, model_id, settings_json, budget_json,"
                " created_at, started_at, ended_at, error_code, error_message, idempotency_key,"
                " generated_segments, total_segments)"
                " VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    new_id,
                    _text(record, "kind"),
                    _text(record, "request_path"),
                    # F-44: restored data belongs to the GUI owner, and no
                    # external client's access is restored with it.
                    owner_client_id,
                    _state(record),
                    RetentionMode.RETAINED.value,
                    record.get("source_text"),
                    _text(record, "model_id"),
                    _text(record, "settings_json"),
                    record.get("budget_json"),
                    _number(record, "created_at", moment),
                    record.get("started_at"),
                    record.get("ended_at"),
                    record.get("error_code"),
                    record.get("error_message"),
                    max(0, int(record.get("generated_segments") or 0)),
                    max(0, int(record.get("total_segments") or 0)),
                ),
            )
            items.append(RestoredItem("job", original, new_id))
            counts["jobs"] += 1
            budget.take()
            done += 1
            gate.note(_emit(progress, RestorePhase.APPLYING, "history", done, total, 0, 0))

        for record in _records(zf, names, SEGMENTS_MEMBER, max_bytes):
            if _cancelled(cancel):
                raise _Cancelled
            job_id = job_map.get(_text(record, "job_id"))
            if job_id is None:
                raise _reject("The backup has a segment with no job.")
            conn.execute(
                "INSERT INTO segments (segment_id, job_id, seq,"
                " source_start_codepoint_inclusive, source_end_codepoint_exclusive,"
                " spoken_text, language, audio_start_ms, audio_end_ms, trailing_silence_ms,"
                " audio_path, frame_count, ready)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    ids.segment_id(),
                    job_id,
                    int(record.get("seq") or 0),
                    _span(record, "source_start_codepoint_inclusive"),
                    _span(record, "source_end_codepoint_exclusive"),
                    _text(record, "spoken_text", allow_empty=True),
                    _text(record, "language", allow_empty=True),
                    record.get("audio_start_ms"),
                    record.get("audio_end_ms"),
                    max(0, int(record.get("trailing_silence_ms") or 0)),
                    max(0, int(record.get("frame_count") or 0)),
                    1 if record.get("ready") else 0,
                ),
            )
            counts["segments"] += 1
            budget.take()
            done += 1
            gate.note(_emit(progress, RestorePhase.APPLYING, "segments", done, total, 0, 0))

        for record in _records(zf, names, RESULTS_MEMBER, max_bytes):
            if _cancelled(cancel):
                raise _Cancelled
            job_id = job_map.get(_text(record, "job_id"))
            if job_id is None:
                raise _reject("The backup has a result with no job.")
            new_id = ids.result_id()
            expected = _text(record, "digest")
            member = _text(record, "audio_member")
            # The member name is validated a second time on the way out of the
            # record, not only on the way in from the archive: this string came
            # from a JSON line, which the name checks never saw.
            _check_member_name(member)
            if member not in names:
                raise _reject("The backup names audio it does not contain.", entry=member[:80])
            destination = restored_dir / f"{new_id}.wav"
            written.paths.append(destination)
            size = _extract_audio(zf, member, destination, expected, cancel, max_bytes)
            relative = destination.relative_to(audio_root).as_posix()
            conn.execute(
                "INSERT INTO results (result_id, job_id, sample_rate, channels,"
                " sample_width_bits, frame_count, byte_size, digest, relative_path,"
                " created_at, expires_at, integrity_state, verified_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ok', ?)",
                (
                    new_id,
                    job_id,
                    int(record.get("sample_rate") or 0),
                    int(record.get("channels") or 0),
                    int(record.get("sample_width_bits") or 0),
                    max(0, int(record.get("frame_count") or 0)),
                    size,
                    expected,
                    relative,
                    _number(record, "created_at", moment),
                    moment,
                ),
            )
            counts["results"] += 1
            counts["audio"] += 1
            # A result is two of 4.1's items: the row and the file.
            budget.take(2)
            done += 1
            gate.note(_emit(progress, RestorePhase.APPLYING, member, done, total, size, size))

        usage = store.storage_usage(at=moment)
        if usage.total_bytes > usage.limit_bytes:
            # Checked again with the rows in place: the pre-check trusted the
            # manifest, and 4.1 refuses rather than deleting to make room.
            raise EchoActError(
                Code.RETENTION_LIMIT_REACHED,
                "Restoring this backup would exceed the retention limit.",
                detail={"used_bytes": usage.total_bytes, "limit_bytes": usage.limit_bytes},
            )

    log.info(
        "restore applied mode=%s documents=%d jobs=%d results=%d source=%s",
        mode.value,
        counts["documents"],
        counts["jobs"],
        counts["results"],
        redact(path),
    )
    final = BackupCounts(
        documents=counts["documents"],
        jobs=counts["jobs"],
        segments=counts["segments"],
        results=counts["results"],
        audio_files=counts["audio"],
    )
    _emit(progress, RestorePhase.COMPLETE, path.name, total, total, 0, 0)
    return RestoreOutcome(
        source=path,
        mode=mode,
        counts=final,
        items=tuple(items),
        owner_client_id=owner_client_id,
        safety_backup=safety,
        cancelled=False,
    )


def _records(
    zf: zipfile.ZipFile, names: set[str], member: str, max_bytes: int
) -> Iterator[dict[str, Any]]:
    if member not in names:
        return
    yield from _iter_records(zf, zf.getinfo(member), limit=max_bytes)


def _extract_audio(
    zf: zipfile.ZipFile,
    member: str,
    destination: Path,
    expected_digest: str,
    cancel: CancelToken | None,
    max_bytes: int,
) -> int:
    """Write one WAV to a name this module chose, and refuse a mismatch.

    The destination is never derived from the archive; ``member`` only says
    which bytes to read.  That is the second half of N-27's path defence: even
    if a name check were wrong, there is nowhere for a crafted name to land.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    info = zf.getinfo(member)
    try:
        with destination.open("wb") as out:
            for chunk in _bounded_read(zf, info, max_bytes):
                if _cancelled(cancel):
                    raise _Cancelled
                out.write(chunk)
                h.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise EchoActError(
            Code.STORAGE_FULL if getattr(exc, "errno", None) == 28 else Code.BACKUP_INVALID,
            "Restored audio could not be written.",
            detail={"path": redact(destination)},
            cause=exc,
        ) from exc
    if h.hexdigest() != expected_digest:
        raise _reject("Restored audio does not match its recorded digest.", entry=member[:80])
    return size


def _text(record: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = record.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise _reject(f"The backup has a record with no {key}.")
    return value


def _number(record: dict[str, Any], key: str, fallback: float) -> float:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return float(value)


def _span(record: dict[str, Any], key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _reject(f"The backup has a segment with a bad {key}.")
    return value


def _state(record: dict[str, Any]) -> str:
    """Only a terminal state may be restored.

    5.1 says a job is never run again, so a restored job in ``generating``
    would be a row nothing could ever advance.  The bundle never contains one;
    an archive that claims otherwise is rejected rather than corrected.
    """
    value = _text(record, "state")
    if value not in _TERMINAL_STATE_VALUES:
        raise _reject("The backup contains a job that never finished.", state=value[:32])
    return value


class _NullGate(RestoreGate):
    """A gate that is never busy, for the backup a restore takes of itself."""

    def require_idle(self, what: str = "This action") -> None:
        return


_NULL_GATE: Final = _NullGate()


# ======================================================================
# F-74: the scheduled backup
# ======================================================================


class ScheduleReason(StrEnum):
    DISABLED = "disabled"
    NO_LOCATION = "no_location"
    NOT_DUE = "not_due"
    FIRST_RUN = "first_run"
    DUE = "due"
    #: The app was not running when the schedule came round.  F-74 makes this
    #: up once, which falls out of dating the next run from the last one that
    #: happened rather than from the schedule that was missed.
    MISSED = "missed"
    DEFERRED_GENERATING = "deferred_generating"
    DEFERRED_RESTORING = "deferred_restoring"
    DEFERRED_AFTER_FAILURE = "deferred_after_failure"


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    run: bool
    reason: ScheduleReason
    due_at: float | None

    def __bool__(self) -> bool:
        return self.run


def last_scheduled_run(store: Store) -> float | None:
    """When the last *sound* scheduled backup was taken (F-74).

    An unverified record is an attempt, not a backup; counting one as a run
    would let a day pass with nothing recoverable on disk.
    """
    for record in store.list_backups(kind="scheduled"):
        if record.verified:
            return record.created_at
    return None


class BackupScheduler:
    """F-74's once-daily backup, as a plain object with an explicit clock.

    Nothing here sleeps, waits, or reads a wall clock of its own: the caller
    ticks it with ``now``.  That is what makes "deferred during generation",
    "made up only once", and "seven kept" testable without a day passing.
    """

    __slots__ = ("interval_s", "keep", "retry_after_s", "run_on_first_enable", "_last_failure")

    def __init__(
        self,
        *,
        interval_s: float = SCHEDULED_BACKUP_INTERVAL_S,
        keep: int = BACKUP_KEEP_SCHEDULED,
        retry_after_s: float = SCHEDULED_BACKUP_RETRY_S,
        run_on_first_enable: bool = True,
    ) -> None:
        self.interval_s = float(interval_s)
        self.keep = int(keep)
        self.retry_after_s = float(retry_after_s)
        #: With this off, a machine that is never on for a whole day would
        #: never get a scheduled backup at all, because F-74 runs the schedule
        #: only while the app is running.
        self.run_on_first_enable = run_on_first_enable
        self._last_failure: float | None = None

    def decide(
        self,
        now: float,
        *,
        enabled: bool,
        location: str | os.PathLike[str] | None,
        last_run: float | None,
        generating: bool = False,
        restoring: bool = False,
    ) -> ScheduleDecision:
        """Whether to take a scheduled backup at ``now``, and why not if not."""
        if not enabled:
            return ScheduleDecision(False, ScheduleReason.DISABLED, None)
        if location is None or not str(location):
            return ScheduleDecision(False, ScheduleReason.NO_LOCATION, None)
        due_at = None if last_run is None else last_run + self.interval_s
        if last_run is None:
            reason = ScheduleReason.FIRST_RUN
            if not self.run_on_first_enable:
                return ScheduleDecision(False, ScheduleReason.NOT_DUE, now + self.interval_s)
        elif now < due_at:
            return ScheduleDecision(False, ScheduleReason.NOT_DUE, due_at)
        elif now >= last_run + 2 * self.interval_s:
            reason = ScheduleReason.MISSED
        else:
            reason = ScheduleReason.DUE
        if generating:
            # F-74 defers rather than cancels: the decision stays due, so the
            # next tick after generation ends runs it.
            return ScheduleDecision(False, ScheduleReason.DEFERRED_GENERATING, due_at)
        if restoring:
            return ScheduleDecision(False, ScheduleReason.DEFERRED_RESTORING, due_at)
        if self._last_failure is not None and now < self._last_failure + self.retry_after_s:
            return ScheduleDecision(
                False, ScheduleReason.DEFERRED_AFTER_FAILURE, self._last_failure + self.retry_after_s
            )
        return ScheduleDecision(True, reason, due_at)

    def run_due(
        self,
        store: Store,
        now: float,
        *,
        enabled: bool,
        location: str | os.PathLike[str] | None,
        generating: bool = False,
        restoring: bool = False,
        selection: BackupSelection | None = None,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
        free_space: FreeSpace = _disk_free,
        gate: RestoreGate | None = None,
    ) -> BackupOutcome | None:
        """Take the backup if it is due, then rotate -- in that order.

        4.1 is explicit that older scheduled backups are cleaned up only after
        a new one is verified, so rotation happens here, after
        :func:`create_backup` has verified and recorded the new file, and never
        on a failed or cancelled attempt.
        """
        the_gate = gate or RESTORE_GATE
        decision = self.decide(
            now,
            enabled=enabled,
            location=location,
            last_run=last_scheduled_run(store),
            generating=generating,
            restoring=restoring or the_gate.active,
        )
        if not decision.run:
            return None
        directory = Path(str(location))
        destination = directory / scheduled_backup_name(now)
        try:
            outcome = create_backup(
                store,
                destination,
                selection=selection,
                kind="scheduled",
                at=now,
                progress=progress,
                cancel=cancel,
                free_space=free_space,
                gate=the_gate,
            )
        except Exception:
            # Every failed attempt backs off, not only the ones that arrive as
            # an EchoActError.  A backup that fails some other way fails the
            # same way on the next tick, and retrying it sixty times an hour
            # is what SCHEDULED_BACKUP_RETRY_S exists to prevent.
            self._last_failure = now
            raise
        if outcome.cancelled:
            self._last_failure = now
            return outcome
        self._last_failure = None
        prune_scheduled_backups(
            store,
            keep=self.keep,
            protect=() if outcome.record is None else (outcome.record.backup_id,),
        )
        return outcome


def prune_scheduled_backups(
    store: Store,
    *,
    keep: int = BACKUP_KEEP_SCHEDULED,
    protect: Sequence[str] = (),
) -> tuple[str, ...]:
    """Keep the ``keep`` most recent sound scheduled backups; drop the rest.

    Only records of kind ``scheduled`` are ever considered.  4.1 says manual
    backups are never deleted automatically, and the pre-migration copies N-15
    takes are not this feature's to reclaim either -- so both are invisible
    here by construction rather than by a filter that could be edited away.
    """
    protected = set(protect)
    records = sorted(store.list_backups(kind="scheduled"), key=lambda r: r.created_at, reverse=True)
    kept = 0
    removed: list[str] = []
    for record in records:
        # The backup that triggered this rotation is one of the kept seven,
        # not an eighth alongside them.
        if record.backup_id in protected or (record.verified and kept < keep):
            kept += 1
            continue
        location = Path(record.location)
        if location.is_file() and location.name.endswith(BACKUP_SUFFIX):
            _discard(location)
        store.delete_backup_record(record.backup_id)
        removed.append(record.backup_id)
    if removed:
        log.info("scheduled backups pruned kept=%d removed=%d", kept, len(removed))
    return tuple(removed)
