"""
Opening a workspace created by an older version.

`CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it was, so a
column added later never appears in an older database. The failure that makes
this worth testing is its timing: nothing goes wrong at startup, and then the
first query naming the new column raises "no such column" — in a view, at the
moment someone opens it.

Each test builds a real database in the old shape and opens a Store on it.
"""

import logging
import sqlite3

import pytest

from control_plane.storage.schema import ADDITIVE_COLUMNS, SCHEMA_VERSION
from control_plane.storage.store import Store
from control_plane.telemetry.events import Component, Event, EventType


def _v1_database(path: str) -> None:
    """A `candidates` table as it was before generation provenance existed."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES('version', '1');
        CREATE TABLE candidates (
            run_id         TEXT NOT NULL,
            candidate_id   TEXT NOT NULL,
            parent_id      TEXT,
            generation     INTEGER,
            iteration      INTEGER,
            island_id      INTEGER,
            created_at     REAL,
            candidate_type TEXT DEFAULT 'code',
            language       TEXT,
            combined_score REAL,
            metrics        TEXT NOT NULL DEFAULT '{}',
            complexity     REAL,
            diversity      REAL,
            code_hash      TEXT,
            code_length    INTEGER,
            changes_summary TEXT,
            map_elites_cell TEXT,
            is_best        INTEGER DEFAULT 0,
            eval_status    TEXT,
            metadata       TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (run_id, candidate_id)
        );
        INSERT INTO candidates(run_id, candidate_id, combined_score)
        VALUES('run_old', 'cand_old', 0.5);
    """)
    conn.commit()
    conn.close()


def _columns(store: Store, table: str) -> set:
    return {r[1] for r in store.reader().execute(f"PRAGMA table_info({table})")}


def test_an_old_database_gains_the_columns_it_is_missing(tmp_path):
    path = str(tmp_path / "old.db")
    _v1_database(path)
    store = Store(path)
    try:
        assert ADDITIVE_COLUMNS["candidates"].keys() <= _columns(store, "candidates")
    finally:
        store.close()


def test_the_query_that_used_to_fail_now_runs(tmp_path):
    """The actual symptom: a view opening and raising "no such column"."""
    path = str(tmp_path / "old.db")
    _v1_database(path)
    store = Store(path)
    try:
        rows = store.query(
            "SELECT gen_provider, gen_model, gen_operator FROM candidates "
            "WHERE run_id = ?", ("run_old",))
        assert rows == [{"gen_provider": None, "gen_model": None,
                         "gen_operator": None}]
    finally:
        store.close()


def test_existing_rows_survive_the_migration(tmp_path):
    """ALTER TABLE ADD COLUMN, not a drop and recreate."""
    path = str(tmp_path / "old.db")
    _v1_database(path)
    store = Store(path)
    try:
        row = store.query_one(
            "SELECT candidate_id, combined_score FROM candidates")
        assert row == {"candidate_id": "cand_old", "combined_score": 0.5}
    finally:
        store.close()


def test_the_version_is_recorded_after_migrating(tmp_path):
    path = str(tmp_path / "old.db")
    _v1_database(path)
    store = Store(path)
    try:
        assert store.query_one(
            "SELECT value FROM schema_meta WHERE key='version'"
        )["value"] == str(SCHEMA_VERSION)
    finally:
        store.close()


def test_a_structural_change_is_reported_rather_than_silently_skipped(tmp_path, caplog):
    """
    A changed primary key cannot be ALTERed in. Saying so beats a projection
    that keeps serving the old shape — a v1 database merged every island's
    feature map into one set of MAP-Elites cells.
    """
    path = str(tmp_path / "old.db")
    _v1_database(path)
    with caplog.at_level(logging.WARNING):
        store = Store(path)
    try:
        messages = [r.getMessage() for r in caplog.records]
        assert any("map_elites_cells" in m for m in messages)
        assert any("rebuild_projections_from_log" in m for m in messages)
    finally:
        store.close()


def test_a_current_database_migrates_silently(tmp_path, caplog):
    """No warning when there is nothing to warn about."""
    path = str(tmp_path / "new.db")
    Store(path).close()
    with caplog.at_level(logging.WARNING):
        store = Store(path)
    try:
        assert not [r for r in caplog.records if "schema" in r.getMessage().lower()]
    finally:
        store.close()


def test_reopening_is_idempotent(tmp_path):
    path = str(tmp_path / "old.db")
    _v1_database(path)
    for _ in range(3):
        store = Store(path)
        cols = [r[1] for r in store.reader().execute("PRAGMA table_info(candidates)")]
        store.close()
    assert len(cols) == len(set(cols)), "a column was added twice"


def test_a_migrated_database_can_still_ingest(tmp_path):
    """The migration has to leave a working store, not just a valid schema."""
    path = str(tmp_path / "old.db")
    _v1_database(path)
    store = Store(path)
    try:
        store.ingest([Event(
            type=EventType.EXPERIMENT_CREATED, component=Component.CONTROL_PLANE,
            run_id="run_new", experiment_id="exp_new", metadata={"name": "t"})])
        store.ingest([Event(
            type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
            run_id="run_new", candidate_id="cand_new", summary="added",
            metadata={"generating_request_id": "req_1",
                      "generating_provider": "opencode_zen",
                      "generating_model": "hy3-free"})])
        row = store.query_one(
            "SELECT gen_provider FROM candidates WHERE candidate_id='cand_new'")
        assert row["gen_provider"] == "opencode_zen"
    finally:
        store.close()
