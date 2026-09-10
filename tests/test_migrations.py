"""Schema versioning: N-15's backup-before-change and refusal to touch a
newer format, and N-14's "previously sound data is preserved" applied to the
schema itself."""

from __future__ import annotations

import sqlite3

import pytest

from echoact import paths
from echoact.db import migrations
from echoact.db.store import Store
from echoact.errors import Code, EchoActError


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path))
    paths.data_dir.cache_clear()
    paths.ensure_tree()
    yield tmp_path
    paths.data_dir.cache_clear()


def _raw(path) -> sqlite3.Connection:
    """A connection that manages its own transactions, as ``migrate`` expects."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _add_note_column(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE documents ADD COLUMN note TEXT")


def _fails_midway(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE halfway (x TEXT)")
    raise sqlite3.OperationalError("the migration failed here")


# ======================================================================
# Creating and re-opening
# ======================================================================


def test_a_new_database_is_created_at_the_version_this_build_supports(data_dir):
    conn = _raw(paths.db_path())
    try:
        report = migrations.migrate(conn)

        assert report.from_version == 0
        assert report.to_version == migrations.SUPPORTED_SCHEMA_VERSION
        assert report.applied == (1,)
        assert migrations.current_version(conn) == migrations.SUPPORTED_SCHEMA_VERSION
        assert {"documents", "jobs", "segments", "results", "clients", "idempotency",
                "backups", "schema_version"} <= _tables(conn)
    finally:
        conn.close()


def test_creating_an_empty_database_takes_no_backup_because_there_is_nothing_to_secure(data_dir):
    conn = _raw(paths.db_path())
    try:
        report = migrations.migrate(conn)

        assert report.backup_path is None
        assert conn.execute("SELECT count(*) FROM backups").fetchone()[0] == 0
    finally:
        conn.close()


def test_migrating_an_up_to_date_database_changes_nothing(data_dir):
    conn = _raw(paths.db_path())
    try:
        migrations.migrate(conn)
        again = migrations.migrate(conn)

        assert again.applied == ()
        assert again.changed is False
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_each_applied_migration_is_recorded_once_and_in_order(data_dir, monkeypatch):
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        migrations.MIGRATIONS + (migrations.Migration(2, "add a note", _add_note_column),),
    )
    monkeypatch.setattr(migrations, "SUPPORTED_SCHEMA_VERSION", 2)
    conn = _raw(paths.db_path())
    try:
        report = migrations.migrate(conn)

        assert report.applied == (1, 2)
        rows = conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(1, "initial schema"), (2, "add a note")]
        assert "note" in _columns(conn, "documents")
    finally:
        conn.close()


# ======================================================================
# N-15: an older build must not damage a newer format
# ======================================================================


def test_a_database_from_a_newer_build_is_refused_and_left_unmodified(data_dir):
    store = Store()
    store.save_document("kept", "sound body", at=1.0)
    store.close()
    conn = _raw(paths.db_path())
    conn.execute(
        "INSERT INTO schema_version (version, applied_at, description)"
        " VALUES (99, 0.0, 'written by a later build')"
    )

    try:
        with pytest.raises(EchoActError) as exc:
            migrations.migrate(conn)

        assert exc.value.code is Code.BACKUP_INCOMPATIBLE
        assert exc.value.retryable is False, "a newer format never becomes readable by waiting"
        assert exc.value.detail["found_version"] == 99
        assert exc.value.detail["supported_version"] == migrations.SUPPORTED_SCHEMA_VERSION
        # Nothing was written: no downgrade row, no lost document.
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 99
        assert [tuple(r) for r in conn.execute("SELECT title, body FROM documents")] == [
            ("kept", "sound body")
        ]
    finally:
        conn.close()


def test_the_store_refuses_to_open_a_database_from_a_newer_build(data_dir):
    first = Store()
    first.save_document("kept", "sound body", at=1.0)
    first.close()
    conn = _raw(paths.db_path())
    conn.execute(
        "INSERT INTO schema_version (version, applied_at, description) VALUES (2, 0.0, 'later')"
    )
    conn.close()

    with pytest.raises(EchoActError) as exc:
        Store()

    assert exc.value.code is Code.BACKUP_INCOMPATIBLE


def test_a_format_change_is_preceded_by_a_recoverable_backup(data_dir, monkeypatch):
    store = Store()
    store.save_document("kept", "sound body", at=1.0)
    store.close()
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        migrations.MIGRATIONS + (migrations.Migration(2, "add a note", _add_note_column),),
    )
    monkeypatch.setattr(migrations, "SUPPORTED_SCHEMA_VERSION", 2)

    conn = _raw(paths.db_path())
    try:
        report = migrations.migrate(conn, backup_dir=data_dir / "backups", at=5.0)

        assert report.applied == (2,)
        assert report.backup_path is not None and report.backup_path.exists()

        copy = _raw(report.backup_path)
        try:
            assert copy.execute("SELECT title FROM documents").fetchone()[0] == "kept"
            assert "note" not in _columns(copy, "documents"), "the copy predates the change"
        finally:
            copy.close()

        recorded = conn.execute(
            "SELECT kind, schema_version, location, verified FROM backups"
        ).fetchall()
        assert len(recorded) == 1
        assert recorded[0]["kind"] == "pre_migration"
        assert recorded[0]["schema_version"] == 1
        assert recorded[0]["location"] == str(report.backup_path)
    finally:
        conn.close()


def test_a_migration_that_fails_leaves_the_database_at_the_previous_version(data_dir, monkeypatch):
    store = Store()
    store.save_document("kept", "sound body", at=1.0)
    store.close()
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        migrations.MIGRATIONS + (migrations.Migration(2, "breaks", _fails_midway),),
    )
    monkeypatch.setattr(migrations, "SUPPORTED_SCHEMA_VERSION", 2)

    conn = _raw(paths.db_path())
    try:
        with pytest.raises(EchoActError):
            migrations.migrate(conn, backup_dir=data_dir / "backups")

        assert migrations.current_version(conn) == 1
        assert "halfway" not in _tables(conn), "the failed migration's own work rolled back"
        assert conn.execute("SELECT title FROM documents").fetchone()[0] == "kept"
        assert conn.execute("SELECT count(*) FROM backups").fetchone()[0] == 1
    finally:
        conn.close()


# ======================================================================
# The schema file itself
# ======================================================================


def test_a_trigger_body_survives_statement_splitting():
    statements = migrations.split_statements(migrations.schema_section("fts5"))

    assert len(statements) == 4
    assert statements[0].startswith("CREATE VIRTUAL TABLE documents_fts")
    assert all(s.rstrip().endswith("END") for s in statements[1:])
    assert all("CREATE TRIGGER" in s for s in statements[1:])


def test_the_core_schema_splits_into_one_statement_per_object():
    statements = migrations.split_statements(migrations.schema_section("core"))

    assert all(s.upper().startswith("CREATE") for s in statements)
    assert sum(1 for s in statements if s.upper().startswith("CREATE TABLE")) == 8


def test_segment_offsets_are_named_as_half_open_code_point_ranges(data_dir):
    store = Store()
    try:
        conn = _raw(paths.db_path())
        try:
            columns = _columns(conn, "segments")
        finally:
            conn.close()
    finally:
        store.close()

    assert "source_start_codepoint_inclusive" in columns
    assert "source_end_codepoint_exclusive" in columns


# ======================================================================
# F-40 without FTS5
# ======================================================================


def test_a_build_without_fts5_still_gets_a_working_searchable_database(data_dir, monkeypatch):
    monkeypatch.setattr(migrations, "fts5_available", lambda conn: False)

    store = Store()
    try:
        assert store.fts_enabled is False
        store.save_document("회의록", "안녕하세요 반갑습니다")
        store.save_document("other", "nothing relevant")

        found = store.search_documents("녕하세")

        assert [d.title for d in found.items] == ["회의록"]
        conn = _raw(paths.db_path())
        try:
            assert "documents_fts" not in _tables(conn)
        finally:
            conn.close()
    finally:
        store.close()


def test_the_index_is_present_where_the_build_supports_it(data_dir):
    store = Store()
    try:
        conn = _raw(paths.db_path())
        try:
            available = migrations.fts5_available(conn)
            present = migrations.has_fts_index(conn)
        finally:
            conn.close()
        assert available == present == store.fts_enabled
    finally:
        store.close()
