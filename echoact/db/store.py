"""The local library: documents, job history, segments, results, and the
bookkeeping that keeps them honest.

SQLite in write-ahead mode with a *bounded* busy timeout, per A.2 and N-14.
The bound is the whole point: N-14 forbids both waiting indefinitely on a
lock and reporting a save that did not happen, so a lock that outlives the
timeout leaves here as ``DB_LOCKED`` -- a refusal a caller can retry -- and
never as a stall or a false success.

Three habits run through every method:

* One connection per thread.  ``sqlite3.Connection`` objects are not
  shareable, and the GUI thread, the job engine, and the REST worker all
  read this store.
* Every write goes through :meth:`Store.transaction`, which begins
  ``IMMEDIATE``.  A deferred transaction takes its write lock only at the
  first write and can then fail with ``SQLITE_BUSY`` *without* honouring the
  busy timeout at all; taking the lock up front is what makes the bound real.
* No method reads an audio file except the two that exist to verify one
  (:meth:`Store.verify_result` and :meth:`Store.reconcile_on_start`).  F-40
  requires a list to be answerable without reading audio, and N-21 forbids a
  list query from loading unbounded data into memory, so summaries carry the
  duration computed from stored frame counts and never open anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any, Final

from .. import __version__
from ..domain import (
    Budget,
    Capability,
    Document,
    Job,
    JobKind,
    JobState,
    RequestPath,
    Result,
    RetentionMode,
    Segment,
    TextRange,
    TimeRange,
    VoiceSettings,
    check_transition,
)
from ..errors import Code, EchoActError
from ..paths import audio_dir, db_path
from ..policy import (
    IDEMPOTENCY_TTL_S,
    LIST_PAGE_DEFAULT,
    LIST_PAGE_MAX,
    RETENTION_DEFAULT_BYTES,
    WORKER_RELEASE_DEADLINE_S,
)
from ..util import ids
from .migrations import (
    SUPPORTED_SCHEMA_VERSION,
    assert_compatible,
    current_version,
    has_fts_index,
    migrate,
    translate_sqlite_error,
)

#: How long a write may wait for another writer before it is refused.
#:
#: ``echoact.policy`` has no database timeout of its own, so this borrows
#: N-22's five-second resource-release deadline as the nearest bounded
#: deadline the requirements fix.  It is an upper bound on a pathological
#: case, not a latency: the app has one writer thread in normal operation,
#: and N-22's one-second p95 for queries is met with room to spare.
DEFAULT_BUSY_TIMEOUT_S: Final = WORKER_RELEASE_DEADLINE_S

#: Trigram FTS5 cannot match a term shorter than one trigram; below this the
#: store answers with LIKE instead of returning a confidently empty page.
_MIN_FTS_QUERY_CODEPOINTS: Final = 3

_LIKE_SPECIAL = re.compile(r"([\\%_])")

def _guard[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Turn every driver failure into the one exception type (rule 3)."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except EchoActError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise translate_sqlite_error(exc, retry_after_s=DEFAULT_BUSY_TIMEOUT_S) from exc

    return wrapper


def text_bytes(text: str | None) -> int:
    """UTF-8 size, which is what 4.1's storage ceiling counts."""
    return 0 if text is None else len(text.encode("utf-8"))


def request_match_digest(*parts: str) -> str:
    """4.2's request-match discriminator.

    A digest rather than the request itself, because 4.2 allows a re-request
    record to keep only what duplicate prevention needs and explicitly no
    source text.  Parts are length-prefixed so that ("ab", "c") and
    ("a", "bc") cannot collide.
    """
    h = hashlib.sha256()
    for part in parts:
        raw = part.encode("utf-8")
        h.update(str(len(raw)).encode("ascii"))
        h.update(b":")
        h.update(raw)
    return h.hexdigest()


# ======================================================================
# Read models
# ======================================================================


class ResultIntegrity(StrEnum):
    """F-45's finding about a result's file, persisted so the GUI can offer
    delete and regenerate at any time and not only in the session that
    happened to notice."""

    UNVERIFIED = "unverified"
    OK = "ok"
    MISSING = "missing"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class Page[T]:
    """One page of a list query (4.1: 20 by default, 100 at most)."""

    items: tuple[T, ...]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    """A document without its body.

    N-21 forbids a list query from loading unbounded data: a hundred
    documents of fifty thousand code points each is five million characters
    nobody asked for.
    """

    document_id: str
    title: str
    created_at: float
    modified_at: float
    version: int
    body_codepoints: int


@dataclass(frozen=True, slots=True)
class JobSummary:
    """F-39's history row, and F-56's default projection of it.

    ``source_text`` stays ``None`` unless a caller asks for it, because F-56
    makes the summary the default and puts the snapshot behind a separate,
    separately authorised request.  ``audio_duration_ms`` comes from the
    stored frame count, so F-40's list never opens a WAV.
    """

    job_id: str
    kind: JobKind
    request_path: RequestPath
    owner_client_id: str
    client_label: str | None
    state: JobState
    retention: RetentionMode
    settings: VoiceSettings
    budget: Budget | None
    created_at: float
    started_at: float | None
    ended_at: float | None
    error_code: str | None
    error_message: str | None
    generated_segments: int
    total_segments: int
    has_result: bool
    result_expired: bool
    result_integrity: ResultIntegrity | None
    audio_duration_ms: int
    source_text: str | None = None

    @property
    def model_id(self) -> str:
        return self.settings.model_id


@dataclass(frozen=True, slots=True)
class ClientRecord:
    """4.2's integration permission.  Holds no credential and no verifier:
    N-17 keeps those in ``echoact.security`` and out of backups."""

    client_id: str
    label: str
    capabilities: frozenset[Capability]
    active: bool
    created_at: float
    revoked_at: float | None = None
    last_seen_at: float | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """4.2's re-request record."""

    client_id: str
    key: str
    request_digest: str
    job_id: str
    created_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class BackupRecord:
    backup_id: str
    kind: str
    created_at: float
    location: str
    byte_size: int
    item_count: int
    schema_version: int
    app_version: str
    verified: bool
    note: str | None


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """N-16's "ceiling and remaining space", in the units 4.1 fixes.

    Model cache, user backups, and exported WAV files are deliberately absent:
    4.1 shows those separately and they are not the store's to count.
    """

    document_bytes: int
    job_text_bytes: int
    audio_bytes: int
    limit_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.document_bytes + self.job_text_bytes + self.audio_bytes

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.limit_bytes - self.total_bytes)

    def fits(self, additional_bytes: int) -> bool:
        return self.total_bytes + max(0, additional_bytes) <= self.limit_bytes


@dataclass(frozen=True, slots=True)
class DeletionScope:
    """F-43's preview.  ``blocked_job_ids`` are jobs that are still active;
    5.3 requires cancellation first and forbids deleting one in part."""

    jobs: int
    documents: int
    segments: int
    results: int
    audio_bytes: int
    text_bytes: int
    blocked_job_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Deletion:
    """What a delete actually removed, and the audio files left for the
    caller to unlink.

    The store never deletes a file.  F-43 keeps user-exported WAVs out of any
    deletion, and the only way to be sure of that is for file removal to
    happen where the audio directory's layout is understood, not here.
    """

    documents: int
    jobs: int
    results: int
    audio_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """F-45's start-up findings.  Nothing here starts, regenerates, or plays
    anything; it records what an abnormal termination left behind."""

    interrupted_job_ids: tuple[str, ...]
    canceled_job_ids: tuple[str, ...]
    expired_result_ids: tuple[str, ...]
    missing_result_ids: tuple[str, ...]
    corrupt_result_ids: tuple[str, ...]

    @property
    def has_findings(self) -> bool:
        return bool(
            self.interrupted_job_ids
            or self.canceled_job_ids
            or self.missing_result_ids
            or self.corrupt_result_ids
        )


# ======================================================================
# Column lists
# ======================================================================

# Enumerated rather than ``SELECT *`` so that F-56's summary cannot pick up
# the source-text snapshot by accident when a column is added later.
_JOB_COLUMNS: Final = (
    "job_id",
    "kind",
    "request_path",
    "owner_client_id",
    "client_label",
    "state",
    "retention",
    "model_id",
    "settings_json",
    "budget_json",
    "created_at",
    "started_at",
    "ended_at",
    "error_code",
    "error_message",
    "idempotency_key",
    "generated_segments",
    "total_segments",
)

_SEGMENT_COLUMNS: Final = (
    "segment_id",
    "job_id",
    "seq",
    "source_start_codepoint_inclusive",
    "source_end_codepoint_exclusive",
    "spoken_text",
    "language",
    "audio_start_ms",
    "audio_end_ms",
    "trailing_silence_ms",
    "audio_path",
    "frame_count",
    "ready",
)

_RESULT_COLUMNS: Final = (
    "result_id",
    "job_id",
    "sample_rate",
    "channels",
    "sample_width_bits",
    "frame_count",
    "byte_size",
    "digest",
    "relative_path",
    "created_at",
    "expires_at",
    "integrity_state",
    "verified_at",
)

_ACTIVE_STATE_VALUES: Final = tuple(s.value for s in JobState if s.is_active)


# ======================================================================
# Row mapping
# ======================================================================


def _to_document(row: sqlite3.Row) -> Document:
    return Document(
        document_id=row["document_id"],
        title=row["title"],
        body=row["body"],
        created_at=row["created_at"],
        modified_at=row["modified_at"],
        version=row["version"],
    )


def _to_segment(row: sqlite3.Row) -> Segment:
    start_ms, end_ms = row["audio_start_ms"], row["audio_end_ms"]
    return Segment(
        index=row["seq"],
        source=TextRange(
            row["source_start_codepoint_inclusive"], row["source_end_codepoint_exclusive"]
        ),
        spoken_text=row["spoken_text"],
        language=row["language"],
        time=None if start_ms is None or end_ms is None else TimeRange(start_ms, end_ms),
        trailing_silence_ms=row["trailing_silence_ms"],
        audio_path=row["audio_path"],
        frame_count=row["frame_count"],
        ready=bool(row["ready"]),
        segment_id=row["segment_id"],
    )


def _to_result(row: sqlite3.Row) -> Result:
    return Result(
        result_id=row["result_id"],
        job_id=row["job_id"],
        sample_rate=row["sample_rate"],
        channels=row["channels"],
        sample_width_bits=row["sample_width_bits"],
        frame_count=row["frame_count"],
        byte_size=row["byte_size"],
        digest=row["digest"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        relative_path=row["relative_path"],
    )


def _to_client(row: sqlite3.Row) -> ClientRecord:
    return ClientRecord(
        client_id=row["client_id"],
        label=row["label"],
        capabilities=frozenset(Capability(c) for c in json.loads(row["capabilities"])),
        active=bool(row["active"]),
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        last_seen_at=row["last_seen_at"],
    )


def _to_backup(row: sqlite3.Row) -> BackupRecord:
    return BackupRecord(
        backup_id=row["backup_id"],
        kind=row["kind"],
        created_at=row["created_at"],
        location=row["location"],
        byte_size=row["byte_size"],
        item_count=row["item_count"],
        schema_version=row["schema_version"],
        app_version=row["app_version"],
        verified=bool(row["verified"]),
        note=row["note"],
    )


def _to_idempotency(row: sqlite3.Row) -> IdempotencyRecord:
    return IdempotencyRecord(
        client_id=row["client_id"],
        key=row["key"],
        request_digest=row["request_digest"],
        job_id=row["job_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _duration_ms(frame_count: int | None, sample_rate: int | None) -> int:
    if not frame_count or not sample_rate:
        return 0
    return int(round(frame_count * 1000 / sample_rate))


def _clamp_page(limit: int | None, offset: int) -> tuple[int, int]:
    size = LIST_PAGE_DEFAULT if limit is None else int(limit)
    size = max(1, min(size, LIST_PAGE_MAX))
    return size, max(0, int(offset))


def _like_pattern(query: str) -> str:
    return "%" + _LIKE_SPECIAL.sub(r"\\\1", query) + "%"


def _fts_phrase(query: str) -> str:
    """Quote a user's words as one FTS5 phrase.

    N-18 treats text a user or a client supplies as data.  Unquoted, a query
    containing ``AND``, ``*`` or ``"`` is FTS5 *syntax*, and at best it makes
    the search behave in a way nobody asked for.
    """
    return '"' + query.replace('"', '""') + '"'


# ======================================================================
# Store
# ======================================================================


class Store:
    """The database, as the rest of the application sees it.

    Construct one per process.  It is safe to use from several threads: each
    gets its own connection, and every write is serialised by SQLite itself.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S,
        retention_limit_bytes: int = RETENTION_DEFAULT_BYTES,
        audio_root: str | Path | None = None,
        migrate_on_open: bool = True,
        backup_dir: Path | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else db_path()
        self._busy_timeout_s = max(0.0, float(busy_timeout_s))
        self._retention_limit_bytes = int(retention_limit_bytes)
        self._audio_root = Path(audio_root) if audio_root is not None else None
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._closed = False

        conn = self._conn()
        try:
            if migrate_on_open:
                migrate(conn, backup_dir=backup_dir)
            else:
                assert_compatible(conn)
            self._fts = has_fts_index(conn)
            self._schema_version = current_version(conn)
        except BaseException:
            # A database this build refuses to touch (N-15) must not leave a
            # handle open on it either.
            self.close()
            raise

    # -- lifecycle ------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def fts_enabled(self) -> bool:
        """Whether F-40's search runs over an FTS5 index or degrades to LIKE."""
        return self._fts

    @property
    def schema_version(self) -> int:
        return self._schema_version

    @property
    def supported_schema_version(self) -> int:
        return SUPPORTED_SCHEMA_VERSION

    @property
    def audio_root(self) -> Path:
        # Resolved late: ``paths.data_dir`` is cached, and a test that
        # redirects it does so after this object might already exist.
        return self._audio_root if self._audio_root is not None else audio_dir()

    @property
    def busy_timeout_s(self) -> float:
        return self._busy_timeout_s

    def close(self) -> None:
        """Close every thread's connection.

        Connections are created with ``check_same_thread=False`` purely so
        that shutdown can close them from the thread that owns the Store;
        they are still used by one thread each.
        """
        with self._lock:
            self._closed = True
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                continue

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- connections ----------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise EchoActError(Code.DB_UNAVAILABLE, "The local database is closed.")
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = self._open()
        self._local.conn = conn
        self._local.depth = 0
        with self._lock:
            self._connections.append(conn)
        return conn

    def _open(self) -> sqlite3.Connection:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self._path,
                timeout=self._busy_timeout_s,
                isolation_level=None,
                check_same_thread=False,
            )
        except (sqlite3.Error, OSError) as exc:
            raise translate_sqlite_error(exc) from exc
        conn.row_factory = sqlite3.Row
        try:
            # A pragma cannot take a bound parameter; the value is an int
            # this module computed, never anything a caller supplied.
            conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_s * 1000)}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            # FULL, not NORMAL: N-14 asks for previously sound data to
            # survive a save that fails midway, and A.2 expects the worker
            # to be killable at any instant.  Writes here are small and rare
            # enough that the extra fsync costs nothing worth having.
            conn.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error as exc:
            conn.close()
            raise translate_sqlite_error(exc) from exc
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a unit of work atomically (N-14).

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: a
        deferred transaction that has already read cannot always be upgraded
        to a writer, and SQLite then returns ``SQLITE_BUSY`` *immediately*
        rather than waiting out the busy timeout, so the bound this class
        advertises would not apply to the very case it exists for.

        Nesting is allowed and uses savepoints, so a caller can compose two
        operations -- creating a job and claiming its re-request key, say --
        into one all-or-nothing write.
        """
        conn = self._conn()
        depth: int = getattr(self._local, "depth", 0)
        savepoint = f"echoact_sp{depth}"
        try:
            if depth == 0:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute(f"SAVEPOINT {savepoint}")
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc, retry_after_s=self._busy_timeout_s) from exc
        self._local.depth = depth + 1
        try:
            yield conn
        except BaseException:
            try:
                if depth == 0:
                    conn.execute("ROLLBACK")
                else:
                    conn.execute(f"ROLLBACK TO {savepoint}")
                    conn.execute(f"RELEASE {savepoint}")
            except sqlite3.Error:
                pass
            raise
        else:
            try:
                if depth == 0:
                    conn.execute("COMMIT")
                else:
                    conn.execute(f"RELEASE {savepoint}")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise translate_sqlite_error(exc, retry_after_s=self._busy_timeout_s) from exc
        finally:
            self._local.depth = depth

    # ==================================================================
    # Documents (F-38, F-40)
    # ==================================================================

    @_guard
    def save_document(
        self,
        title: str,
        body: str,
        *,
        document_id: str | None = None,
        at: float | None = None,
    ) -> Document:
        """F-38's explicit save.

        Checked against the retention ceiling first: N-16 forbids making room
        by deleting something the user saved on purpose, so a save that does
        not fit is refused instead.
        """
        moment = ids.now() if at is None else at
        doc = Document(
            document_id=document_id or ids.document_id(),
            title=title,
            body=body,
            created_at=moment,
            modified_at=moment,
            version=1,
        )
        with self.transaction() as conn:
            self.require_retention_capacity(text_bytes(title) + text_bytes(body))
            conn.execute(
                "INSERT INTO documents (document_id, title, body, created_at, modified_at,"
                " version) VALUES (?, ?, ?, ?, ?, ?)",
                (doc.document_id, doc.title, doc.body, moment, moment, doc.version),
            )
        return doc

    @_guard
    def get_document(self, document_id: str) -> Document:
        row = self._conn().execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise EchoActError(Code.NOT_FOUND, "No such document.", detail={"id": document_id})
        return _to_document(row)

    @_guard
    def list_documents(self, *, limit: int | None = None, offset: int = 0) -> Page[DocumentSummary]:
        size, start = _clamp_page(limit, offset)
        conn = self._conn()
        total = int(conn.execute("SELECT count(*) FROM documents").fetchone()[0])
        rows = conn.execute(
            "SELECT document_id, title, created_at, modified_at, version,"
            " length(body) AS body_codepoints FROM documents"
            " ORDER BY modified_at DESC, document_id LIMIT ? OFFSET ?",
            (size, start),
        ).fetchall()
        return Page(tuple(self._document_summary(r) for r in rows), total, size, start)

    @staticmethod
    def _document_summary(row: sqlite3.Row) -> DocumentSummary:
        return DocumentSummary(
            document_id=row["document_id"],
            title=row["title"],
            created_at=row["created_at"],
            modified_at=row["modified_at"],
            version=row["version"],
            body_codepoints=row["body_codepoints"],
        )

    @_guard
    def update_document(
        self,
        document_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        at: float | None = None,
    ) -> Document:
        """F-38's edit.

        N-14 separates document edits from job snapshots: nothing here can
        reach ``jobs.source_text``, and there is no foreign key that would
        let a cascade do it either.
        """
        moment = ids.now() if at is None else at
        with self.transaction() as conn:
            current = self.get_document(document_id)
            new_title = current.title if title is None else title
            new_body = current.body if body is None else body
            grew = (text_bytes(new_title) + text_bytes(new_body)) - (
                text_bytes(current.title) + text_bytes(current.body)
            )
            if grew > 0:
                self.require_retention_capacity(grew)
            conn.execute(
                "UPDATE documents SET title = ?, body = ?, modified_at = ?, version = version + 1"
                " WHERE document_id = ?",
                (new_title, new_body, moment, document_id),
            )
        return Document(
            document_id=document_id,
            title=new_title,
            body=new_body,
            created_at=current.created_at,
            modified_at=moment,
            version=current.version + 1,
        )

    @_guard
    def delete_document(self, document_id: str) -> None:
        """Delete one document and nothing else.

        F-43 is explicit that deleting a document is never presented as
        having deleted a separate job's source text -- and here it cannot,
        because a job's snapshot is its own copy.
        """
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            if cur.rowcount == 0:
                raise EchoActError(Code.NOT_FOUND, "No such document.", detail={"id": document_id})

    @_guard
    def search_documents(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> Page[DocumentSummary]:
        """F-40's search over titles and retained body text.

        Runs on the FTS5 index where the build has one.  Two cases fall back
        to ``LIKE``: a build without FTS5, and a query shorter than a
        trigram, which the index cannot match at all.  Falling back keeps the
        feature working rather than quietly returning nothing.
        """
        text = query.strip()
        if not text:
            return self.list_documents(limit=limit, offset=offset)
        size, start = _clamp_page(limit, offset)
        conn = self._conn()
        if self._fts and len(text) >= _MIN_FTS_QUERY_CODEPOINTS:
            phrase = _fts_phrase(text)
            total = int(
                conn.execute(
                    "SELECT count(*) FROM documents_fts WHERE documents_fts MATCH ?", (phrase,)
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT d.document_id, d.title, d.created_at, d.modified_at, d.version,"
                " length(d.body) AS body_codepoints"
                " FROM documents_fts JOIN documents d ON d.rowid = documents_fts.rowid"
                " WHERE documents_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?",
                (phrase, size, start),
            ).fetchall()
        else:
            pattern = _like_pattern(text)
            where = (
                " WHERE title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\'"
            )
            total = int(
                conn.execute(
                    "SELECT count(*) FROM documents" + where, (pattern, pattern)
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT document_id, title, created_at, modified_at, version,"
                " length(body) AS body_codepoints FROM documents" + where
                + " ORDER BY modified_at DESC, document_id LIMIT ? OFFSET ?",
                (pattern, pattern, size, start),
            ).fetchall()
        return Page(tuple(self._document_summary(r) for r in rows), total, size, start)

    # ==================================================================
    # Jobs (F-39, F-40, F-41)
    # ==================================================================

    @_guard
    def create_job(self, job: Job, *, at: float | None = None) -> Job:
        """Record an accepted job and, if it already has them, its segments.

        A retained job's snapshot is counted against the ceiling before the
        row is written, so 4.1's "jobs requesting retention fail with a
        storage error" happens at acceptance rather than at completion, when
        the user has already waited.
        """
        moment = job.created_at if at is None else at
        with self.transaction() as conn:
            if job.retention is RetentionMode.RETAINED:
                self.require_retention_capacity(text_bytes(job.source_text))
            conn.execute(
                "INSERT INTO jobs (job_id, kind, request_path, owner_client_id, client_label,"
                " state, retention, source_text, model_id, settings_json, budget_json,"
                " created_at, started_at, ended_at, error_code, error_message, idempotency_key,"
                " generated_segments, total_segments)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.job_id,
                    job.kind.value,
                    job.request_path.value,
                    job.owner_client_id,
                    job.client_label,
                    job.state.value,
                    job.retention.value,
                    job.source_text,
                    job.settings.model_id,
                    json.dumps(job.settings.to_dict(), ensure_ascii=False),
                    None if job.budget is None else json.dumps(job.budget.to_dict()),
                    moment,
                    job.started_at,
                    job.ended_at,
                    job.error_code,
                    job.error_message,
                    job.idempotency_key,
                    job.generated_segments,
                    job.total_segments or len(job.segments),
                ),
            )
            if job.segments:
                self.insert_segments(job.job_id, job.segments)
        return job

    @_guard
    def get_job(
        self,
        job_id: str,
        *,
        include_source_text: bool = True,
        include_segments: bool = True,
    ) -> Job:
        """F-41's reopen: source text, settings, and the result if one exists."""
        conn = self._conn()
        columns = ", ".join(_JOB_COLUMNS)
        row = conn.execute(
            f"SELECT {columns} FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise EchoActError(Code.NOT_FOUND, "No such job.", detail={"id": job_id})
        source = ""
        if include_source_text:
            source = self.get_job_text(job_id) or ""
        segments = self.list_segments(job_id) if include_segments else []
        return self._to_job(row, source_text=source, segments=segments)

    @_guard
    def get_job_text(self, job_id: str) -> str | None:
        """F-56's separate snapshot request.

        ``None`` means the snapshot is not stored: either the job was one-off
        or its retention window has closed and F-42 required the text to go.
        The caller decides what that is on its surface; the store does not
        pretend an empty string is the same thing.
        """
        row = self._conn().execute(
            "SELECT source_text FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise EchoActError(Code.NOT_FOUND, "No such job.", detail={"id": job_id})
        return row["source_text"]

    def _to_job(
        self,
        row: sqlite3.Row,
        *,
        source_text: str,
        segments: Sequence[Segment],
        result: Result | None = None,
    ) -> Job:
        return Job(
            job_id=row["job_id"],
            kind=JobKind(row["kind"]),
            request_path=RequestPath(row["request_path"]),
            owner_client_id=row["owner_client_id"],
            state=JobState(row["state"]),
            source_text=source_text,
            settings=VoiceSettings.from_dict(json.loads(row["settings_json"])),
            budget=None if row["budget_json"] is None else Budget.from_dict(
                json.loads(row["budget_json"])
            ),
            retention=RetentionMode(row["retention"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            client_label=row["client_label"],
            idempotency_key=row["idempotency_key"],
            segments=list(segments),
            result=result if result is not None else self.get_result_for_job(row["job_id"]),
            generated_segments=row["generated_segments"],
            total_segments=row["total_segments"],
        )

    @_guard
    def list_jobs(
        self,
        *,
        created_from: float | None = None,
        created_to: float | None = None,
        model_id: str | None = None,
        states: Iterable[JobState] | None = None,
        owner_client_id: str | None = None,
        retention: RetentionMode | None = None,
        include_source_text: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[JobSummary]:
        """F-40's filtered, paged history.

        Date, model, and state are all indexed columns, and the result's
        length comes from the joined ``results`` row -- no WAV is opened and
        no snapshot is read unless ``include_source_text`` asks for one,
        which is F-56's default made structural.
        """
        size, start = _clamp_page(limit, offset)
        where: list[str] = []
        args: list[Any] = []
        if created_from is not None:
            where.append("j.created_at >= ?")
            args.append(created_from)
        if created_to is not None:
            where.append("j.created_at <= ?")
            args.append(created_to)
        if model_id is not None:
            where.append("j.model_id = ?")
            args.append(model_id)
        if owner_client_id is not None:
            where.append("j.owner_client_id = ?")
            args.append(owner_client_id)
        if retention is not None:
            where.append("j.retention = ?")
            args.append(retention.value)
        state_values = tuple(JobState(s).value for s in states) if states is not None else ()
        if state_values:
            where.append(f"j.state IN ({','.join('?' * len(state_values))})")
            args.extend(state_values)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        conn = self._conn()
        total = int(
            conn.execute(f"SELECT count(*) FROM jobs j{clause}", tuple(args)).fetchone()[0]
        )
        columns = ", ".join(f"j.{c}" for c in _JOB_COLUMNS)
        if include_source_text:
            columns += ", j.source_text"
        rows = conn.execute(
            f"SELECT {columns}, r.result_id AS r_id, r.frame_count AS r_frames,"
            " r.sample_rate AS r_rate, r.expires_at AS r_expires,"
            " r.integrity_state AS r_integrity"
            " FROM jobs j LEFT JOIN results r ON r.job_id = j.job_id"
            f"{clause} ORDER BY j.created_at DESC, j.job_id LIMIT ? OFFSET ?",
            (*args, size, start),
        ).fetchall()
        now = ids.now()
        items = tuple(
            self._job_summary(r, now=now, with_text=include_source_text) for r in rows
        )
        return Page(items, total, size, start)

    @staticmethod
    def _job_summary(row: sqlite3.Row, *, now: float, with_text: bool) -> JobSummary:
        expires = row["r_expires"]
        integrity = row["r_integrity"]
        return JobSummary(
            job_id=row["job_id"],
            kind=JobKind(row["kind"]),
            request_path=RequestPath(row["request_path"]),
            owner_client_id=row["owner_client_id"],
            client_label=row["client_label"],
            state=JobState(row["state"]),
            retention=RetentionMode(row["retention"]),
            settings=VoiceSettings.from_dict(json.loads(row["settings_json"])),
            budget=None if row["budget_json"] is None else Budget.from_dict(
                json.loads(row["budget_json"])
            ),
            created_at=row["created_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            generated_segments=row["generated_segments"],
            total_segments=row["total_segments"],
            has_result=row["r_id"] is not None,
            result_expired=expires is not None and expires <= now,
            result_integrity=None if integrity is None else ResultIntegrity(integrity),
            audio_duration_ms=_duration_ms(row["r_frames"], row["r_rate"]),
            source_text=row["source_text"] if with_text else None,
        )

    @_guard
    def update_job_state(
        self,
        job_id: str,
        state: JobState,
        *,
        at: float | None = None,
        force: bool = False,
    ) -> JobState:
        """Move a job along Section 5.1's state machine.

        The transition is checked against ``domain.can_transition`` rather
        than trusted, which is what stops a late worker reply from
        overwriting ``Complete`` with ``Canceled``: 5.1 keeps whichever
        terminal state was confirmed first, and ``Complete`` has no outgoing
        transitions at all.  Repeating the state a job is already in is a
        no-op, so F-49's "repeated cancellation adds no further side effects"
        holds here too.
        """
        moment = ids.now() if at is None else at
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state, started_at FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise EchoActError(Code.NOT_FOUND, "No such job.", detail={"id": job_id})
            current = JobState(row["state"])
            if current is state:
                return current
            if not force:
                check_transition(current, state)
            started = row["started_at"]
            if started is None and state in (JobState.PREPARING_MODEL, JobState.GENERATING):
                started = moment
            ended = moment if state.is_terminal else None
            conn.execute(
                "UPDATE jobs SET state = ?, started_at = ?,"
                " ended_at = CASE WHEN ? IS NULL THEN ended_at ELSE ? END WHERE job_id = ?",
                (state.value, started, ended, ended, job_id),
            )
        return state

    @_guard
    def record_job_error(
        self,
        job_id: str,
        code: Code | str,
        message: str | None = None,
        *,
        state: JobState = JobState.FAILED,
        at: float | None = None,
    ) -> JobState:
        """F-39 keeps the error code with the job; N-25 traces a problem by it.

        The message is the code's own English text unless the caller has
        something more specific.  Neither field ever carries body text --
        N-20 keeps that out of anything that is later exported.
        """
        as_code = Code(code) if not isinstance(code, Code) else code
        text = message if message is not None else EchoActError(as_code).message
        moment = ids.now() if at is None else at
        with self.transaction() as conn:
            final = self.update_job_state(job_id, state, at=moment)
            conn.execute(
                "UPDATE jobs SET error_code = ?, error_message = ? WHERE job_id = ?",
                (as_code.value, text, job_id),
            )
        return final

    @_guard
    def set_job_budget(self, job_id: str, budget: Budget) -> None:
        """F-78 distinguishes the setting on screen from the one in force;
        this is the one in force, recorded on the job it applied to."""
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE jobs SET budget_json = ? WHERE job_id = ?",
                (json.dumps(budget.to_dict()), job_id),
            )
            if cur.rowcount == 0:
                raise EchoActError(Code.NOT_FOUND, "No such job.", detail={"id": job_id})

    @_guard
    def clear_job_source_text(self, job_id: str) -> None:
        """F-42: a one-off job's body text leaves permanent storage.

        The job row stays, because 4.2 still requires the terminal job and
        its reason to be returned to a repeated re-request key.
        """
        with self.transaction() as conn:
            conn.execute("UPDATE jobs SET source_text = NULL WHERE job_id = ?", (job_id,))

    @_guard
    def attach_result(self, result: Result, *, at: float | None = None) -> Result:
        """F-41's "if a result exists it can be played and saved", recorded.

        A retained job's audio is checked against the ceiling here as well as
        at acceptance, because 4.1 lets the limit be reached at runtime and
        N-16 refuses the retention rather than deleting something to fit it.
        """
        moment = result.created_at if at is None else at
        result_id = result.result_id or ids.result_id()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT retention FROM jobs WHERE job_id = ?", (result.job_id,)
            ).fetchone()
            if row is None:
                raise EchoActError(Code.NOT_FOUND, "No such job.", detail={"id": result.job_id})
            if RetentionMode(row["retention"]) is RetentionMode.RETAINED:
                self.require_retention_capacity(result.byte_size)
            conn.execute(
                "INSERT INTO results (result_id, job_id, sample_rate, channels,"
                " sample_width_bits, frame_count, byte_size, digest, relative_path,"
                " created_at, expires_at, integrity_state, verified_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unverified', NULL)",
                (
                    result_id,
                    result.job_id,
                    result.sample_rate,
                    result.channels,
                    result.sample_width_bits,
                    result.frame_count,
                    result.byte_size,
                    result.digest,
                    result.relative_path,
                    moment,
                    result.expires_at,
                ),
            )
        return replace(result, result_id=result_id, created_at=moment)

    @_guard
    def preview_deletion(
        self,
        *,
        job_ids: Sequence[str] = (),
        document_ids: Sequence[str] = (),
        all_history: bool = False,
    ) -> DeletionScope:
        """F-43's scope preview, so a confirmation can state what goes.

        Active jobs are reported rather than counted: 5.3 requires
        cancellation first and forbids deleting a running job in part.
        """
        conn = self._conn()
        if all_history:
            job_rows = conn.execute("SELECT job_id, state FROM jobs").fetchall()
        elif job_ids:
            marks = ",".join("?" * len(job_ids))
            job_rows = conn.execute(
                f"SELECT job_id, state FROM jobs WHERE job_id IN ({marks})", tuple(job_ids)
            ).fetchall()
        else:
            job_rows = []
        chosen = tuple(r["job_id"] for r in job_rows)
        blocked = tuple(r["job_id"] for r in job_rows if JobState(r["state"]).is_active)

        segments = results = audio = text = documents = 0
        if chosen:
            marks = ",".join("?" * len(chosen))
            segments = int(
                conn.execute(
                    f"SELECT count(*) FROM segments WHERE job_id IN ({marks})", chosen
                ).fetchone()[0]
            )
            row = conn.execute(
                f"SELECT count(*), coalesce(sum(byte_size), 0) FROM results"
                f" WHERE job_id IN ({marks})",
                chosen,
            ).fetchone()
            results, audio = int(row[0]), int(row[1])
            text = int(
                conn.execute(
                    "SELECT coalesce(sum(length(CAST(source_text AS BLOB))), 0) FROM jobs"
                    f" WHERE job_id IN ({marks})",
                    chosen,
                ).fetchone()[0]
            )
        if document_ids:
            marks = ",".join("?" * len(document_ids))
            row = conn.execute(
                "SELECT count(*), coalesce(sum(length(CAST(title AS BLOB))"
                f" + length(CAST(body AS BLOB))), 0) FROM documents WHERE document_id IN ({marks})",
                tuple(document_ids),
            ).fetchone()
            documents, doc_bytes = int(row[0]), int(row[1])
            text += doc_bytes
        return DeletionScope(
            jobs=len(chosen),
            documents=documents,
            segments=segments,
            results=results,
            audio_bytes=audio,
            text_bytes=text,
            blocked_job_ids=blocked,
        )

    @_guard
    def delete_job(self, job_id: str, *, force: bool = False) -> Deletion:
        """F-43's job deletion.

        Refuses while the job is active: 5.3 requires cancellation first, and
        ``DELETE_BLOCKED_IN_USE`` is retryable so N-16's "deletion failures
        are shown as retryable and are not mistaken for completed deletions"
        holds at the surface without further translation.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise EchoActError(Code.NOT_FOUND, "No such job.", detail={"id": job_id})
            if JobState(row["state"]).is_active and not force:
                raise EchoActError(
                    Code.DELETE_BLOCKED_IN_USE,
                    "Cancel this job before deleting it.",
                    detail={"job_id": job_id, "state": row["state"]},
                    retry_after_s=self._busy_timeout_s,
                )
            paths = self._audio_paths_for([job_id], conn)
            results = int(
                conn.execute(
                    "SELECT count(*) FROM results WHERE job_id = ?", (job_id,)
                ).fetchone()[0]
            )
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        return Deletion(documents=0, jobs=1, results=results, audio_paths=paths)

    @_guard
    def delete_all_history(self, *, force: bool = False) -> Deletion:
        """F-43's "delete all history".  Documents are a separate scope and
        are untouched, which is the distinction F-43 insists on."""
        with self.transaction() as conn:
            active = [
                r["job_id"]
                for r in conn.execute(
                    "SELECT job_id FROM jobs WHERE state IN"
                    f" ({','.join('?' * len(_ACTIVE_STATE_VALUES))})",
                    _ACTIVE_STATE_VALUES,
                ).fetchall()
            ]
            if active and not force:
                raise EchoActError(
                    Code.DELETE_BLOCKED_IN_USE,
                    "Cancel the running job before deleting history.",
                    detail={"job_ids": active[:10]},
                    retry_after_s=self._busy_timeout_s,
                )
            job_ids = [r["job_id"] for r in conn.execute("SELECT job_id FROM jobs").fetchall()]
            paths = self._audio_paths_for(job_ids, conn)
            results = int(conn.execute("SELECT count(*) FROM results").fetchone()[0])
            conn.execute("DELETE FROM jobs")
        return Deletion(documents=0, jobs=len(job_ids), results=results, audio_paths=paths)

    @staticmethod
    def _audio_paths_for(job_ids: Sequence[str], conn: sqlite3.Connection) -> tuple[str, ...]:
        if not job_ids:
            return ()
        marks = ",".join("?" * len(job_ids))
        paths = [
            r[0]
            for r in conn.execute(
                f"SELECT relative_path FROM results WHERE job_id IN ({marks})", tuple(job_ids)
            )
            if r[0]
        ]
        paths.extend(
            r[0]
            for r in conn.execute(
                f"SELECT audio_path FROM segments WHERE job_id IN ({marks})"
                " AND audio_path IS NOT NULL",
                tuple(job_ids),
            )
        )
        return tuple(paths)

    # ==================================================================
    # Segments
    # ==================================================================

    @_guard
    def insert_segments(self, job_id: str, segments: Sequence[Segment]) -> tuple[Segment, ...]:
        """Write a job's segment table in one go.

        Offsets go into columns named for what 4.2 standardises them as:
        code points, start inclusive, end exclusive.  The schema enforces the
        ordering, so a UTF-16 index borrowed from Qt cannot quietly survive
        here even if it arrives.
        """
        stored: list[Segment] = []
        with self.transaction() as conn:
            for position, seg in enumerate(segments):
                index = seg.index if seg.index is not None else position
                segment_id = seg.segment_id or ids.segment_id()
                conn.execute(
                    "INSERT INTO segments (segment_id, job_id, seq,"
                    " source_start_codepoint_inclusive, source_end_codepoint_exclusive,"
                    " spoken_text, language, audio_start_ms, audio_end_ms,"
                    " trailing_silence_ms, audio_path, frame_count, ready)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        segment_id,
                        job_id,
                        index,
                        seg.source.start,
                        seg.source.end,
                        seg.spoken_text,
                        seg.language,
                        None if seg.time is None else seg.time.start_ms,
                        None if seg.time is None else seg.time.end_ms,
                        seg.trailing_silence_ms,
                        seg.audio_path,
                        seg.frame_count,
                        int(seg.ready),
                    ),
                )
                stored.append(
                    Segment(
                        index=index,
                        source=seg.source,
                        spoken_text=seg.spoken_text,
                        language=seg.language,
                        time=seg.time,
                        trailing_silence_ms=seg.trailing_silence_ms,
                        audio_path=seg.audio_path,
                        frame_count=seg.frame_count,
                        ready=seg.ready,
                        segment_id=segment_id,
                    )
                )
            self._refresh_segment_counts(job_id, conn)
        return tuple(stored)

    @_guard
    def mark_segment_ready(
        self,
        job_id: str,
        index: int,
        *,
        time: TimeRange,
        audio_path: str | None = None,
        frame_count: int = 0,
        trailing_silence_ms: int | None = None,
    ) -> None:
        """A segment becomes playable (F-55, F-12).

        The job's generated count is recomputed from the segment rows rather
        than incremented, so replaying the same completion -- which a
        reconnecting worker can do -- cannot inflate the progress F-11 shows.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE segments SET ready = 1, audio_start_ms = ?, audio_end_ms = ?,"
                " audio_path = ?, frame_count = ?,"
                " trailing_silence_ms = CASE WHEN ? IS NULL THEN trailing_silence_ms ELSE ? END"
                " WHERE job_id = ? AND seq = ?",
                (
                    time.start_ms,
                    time.end_ms,
                    audio_path,
                    frame_count,
                    trailing_silence_ms,
                    trailing_silence_ms,
                    job_id,
                    index,
                ),
            )
            if cur.rowcount == 0:
                raise EchoActError(
                    Code.NOT_FOUND, "No such segment.", detail={"job_id": job_id, "index": index}
                )
            self._refresh_segment_counts(job_id, conn)

    @staticmethod
    def _refresh_segment_counts(job_id: str, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE jobs SET"
            " total_segments = (SELECT count(*) FROM segments WHERE job_id = ?),"
            " generated_segments = (SELECT count(*) FROM segments WHERE job_id = ? AND ready = 1)"
            " WHERE job_id = ?",
            (job_id, job_id, job_id),
        )

    @_guard
    def list_segments(self, job_id: str, *, ready_only: bool = False) -> list[Segment]:
        """F-55's segment list: order, source range, times, readiness.  No
        audio is read -- an unready segment has no audio to read anyway, and
        F-55 forbids returning one as if it were finished."""
        columns = ", ".join(_SEGMENT_COLUMNS)
        clause = " AND ready = 1" if ready_only else ""
        rows = self._conn().execute(
            f"SELECT {columns} FROM segments WHERE job_id = ?{clause} ORDER BY seq", (job_id,)
        ).fetchall()
        return [_to_segment(r) for r in rows]

    # ==================================================================
    # Results
    # ==================================================================

    @_guard
    def get_result(self, result_id: str) -> Result:
        columns = ", ".join(_RESULT_COLUMNS)
        row = self._conn().execute(
            f"SELECT {columns} FROM results WHERE result_id = ?", (result_id,)
        ).fetchone()
        if row is None:
            raise EchoActError(Code.NOT_FOUND, "No such result.", detail={"id": result_id})
        return _to_result(row)

    @_guard
    def get_result_for_job(self, job_id: str) -> Result | None:
        columns = ", ".join(_RESULT_COLUMNS)
        row = self._conn().execute(
            f"SELECT {columns} FROM results WHERE job_id = ?", (job_id,)
        ).fetchone()
        return None if row is None else _to_result(row)

    @_guard
    def result_integrity(self, result_id: str) -> ResultIntegrity:
        row = self._conn().execute(
            "SELECT integrity_state FROM results WHERE result_id = ?", (result_id,)
        ).fetchone()
        if row is None:
            raise EchoActError(Code.NOT_FOUND, "No such result.", detail={"id": result_id})
        return ResultIntegrity(row["integrity_state"])

    @_guard
    def expire_result(self, result_id: str, *, at: float | None = None) -> None:
        """4.1's one-off lifetime, applied.

        The row survives its own expiry: 4.2 requires a repeated re-request
        key to return the existing job *and* the result's expired state, and
        a deleted row could only be reported as "never existed".
        """
        moment = ids.now() if at is None else at
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE results SET expires_at = ? WHERE result_id = ?"
                " AND (expires_at IS NULL OR expires_at > ?)",
                (moment, result_id, moment),
            )
            if cur.rowcount == 0 and self._count(conn, "results", "result_id", result_id) == 0:
                raise EchoActError(Code.NOT_FOUND, "No such result.", detail={"id": result_id})

    @_guard
    def due_results(self, *, at: float | None = None) -> tuple[Result, ...]:
        """Results whose lifetime has passed, for the caller to clean up.

        Returned rather than deleted here: the files are the caller's to
        unlink, and F-73 excludes anything currently playing or generating
        from cleanup, which is knowledge this layer does not have.
        """
        moment = ids.now() if at is None else at
        columns = ", ".join(_RESULT_COLUMNS)
        rows = self._conn().execute(
            f"SELECT {columns} FROM results WHERE expires_at IS NOT NULL AND expires_at <= ?"
            " ORDER BY expires_at",
            (moment,),
        ).fetchall()
        return tuple(_to_result(r) for r in rows)

    @_guard
    def delete_result(self, result_id: str) -> Deletion:
        """Remove a result row, leaving its file for the caller to unlink."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT relative_path FROM results WHERE result_id = ?", (result_id,)
            ).fetchone()
            if row is None:
                raise EchoActError(Code.NOT_FOUND, "No such result.", detail={"id": result_id})
            conn.execute("DELETE FROM results WHERE result_id = ?", (result_id,))
        path = row["relative_path"]
        return Deletion(documents=0, jobs=0, results=1, audio_paths=(path,) if path else ())

    def _resolve_audio(self, relative_path: str) -> Path | None:
        """Resolve a stored relative path inside the audio directory.

        Anything that climbs out of the root resolves to ``None`` and is
        treated as a missing file.  N-18 and N-27 both forbid a stored path
        from reaching outside the app's own tree, and a restored or tampered
        row is exactly where such a path would come from.
        """
        if not relative_path:
            return None
        root = self.audio_root
        try:
            candidate = (root / relative_path).resolve()
            candidate.relative_to(root.resolve())
        except (OSError, ValueError):
            return None
        return candidate

    @_guard
    def verify_result(self, result_id: str, *, deep: bool = True, at: float | None = None) -> ResultIntegrity:
        """F-45's detection, for one result.

        ``deep`` recomputes the SHA-256 in 4.2's integrity field.  Shallow
        verification compares the recorded byte size, which is what catches
        the failure an abnormal termination actually produces -- a truncated
        file -- without reading gigabytes at start-up.
        """
        moment = ids.now() if at is None else at
        result = self.get_result(result_id)
        path = self._resolve_audio(result.relative_path)
        state = ResultIntegrity.OK
        if path is None or not path.exists():
            state = ResultIntegrity.MISSING
        else:
            try:
                if path.stat().st_size != result.byte_size:
                    state = ResultIntegrity.CORRUPT
                elif deep:
                    with path.open("rb") as fh:
                        actual = hashlib.file_digest(fh, "sha256").hexdigest()
                    if actual != result.digest:
                        state = ResultIntegrity.CORRUPT
            except OSError:
                state = ResultIntegrity.MISSING
        with self.transaction() as conn:
            conn.execute(
                "UPDATE results SET integrity_state = ?, verified_at = ? WHERE result_id = ?",
                (state.value, moment, result_id),
            )
        return state

    # ==================================================================
    # Re-request records (F-49, 4.1, 4.2)
    # ==================================================================

    @_guard
    def lookup_idempotency(
        self, client_id: str, key: str, *, at: float | None = None
    ) -> IdempotencyRecord | None:
        """4.2's re-request lookup, scoped per client.

        An expired entry reads as absent: 4.1 discloses that reusing a key
        after expiry may become a new request, and pretending otherwise would
        return a job the caller can no longer reason about.
        """
        moment = ids.now() if at is None else at
        row = self._conn().execute(
            "SELECT client_id, key, request_digest, job_id, created_at, expires_at"
            " FROM idempotency WHERE client_id = ? AND key = ? AND expires_at > ?",
            (client_id, key, moment),
        ).fetchone()
        return None if row is None else _to_idempotency(row)

    @_guard
    def remember_idempotency(
        self,
        client_id: str,
        key: str,
        *,
        request_digest: str,
        job_id: str,
        at: float | None = None,
        ttl_s: float = IDEMPOTENCY_TTL_S,
    ) -> IdempotencyRecord:
        """Store the discriminator, the job, and an expiry -- and no text.

        Kept in the database rather than in memory because 4.1 requires
        duplicate prevention to survive a normal restart for the same hour.

        A live entry for the same key is never overwritten in silence: the
        same content returns the entry that already exists, and different
        content is F-49's conflict.  Only an expired entry is replaced, which
        is 4.1's disclosed "reusing the same key after expiry may become a
        new request".
        """
        moment = ids.now() if at is None else at
        record = IdempotencyRecord(
            client_id=client_id,
            key=key,
            request_digest=request_digest,
            job_id=job_id,
            created_at=moment,
            expires_at=moment + ttl_s,
        )
        with self.transaction() as conn:
            existing = self.lookup_idempotency(client_id, key, at=moment)
            if existing is not None:
                if existing.request_digest != request_digest or existing.job_id != job_id:
                    raise EchoActError(
                        Code.IDEMPOTENCY_KEY_CONFLICT, detail={"job_id": existing.job_id}
                    )
                return existing
            conn.execute(
                "INSERT INTO idempotency (client_id, key, request_digest, job_id, created_at,"
                " expires_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (client_id, key) DO UPDATE SET"
                " request_digest = excluded.request_digest, job_id = excluded.job_id,"
                " created_at = excluded.created_at, expires_at = excluded.expires_at",
                (client_id, key, request_digest, job_id, moment, record.expires_at),
            )
        return record

    @_guard
    def claim_job(
        self,
        job: Job,
        *,
        client_id: str,
        key: str,
        request_digest: str,
        at: float | None = None,
        ttl_s: float = IDEMPOTENCY_TTL_S,
    ) -> tuple[Job, bool]:
        """F-49's gate, as one atomic step.

        Returns ``(job, created)``.  The same key with the same content
        returns the existing job; different content is
        ``IDEMPOTENCY_KEY_CONFLICT``.  Lookup and insert share one
        ``BEGIN IMMEDIATE`` transaction on purpose: with a single generation
        slot, two racing retries of the same request are the ordinary case,
        not the exotic one, and checking then inserting without a lock would
        let both through.
        """
        moment = ids.now() if at is None else at
        with self.transaction():
            existing = self.lookup_idempotency(client_id, key, at=moment)
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise EchoActError(
                        Code.IDEMPOTENCY_KEY_CONFLICT,
                        detail={"job_id": existing.job_id},
                    )
                return self.get_job(existing.job_id), False
            self.create_job(job, at=moment)
            self.remember_idempotency(
                client_id,
                key,
                request_digest=request_digest,
                job_id=job.job_id,
                at=moment,
                ttl_s=ttl_s,
            )
        return job, True

    @_guard
    def purge_expired_idempotency(self, *, at: float | None = None) -> int:
        moment = ids.now() if at is None else at
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM idempotency WHERE expires_at <= ?", (moment,))
            return cur.rowcount

    # ==================================================================
    # Retention accounting (N-16, 4.1)
    # ==================================================================

    @property
    def retention_limit_bytes(self) -> int:
        return self._retention_limit_bytes

    def set_retention_limit(self, limit_bytes: int) -> None:
        """4.1: lowering the limit below what is already stored keeps the
        data and blocks new retention.  Nothing is deleted here, ever."""
        self._retention_limit_bytes = int(limit_bytes)

    @_guard
    def storage_usage(self, *, at: float | None = None) -> StorageUsage:
        """N-16's display: what the app manages against the ceiling.

        Audio counts every result whose lifetime has not passed, one-off
        included, because the space is genuinely occupied while it lives.
        Source text counts only retained jobs: a one-off snapshot is
        temporary by definition and F-42 keeps it out of permanent history.
        """
        moment = ids.now() if at is None else at
        row = self._conn().execute(
            "SELECT"
            " (SELECT coalesce(sum(length(CAST(title AS BLOB))"
            "   + length(CAST(body AS BLOB))), 0) FROM documents),"
            " (SELECT coalesce(sum(length(CAST(source_text AS BLOB))), 0) FROM jobs"
            "   WHERE retention = ? AND source_text IS NOT NULL),"
            " (SELECT coalesce(sum(byte_size), 0) FROM results"
            "   WHERE expires_at IS NULL OR expires_at > ?)",
            (RetentionMode.RETAINED.value, moment),
        ).fetchone()
        return StorageUsage(
            document_bytes=int(row[0]),
            job_text_bytes=int(row[1]),
            audio_bytes=int(row[2]),
            limit_bytes=self._retention_limit_bytes,
        )

    def retention_fits(self, additional_bytes: int) -> bool:
        return self.storage_usage().fits(additional_bytes)

    def require_retention_capacity(self, additional_bytes: int) -> None:
        """N-16, stated as code: the request fails, the data stays.

        There is deliberately no eviction path here.  "Explicitly saved
        documents and retained results are never silently deleted to free
        space" leaves exactly one behaviour when the ceiling is reached, and
        4.1 confirms it: the retention request is reported as a failure.
        """
        if additional_bytes <= 0:
            return
        usage = self.storage_usage()
        if not usage.fits(additional_bytes):
            raise EchoActError(
                Code.RETENTION_LIMIT_REACHED,
                detail={
                    "used_bytes": usage.total_bytes,
                    "limit_bytes": usage.limit_bytes,
                    "requested_bytes": additional_bytes,
                },
            )

    # ==================================================================
    # Start-up reconciliation (F-45)
    # ==================================================================

    @_guard
    def reconcile_on_start(self, *, at: float | None = None, deep: bool = False) -> ReconcileReport:
        """F-45.  Call once, before anything else uses the store.

        Three findings, and no action beyond recording them -- F-45 forbids
        regenerating or playing anything automatically, so nothing here
        starts a job, and the GUI decides what to offer.

        * A job left non-terminal by an abnormal termination becomes
          Interrupted.  It never becomes Complete: the audio it would claim
          to have does not exist.  A job caught in Canceling becomes
          Canceled, because Section 5.1's transition table has no
          Canceling -> Interrupted edge and the user's cancellation is the
          outcome that actually happened.
        * A one-off result from a previous run is expired outright.  4.1
          gives it "one hour after a terminal state, or app exit, whichever
          comes first", and app exit has demonstrably come first.
        * A retained result whose file is gone or the wrong size is marked,
          so the GUI can say why playback is unavailable and offer delete and
          regenerate.
        """
        moment = ids.now() if at is None else at
        interrupted: list[str] = []
        canceled: list[str] = []

        with self.transaction() as conn:
            revivable = tuple(
                v for v in _ACTIVE_STATE_VALUES if v != JobState.CANCELING.value
            )
            marks = ",".join("?" * len(revivable))
            interrupted = [
                r["job_id"]
                for r in conn.execute(
                    f"SELECT job_id FROM jobs WHERE state IN ({marks})", revivable
                ).fetchall()
            ]
            canceled = [
                r["job_id"]
                for r in conn.execute(
                    "SELECT job_id FROM jobs WHERE state = ?", (JobState.CANCELING.value,)
                ).fetchall()
            ]
            if interrupted:
                conn.execute(
                    f"UPDATE jobs SET state = ?, ended_at = coalesce(ended_at, ?)"
                    f" WHERE state IN ({marks})",
                    (JobState.INTERRUPTED.value, moment, *revivable),
                )
            if canceled:
                conn.execute(
                    "UPDATE jobs SET state = ?, ended_at = coalesce(ended_at, ?) WHERE state = ?",
                    (JobState.CANCELED.value, moment, JobState.CANCELING.value),
                )

        with self.transaction() as conn:
            expired = [
                r["result_id"]
                for r in conn.execute(
                    "SELECT r.result_id FROM results r JOIN jobs j ON j.job_id = r.job_id"
                    " WHERE j.retention = ? AND (r.expires_at IS NULL OR r.expires_at > ?)",
                    (RetentionMode.ONE_OFF.value, moment),
                ).fetchall()
            ]
            if expired:
                marks = ",".join("?" * len(expired))
                conn.execute(
                    f"UPDATE results SET expires_at = ? WHERE result_id IN ({marks})",
                    (moment, *expired),
                )

        retained = [
            r["result_id"]
            for r in self._conn().execute(
                "SELECT r.result_id FROM results r JOIN jobs j ON j.job_id = r.job_id"
                " WHERE j.retention = ?",
                (RetentionMode.RETAINED.value,),
            ).fetchall()
        ]
        missing: list[str] = []
        corrupt: list[str] = []
        for result_id in retained:
            state = self.verify_result(result_id, deep=deep, at=moment)
            if state is ResultIntegrity.MISSING:
                missing.append(result_id)
            elif state is ResultIntegrity.CORRUPT:
                corrupt.append(result_id)

        return ReconcileReport(
            interrupted_job_ids=tuple(interrupted),
            canceled_job_ids=tuple(canceled),
            expired_result_ids=tuple(expired),
            missing_result_ids=tuple(missing),
            corrupt_result_ids=tuple(corrupt),
        )

    # ==================================================================
    # Integration permissions (4.2, F-61, F-71)
    # ==================================================================

    @_guard
    def upsert_client(
        self,
        client_id: str,
        label: str,
        capabilities: Iterable[Capability],
        *,
        active: bool = True,
        at: float | None = None,
    ) -> ClientRecord:
        moment = ids.now() if at is None else at
        caps = frozenset(Capability(c) for c in capabilities)
        payload = json.dumps(sorted(c.value for c in caps))
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO clients (client_id, label, capabilities, active, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (client_id) DO UPDATE SET label = excluded.label,"
                " capabilities = excluded.capabilities, active = excluded.active,"
                " revoked_at = CASE WHEN excluded.active = 1 THEN NULL ELSE clients.revoked_at END",
                (client_id, label, payload, int(active), moment),
            )
        return ClientRecord(
            client_id=client_id,
            label=label,
            capabilities=caps,
            active=active,
            created_at=moment,
        )

    @_guard
    def get_client(self, client_id: str) -> ClientRecord | None:
        row = self._conn().execute(
            "SELECT * FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return None if row is None else _to_client(row)

    @_guard
    def list_clients(self) -> tuple[ClientRecord, ...]:
        rows = self._conn().execute("SELECT * FROM clients ORDER BY created_at").fetchall()
        return tuple(_to_client(r) for r in rows)

    @_guard
    def revoke_client(self, client_id: str, *, at: float | None = None) -> None:
        """F-61's revocation, recorded rather than deleted.

        The row stays so F-71 can still show what was granted and when it was
        withdrawn; deleting it would make an audit of a revoked client
        impossible.  Cancelling that client's in-flight jobs is 5.3's
        separate obligation and belongs to the job engine.
        """
        moment = ids.now() if at is None else at
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE clients SET active = 0, revoked_at = ? WHERE client_id = ?",
                (moment, client_id),
            )
            if cur.rowcount == 0:
                raise EchoActError(Code.NOT_FOUND, "No such client.", detail={"id": client_id})

    @_guard
    def touch_client(self, client_id: str, *, at: float | None = None) -> None:
        """F-71 shows a client's last access time."""
        moment = ids.now() if at is None else at
        with self.transaction() as conn:
            conn.execute(
                "UPDATE clients SET last_seen_at = ? WHERE client_id = ?", (moment, client_id)
            )

    # ==================================================================
    # Backup records (N-15, F-44 bookkeeping only)
    # ==================================================================

    @_guard
    def list_backups(self, *, kind: str | None = None) -> tuple[BackupRecord, ...]:
        conn = self._conn()
        if kind is None:
            rows = conn.execute("SELECT * FROM backups ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM backups WHERE kind = ? ORDER BY created_at DESC", (kind,)
            ).fetchall()
        return tuple(_to_backup(r) for r in rows)

    @_guard
    def record_backup(
        self,
        *,
        location: str,
        kind: str = "manual",
        byte_size: int = 0,
        item_count: int = 0,
        verified: bool = False,
        note: str | None = None,
        app_version: str | None = None,
        at: float | None = None,
    ) -> BackupRecord:
        """Bookkeeping for a bundle another module produced.

        The store records that a copy exists and at which schema version;
        producing, verifying, and restoring the bundle are F-44's and N-27's
        and live outside this package.
        """
        moment = ids.now() if at is None else at
        record = BackupRecord(
            backup_id=ids.backup_id(),
            kind=kind,
            created_at=moment,
            location=location,
            byte_size=byte_size,
            item_count=item_count,
            schema_version=self._schema_version,
            app_version=app_version or __version__,
            verified=verified,
            note=note,
        )
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO backups (backup_id, kind, created_at, location, byte_size,"
                " item_count, schema_version, app_version, verified, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.backup_id,
                    record.kind,
                    record.created_at,
                    record.location,
                    record.byte_size,
                    record.item_count,
                    record.schema_version,
                    record.app_version,
                    int(record.verified),
                    record.note,
                ),
            )
        return record

    @_guard
    def delete_backup_record(self, backup_id: str) -> None:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM backups WHERE backup_id = ?", (backup_id,))
            if cur.rowcount == 0:
                raise EchoActError(Code.NOT_FOUND, "No such backup.", detail={"id": backup_id})

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _count(conn: sqlite3.Connection, table: str, column: str, value: Any) -> int:
        return int(
            conn.execute(f"SELECT count(*) FROM {table} WHERE {column} = ?", (value,)).fetchone()[0]
        )


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_S",
    "BackupRecord",
    "ClientRecord",
    "Deletion",
    "DeletionScope",
    "DocumentSummary",
    "IdempotencyRecord",
    "JobSummary",
    "Page",
    "ReconcileReport",
    "ResultIntegrity",
    "StorageUsage",
    "Store",
    "request_match_digest",
    "text_bytes",
    "translate_sqlite_error",
]
