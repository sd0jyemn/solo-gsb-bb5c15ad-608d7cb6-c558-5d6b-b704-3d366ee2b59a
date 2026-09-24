"""SQLite connection handling, schema and initial seeding."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from .seed import seed_items

# Default keeps local runs working out of the box; the Docker image sets this
# to /data/quiz.db (a named volume) so data survives container restarts.
DB_PATH = Path(os.getenv("QUIZ_DB_PATH", "data/quiz.db"))

SCHEMA_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id               TEXT PRIMARY KEY,
    theta            REAL NOT NULL,
    current_item_id  TEXT REFERENCES items(id), -- item currently served, not yet answered
    max_items        INTEGER NOT NULL,
    exposure_limit   INTEGER NOT NULL,
    duration_seconds INTEGER, -- optional time limit in seconds (60..86400); NULL = no limit
    expires_at       TEXT,    -- UTC ISO-8601 deadline (recomputed after every resume)
    target_se        REAL,    -- optional precision-stop target SE (positive finite); NULL = off
    min_precision_items INTEGER, -- optional min answers before precision stop (1..max_items)
    -- Pause support.  A paused session keeps status='paused' while the clock is
    -- frozen: paused_at records when the pause began and remaining_seconds caches
    -- the time left at that instant; both are NULL unless the session is paused.
    paused_at        TEXT,
    remaining_seconds INTEGER,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'paused', 'finished')),
    finish_reason    TEXT, -- 'max_items' | 'no_available_items'
                     -- 'time_expired' | 'tag_minimums_unfulfillable'
                     -- 'precision_reached'
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id             TEXT PRIMARY KEY,
    difficulty     REAL NOT NULL,
    discrimination REAL NOT NULL,
    content_tag    TEXT,
    stem           TEXT NOT NULL,
    options        TEXT NOT NULL, -- JSON: [{"key": "A", "text": "..."}]
    answer         TEXT NOT NULL, -- correct option key, e.g. "B"
    exposure_count INTEGER NOT NULL DEFAULT 0,
    -- Item withdrawal: 1 = enabled (eligible for new selections), 0 = withdrawn.
    -- Legacy databases acquire the column via ALTER TABLE ... DEFAULT 1, so
    -- every pre-existing item migrates to "enabled".
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

""" + SCHEMA_SESSIONS + """
-- Optional per-session minimum answer quotas keyed by content tag.  A new
-- table (created IF NOT EXISTS) also serves as the migration: databases from
-- older versions simply acquire the table on startup and existing sessions
-- have no rows, so their behaviour is unchanged.
CREATE TABLE IF NOT EXISTS session_tag_minimums (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    content_tag TEXT NOT NULL,
    minimum     INTEGER NOT NULL CHECK (minimum >= 1),
    PRIMARY KEY (session_id, content_tag)
);

CREATE TABLE IF NOT EXISTS responses (
    id                        TEXT PRIMARY KEY, -- client-supplied idempotency key
    session_id                TEXT NOT NULL REFERENCES sessions(id),
    item_id                   TEXT NOT NULL REFERENCES items(id),
    response                  TEXT NOT NULL,
    score                     INTEGER NOT NULL CHECK (score IN (0, 1)),
    correct_answer            TEXT NOT NULL,
    theta_before              REAL NOT NULL,
    theta_after               REAL NOT NULL,
    p_correct                 REAL NOT NULL,
    information_at_theta      REAL NOT NULL,
    exposure_count_at_select  INTEGER NOT NULL,
    created_at                TEXT NOT NULL,
    UNIQUE (session_id, item_id) -- an item can only be answered once per session
);

CREATE INDEX IF NOT EXISTS idx_responses_session ON responses(session_id);
"""


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: a per-request SQLite connection.

    ``check_same_thread=False`` is required because FastAPI may run a sync
    dependency's enter, the endpoint and its exit on different threadpool
    workers under load.  Usage is still strictly serial (anyio awaits each
    stage), so a connection is never touched by two threads at once.
    ``busy_timeout`` makes competing BEGIN IMMEDIATE writers wait in a queue
    instead of raising "database is locked" immediately.
    """
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    """Add a column to an existing table if it is missing (idempotent)."""
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# Columns copied verbatim when rebuilding the sessions table for the pause
# migration; kept in sync with the canonical CREATE TABLE above.
_SESSION_COLUMNS = (
    "id, theta, current_item_id, max_items, exposure_limit, duration_seconds, "
    "expires_at, target_se, min_precision_items, paused_at, remaining_seconds, "
    "status, finish_reason, created_at, updated_at"
)


def _migrate_pause_support(conn: sqlite3.Connection) -> None:
    """Bring pre-pause databases up to date.

    Adds the ``paused_at`` / ``remaining_seconds`` columns and widens the
    ``status`` CHECK constraint to allow ``'paused'``.  A CHECK clause cannot
    be altered in place, so the classic SQLite table rebuild is used: rename
    the old table, create the new one with the canonical schema, copy every
    row, drop the old table.  The rebuild runs in autocommit mode with an
    explicit transaction; Python's implicit DML/DDL transaction handling
    would otherwise defer the statements and leave a dangling
    ``sessions_old`` table behind.

    A brand-new database has no sessions table yet; executescript in
    init_db creates the canonical one, so there is nothing to migrate.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    if exists is None:
        return

    _ensure_column(conn, "sessions", "paused_at", "TEXT")
    _ensure_column(conn, "sessions", "remaining_seconds", "INTEGER")

    create_sql = next(
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
    )
    if "'paused'" in create_sql:
        return  # CHECK constraint already allows the paused status

    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # autocommit: manage the rebuild transaction
    try:
        # Keep responses/session_tag_minimums pointing at the sessions name:
        # a modern SQLite RENAME would otherwise rewrite their foreign-key
        # references to sessions_old and make the final DROP fail.
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE sessions RENAME TO sessions_old")
        conn.execute(SCHEMA_SESSIONS)
        conn.execute(
            f"INSERT INTO sessions ({_SESSION_COLUMNS}) "
            f"SELECT {_SESSION_COLUMNS} FROM sessions_old"
        )
        conn.execute("DROP TABLE sessions_old")
        conn.commit()
        conn.execute("PRAGMA legacy_alter_table = OFF")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.isolation_level = previous_isolation


def init_db() -> None:
    """Create tables (idempotent) and seed a deterministic demo bank once."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        # Columns added by earlier feature releases must exist *before* the
        # pause migration's table rebuild, because the rebuild copies every
        # column by name (including the ones a legacy database lacks).
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone() is not None:
            _ensure_column(conn, "sessions", "duration_seconds", "INTEGER")
            _ensure_column(conn, "sessions", "expires_at", "TEXT")
            _ensure_column(conn, "sessions", "target_se", "REAL")
            _ensure_column(conn, "sessions", "min_precision_items", "INTEGER")

        # Item withdrawal: a legacy item bank acquires the is_active column
        # with DEFAULT 1, i.e. every existing item migrates to "enabled".
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
        ).fetchone() is not None:
            _ensure_column(
                conn, "items", "is_active", "INTEGER NOT NULL DEFAULT 1"
            )

        # Rebuild the sessions table on legacy databases (widening the status
        # CHECK requires it); executescript below then only creates the
        # objects that are still missing.
        _migrate_pause_support(conn)
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if count == 0:
            conn.executemany(
                """
                INSERT INTO items
                    (id, difficulty, discrimination, content_tag, stem,
                     options, answer, exposure_count)
                VALUES
                    (:id, :difficulty, :discrimination, :content_tag, :stem,
                     :options, :answer, 0)
                """,
                seed_items(),
            )
        conn.commit()
