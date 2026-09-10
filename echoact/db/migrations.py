"""Versioned, forward-only schema migrations.

N-15 sets the two rules this module exists for:

* A recoverable backup is secured *before* any data format change.  The copy
  is taken with SQLite's online backup API, which yields a consistent file
  even while the write-ahead log is in use, and it is committed to the
  ``backups`` table before the first migration runs so the record survives a
  migration that then fails.
* An older application that opens a newer data format must not modify it
  destructively.  Every open therefore reads the version first and refuses
  outright when it is beyond what this build understands.  Refusing is not a
  fallback for "try and see": a v2 build may have added a column this build
  would silently drop out of every ``INSERT``.

Forward-only is deliberate.  A downgrade path would have to invent what the
removed information used to be, and N-15's answer to going backwards is the
backup taken here, not a reverse migration.

The error translation for the whole ``echoact.db`` package lives here rather
than in ``store`` because ``store`` imports this module and both need it;
rule 3 of the working notes forbids letting ``sqlite3.Error`` escape either.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from .. import __version__
from ..errors import Code, EchoActError
from ..paths import data_dir
from ..util.ids import backup_id as new_backup_id
from ..util.ids import now as wall_now

#: The newest schema this build can read and write.  A database at a higher
#: version is refused; a database at a lower one is migrated up.
SUPPORTED_SCHEMA_VERSION: Final = 1

_SCHEMA_FILE: Final = Path(__file__).with_name("schema.sql")
_SECTION = re.compile(r"^--\s*@section:\s*(\w+)\s*$", re.MULTILINE)
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


# ======================================================================
# Error translation
# ======================================================================


def translate_sqlite_error(exc: BaseException, *, retry_after_s: float | None = None) -> EchoActError:
    """Map a raw driver failure onto the one exception type that leaves this
    package, with the code Section 5.3 names for it.

    N-14 forbids two outcomes in particular: waiting indefinitely on a lock,
    and reporting success.  A lock that outlives the busy timeout arrives
    here as ``OperationalError("database is locked")`` and becomes
    ``DB_LOCKED`` -- a retryable refusal with a hint -- rather than a stall.
    A full disk arrives as "database or disk is full" and becomes
    ``STORAGE_FULL``, which is *not* retryable, because retrying without
    freeing space cannot succeed.
    """
    text = str(exc).lower()
    if "disk is full" in text or "disk full" in text or "database or disk is full" in text:
        return EchoActError(Code.STORAGE_FULL, detail={"driver": type(exc).__name__}, cause=exc)
    if "locked" in text or "busy" in text:
        return EchoActError(
            Code.DB_LOCKED,
            detail={"driver": type(exc).__name__},
            retry_after_s=retry_after_s,
            cause=exc,
        )
    if isinstance(exc, sqlite3.IntegrityError):
        # A constraint violation is this application contradicting itself --
        # a duplicate identifier, a segment pointing at no job.  It is a
        # defect, not an operating condition, so it must not look retryable.
        return EchoActError(
            Code.INTERNAL, "The database rejected an inconsistent write.", cause=exc
        )
    if isinstance(exc, sqlite3.ProgrammingError) or any(
        marker in text for marker in ("no such column", "no such table", "syntax error")
    ):
        # A malformed statement is a defect in this application, and
        # DB_UNAVAILABLE would advertise it as worth retrying (N-23).
        return EchoActError(Code.INTERNAL, "The database rejected a malformed query.", cause=exc)
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:  # ENOSPC
        return EchoActError(Code.STORAGE_FULL, cause=exc)
    return EchoActError(
        Code.DB_UNAVAILABLE, detail={"driver": type(exc).__name__}, cause=exc
    )


# ======================================================================
# schema.sql
# ======================================================================


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    """Split ``schema.sql`` on its ``-- @section:`` markers."""
    text = _SCHEMA_FILE.read_text(encoding="utf-8")
    marks = list(_SECTION.finditer(text))
    if not marks:
        raise EchoActError(Code.INTERNAL, "schema.sql has no @section markers")
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[m.group(1)] = text[m.end() : end]
    return out


def schema_section(name: str) -> str:
    return _sections()[name]


def split_statements(sql: str) -> list[str]:
    """Split a script into statements, keeping trigger bodies whole.

    ``executescript`` is not used anywhere in this module: it commits any
    open transaction before it runs, which would defeat the one-transaction-
    per-migration rule this module is built around.  So the splitting is done
    here, and it has to understand that the ``;`` inside a ``CREATE TRIGGER
    ... BEGIN ... END;`` body does not end a statement.
    """
    out: list[str] = []
    cur: list[str] = []
    block = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            cur.append(sql[i : j + 1])
            i = j + 1
            continue
        if sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl < 0 else nl + 1
            cur.append("\n")
            continue
        word = _WORD.match(sql, i)
        if word:
            upper = word.group(0).upper()
            if upper == "BEGIN":
                block += 1
            elif upper == "END":
                block = max(0, block - 1)
            cur.append(word.group(0))
            i = word.end()
            continue
        if ch == ";" and block == 0:
            stmt = "".join(cur).strip()
            if stmt:
                out.append(stmt)
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _run(conn: sqlite3.Connection, sql: str) -> None:
    for statement in split_statements(sql):
        conn.execute(statement)


# ======================================================================
# Capability probes
# ======================================================================


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Whether this SQLite build can create an FTS5 index (F-40).

    Probed with ``PRAGMA compile_options`` rather than by attempting the
    ``CREATE`` and catching the failure, because the attempt would have to
    happen inside the migration's transaction and a failed DDL statement
    there is a rollback we do not want to reason about.
    """
    try:
        return any(row[0] == "ENABLE_FTS5" for row in conn.execute("PRAGMA compile_options"))
    except sqlite3.Error:
        return False


def has_fts_index(conn: sqlite3.Connection) -> bool:
    """Whether *this database* carries the FTS5 objects.

    Availability is a property of the build; presence is a property of the
    file, and they can disagree.  The index is never added to an existing
    database outside a migration: its triggers fire on every document write,
    so a build without FTS5 could not so much as save a document into a file
    that has them.  That makes adding them a format change, which N-15 puts
    behind a version bump and a backup.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents_fts'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# ======================================================================
# Migrations
# ======================================================================


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _migration_1(conn: sqlite3.Connection) -> None:
    """The initial schema.

    The FTS5 section is applied only where the build supports it; F-40's
    search then runs over the index, and ``Store`` falls back to ``LIKE``
    where it is absent so that search degrades rather than vanishing.
    """
    _run(conn, schema_section("core"))
    if fts5_available(conn):
        _run(conn, schema_section("fts5"))


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, "initial schema", _migration_1),
)


@dataclass(frozen=True, slots=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied: tuple[int, ...]
    backup_path: Path | None
    fts5: bool

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def current_version(conn: sqlite3.Connection) -> int:
    """0 for a database that has never been migrated."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        found = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0]
    except sqlite3.Error as exc:
        raise translate_sqlite_error(exc) from exc
    return int(found or 0)


def assert_compatible(conn: sqlite3.Connection) -> int:
    """N-15: refuse a database written by a newer build, without touching it.

    Returns the version so a caller can decide whether to migrate.  The code
    is ``BACKUP_INCOMPATIBLE`` because it is the only non-retryable
    "incompatible version" code in the catalogue; ``DB_UNAVAILABLE`` is
    marked retryable and would invite a client to try again at something that
    will never change on its own, which N-23 forbids.
    """
    version = current_version(conn)
    if version > SUPPORTED_SCHEMA_VERSION:
        raise EchoActError(
            Code.BACKUP_INCOMPATIBLE,
            "This database was written by a newer version of EchoAct "
            f"(data format v{version}); this build understands v{SUPPORTED_SCHEMA_VERSION}. "
            "It has not been modified.",
            detail={"found_version": version, "supported_version": SUPPORTED_SCHEMA_VERSION},
        )
    return version


def default_backup_dir() -> Path:
    """Where a pre-migration copy goes.

    ``echoact.paths`` names every other location; it has no backup directory
    yet, so this module keeps the choice in one place until it does.
    """
    return data_dir() / "backups"


def _item_count(conn: sqlite3.Connection) -> int:
    total = 0
    for table in ("documents", "jobs", "results"):
        try:
            total += int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            continue
    return total


def take_backup(
    conn: sqlite3.Connection,
    *,
    directory: Path | None = None,
    kind: str = "pre_migration",
    at: float | None = None,
    note: str | None = None,
) -> Path:
    """Copy the live database to a new file and record it (N-15).

    The online backup API is used rather than a file copy because the source
    is open in write-ahead mode: a byte copy of the main file alone would
    miss everything still in the log.
    """
    moment = wall_now() if at is None else at
    version = current_version(conn)
    target_dir = directory or default_backup_dir()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(moment))
    dest = target_dir / f"{kind}-v{version}-{stamp}-{new_backup_id()}.sqlite3"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(dest)
        try:
            conn.backup(destination)
        finally:
            destination.close()
        size = dest.stat().st_size
        items = _item_count(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO backups (backup_id, kind, created_at, location, byte_size,"
            " item_count, schema_version, app_version, verified, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                new_backup_id(),
                kind,
                moment,
                str(dest),
                size,
                items,
                version,
                __version__,
                note,
            ),
        )
        conn.execute("COMMIT")
    except (sqlite3.Error, OSError) as exc:
        raise translate_sqlite_error(exc) from exc
    return dest


def migrate(
    conn: sqlite3.Connection,
    *,
    backup_dir: Path | None = None,
    at: float | None = None,
) -> MigrationReport:
    """Bring a database up to ``SUPPORTED_SCHEMA_VERSION``.

    Each pending migration runs in its own transaction together with its
    ``schema_version`` row, so a failure half way through leaves the database
    at the last version that completed rather than in a shape no build
    recognises -- which is N-14's "previously sound data is preserved even if
    a save fails midway" applied to the schema itself.
    """
    start = assert_compatible(conn)
    pending = tuple(m for m in MIGRATIONS if m.version > start)
    backup: Path | None = None

    # Creating an empty database is not a format change: there is nothing
    # recoverable to secure.  Every later step is.
    if pending and start > 0:
        backup = take_backup(conn, directory=backup_dir, at=at)

    applied: list[int] = []
    for migration in pending:
        try:
            conn.execute("BEGIN IMMEDIATE")
            migration.apply(conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                (migration.version, wall_now() if at is None else at, migration.description),
            )
            conn.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise translate_sqlite_error(exc) from exc
        applied.append(migration.version)

    return MigrationReport(
        from_version=start,
        to_version=current_version(conn),
        applied=tuple(applied),
        backup_path=backup,
        fts5=has_fts_index(conn),
    )


__all__ = [
    "MIGRATIONS",
    "SUPPORTED_SCHEMA_VERSION",
    "Migration",
    "MigrationReport",
    "assert_compatible",
    "current_version",
    "default_backup_dir",
    "fts5_available",
    "has_fts_index",
    "migrate",
    "schema_section",
    "split_statements",
    "take_backup",
    "translate_sqlite_error",
]
