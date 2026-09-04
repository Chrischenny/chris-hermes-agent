"""Versioned SQLite schema for durable task and context state."""

from __future__ import annotations

import logging
import sqlite3

SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)

_CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    parent_task_id TEXT REFERENCES tasks(task_id),
    title TEXT NOT NULL,
    created_session_id TEXT NOT NULL,
    last_session_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    current_phase TEXT NOT NULL,
    completed_json TEXT NOT NULL,
    in_progress_json TEXT NOT NULL,
    known_issues_json TEXT NOT NULL,
    next_actions_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('active', 'paused', 'blocked', 'completed', 'cancelled')
    ),
    search_aliases_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    paused_at TEXT,
    last_resumed_at TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0 CHECK (resume_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_updated
    ON tasks(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_sequence
    ON task_events(task_id, sequence);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    current_phase TEXT NOT NULL,
    completed_json TEXT NOT NULL,
    current_state_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    rejected_alternatives_json TEXT NOT NULL,
    known_issues_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    next_actions_json TEXT NOT NULL,
    content_checksum TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_task_created
    ON checkpoints(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS context_segments (
    context_segment_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    parent_segment_id TEXT REFERENCES context_segments(context_segment_id),
    checkpoint_id TEXT REFERENCES checkpoints(checkpoint_id),
    start_message_index INTEGER NOT NULL CHECK (start_message_index >= 0),
    end_message_index INTEGER,
    start_time TEXT NOT NULL,
    end_time TEXT,
    handoff_reason TEXT,
    handoff_policy_snapshot TEXT,
    archived_context_reference TEXT,
    start_message_checksum TEXT
);

CREATE INDEX IF NOT EXISTS idx_segments_task_start
    ON context_segments(task_id, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_segments_session_start
    ON context_segments(session_id, start_time DESC);

CREATE TABLE IF NOT EXISTS session_context_state (
    session_id TEXT PRIMARY KEY,
    active_task_id TEXT REFERENCES tasks(task_id),
    active_context_segment_id TEXT REFERENCES context_segments(context_segment_id),
    handoff_pending INTEGER NOT NULL DEFAULT 0 CHECK (handoff_pending IN (0, 1)),
    pending_checkpoint_id TEXT REFERENCES checkpoints(checkpoint_id),
    last_handoff_at TEXT,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0)
);

CREATE TABLE IF NOT EXISTS task_search_fallback (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    search_text TEXT NOT NULL
);
"""


def _has_wal_reset_corruption_bug(version_info: tuple[int, ...]) -> bool:
    """Match SQLite's published WAL-reset vulnerability and backports."""
    version = (*version_info, 0, 0, 0)[:3]
    if version < (3, 7, 0) or version >= (3, 51, 3):
        return False
    if (3, 50, 7) <= version < (3, 51, 0):
        return False
    return not ((3, 44, 6) <= version < (3, 45, 0))


def _configure_journal_mode(connection: sqlite3.Connection) -> None:
    version = sqlite3.sqlite_version_info
    if _has_wal_reset_corruption_bug(version):
        connection.execute("PRAGMA journal_mode=DELETE")
        logger.warning(
            "chris-hermes-agent: SQLite %s has the WAL-reset corruption bug; "
            "using journal_mode=DELETE for plugin state.",
            sqlite3.sqlite_version,
        )
        return
    connection.execute("PRAGMA journal_mode=WAL")


def initialize_database(connection: sqlite3.Connection) -> str:
    """Apply idempotent migrations and return the selected search backend."""
    _configure_journal_mode(connection)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema {current_version} is newer than supported "
            f"version {SCHEMA_VERSION}."
        )
    connection.executescript(_CORE_SCHEMA)
    _ensure_segment_anchor_checksum(connection)
    backend = _initialize_search_backend(connection)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
    return backend


def _ensure_segment_anchor_checksum(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(context_segments)")
    }
    if "start_message_checksum" not in columns:
        connection.execute(
            "ALTER TABLE context_segments ADD COLUMN start_message_checksum TEXT"
        )


def _initialize_search_backend(connection: sqlite3.Connection) -> str:
    existing = connection.execute(
        "SELECT value FROM plugin_metadata WHERE key = 'search_backend'"
    ).fetchone()
    if existing is not None:
        return str(existing[0])
    backend = "fallback"
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS task_search_fts "
            "USING fts5(task_id UNINDEXED, search_text, tokenize='trigram')"
        )
        backend = "fts5_trigram"
    except sqlite3.OperationalError:
        backend = "fallback"
    connection.execute(
        "INSERT INTO plugin_metadata(key, value) VALUES('search_backend', ?)",
        (backend,),
    )
    return backend
