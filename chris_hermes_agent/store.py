"""SQLite repositories and transaction boundaries for durable task state."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any

from .task_models import (
    CheckpointRecord,
    ContextSegmentRecord,
    EventType,
    SessionContextState,
    TaskEventRecord,
    TaskRecord,
    TaskSearchResult,
    TaskStatus,
)


class ConcurrentUpdateError(RuntimeError):
    """Raised when an optimistic-lock version no longer matches."""


_TASK_UPDATE_COLUMNS = frozenset(
    {
        "parent_task_id",
        "title",
        "last_session_id",
        "goal",
        "constraints",
        "current_phase",
        "completed",
        "in_progress",
        "known_issues",
        "next_actions",
        "decisions",
        "artifacts",
        "status",
        "search_aliases",
        "tags",
        "paused_at",
        "last_resumed_at",
        "resume_count",
    }
)
_JSON_TASK_COLUMNS = frozenset(
    {
        "constraints",
        "completed",
        "in_progress",
        "known_issues",
        "next_actions",
        "decisions",
        "artifacts",
        "search_aliases",
        "tags",
    }
)


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _tuple_from_json(value: str) -> tuple[str, ...]:
    loaded = json.loads(value)
    return tuple(str(item) for item in loaded)


class TaskRepository:
    """Thread-safe repository over one Hermes plugin database connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        row = self._connection.execute(
            "SELECT value FROM plugin_metadata WHERE key = 'search_backend'"
        ).fetchone()
        self.search_backend = str(row[0]) if row is not None else "fallback"

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            owns_transaction = not self._connection.in_transaction
            if owns_transaction:
                self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                if owns_transaction:
                    self._connection.rollback()
                raise
            else:
                if owns_transaction:
                    self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_task(self, task: TaskRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO tasks(
                task_id, parent_task_id, title, created_session_id,
                last_session_id, goal, constraints_json, current_phase,
                completed_json, in_progress_json, known_issues_json,
                next_actions_json, decisions_json, artifacts_json, status,
                search_aliases_json, tags_json, paused_at, last_resumed_at,
                resume_count, created_at, updated_at, version
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                task.task_id,
                task.parent_task_id,
                task.title,
                task.created_session_id,
                task.last_session_id,
                task.goal,
                _json_dump(task.constraints),
                task.current_phase,
                _json_dump(task.completed),
                _json_dump(task.in_progress),
                _json_dump(task.known_issues),
                _json_dump(task.next_actions),
                _json_dump(task.decisions),
                _json_dump(task.artifacts),
                task.status.value,
                _json_dump(task.search_aliases),
                _json_dump(task.tags),
                task.paused_at,
                task.last_resumed_at,
                task.resume_count,
                task.created_at,
                task.updated_at,
                task.version,
            ),
        )
        self._sync_search_index(task)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def list_tasks(
        self,
        statuses: Sequence[TaskStatus] = (),
        limit: int = 20,
    ) -> tuple[TaskRecord, ...]:
        query = "SELECT * FROM tasks"
        params: list[object] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(status.value for status in statuses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return tuple(self._task_from_row(row) for row in rows)

    def update_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        changes: Mapping[str, object],
        updated_at: str,
    ) -> TaskRecord:
        unknown = set(changes) - _TASK_UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"Unsupported task update fields: {sorted(unknown)}")
        assignments: list[str] = []
        values: list[object] = []
        for field, value in changes.items():
            column = f"{field}_json" if field in _JSON_TASK_COLUMNS else field
            if field in _JSON_TASK_COLUMNS:
                value = _json_dump(value)
            elif field == "status" and isinstance(value, TaskStatus):
                value = value.value
            assignments.append(f"{column} = ?")
            values.append(value)
        assignments.extend(["updated_at = ?", "version = version + 1"])
        values.extend([updated_at, task_id, expected_version])
        cursor = self._connection.execute(
            f"UPDATE tasks SET {', '.join(assignments)} "
            "WHERE task_id = ? AND version = ?",
            values,
        )
        if cursor.rowcount != 1:
            raise ConcurrentUpdateError(
                f"Task {task_id!r} version changed from {expected_version}."
            )
        task = self.get_task(task_id)
        if task is None:  # pragma: no cover - guarded by the successful update
            raise RuntimeError("Updated task disappeared.")
        self._sync_search_index(task)
        return task

    def append_event(
        self,
        *,
        task_id: str,
        event_type: EventType,
        payload: Mapping[str, Any],
        session_id: str,
        created_at: str,
        event_id: str,
    ) -> TaskEventRecord:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        sequence = int(row[0])
        self._connection.execute(
            """
            INSERT INTO task_events(
                event_id, task_id, sequence, event_type, payload_json,
                session_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                sequence,
                event_type.value,
                _json_dump(dict(payload)),
                session_id,
                created_at,
            ),
        )
        record = TaskEventRecord(
            event_id=event_id,
            task_id=task_id,
            sequence=sequence,
            event_type=event_type,
            payload=MappingProxyType(dict(payload)),
            session_id=session_id,
            created_at=created_at,
        )
        if event_type in {
            EventType.GOAL_CHANGED,
            EventType.DECISION_MADE,
            EventType.DECISION_REVOKED,
            EventType.PHASE_COMPLETED,
        }:
            task = self.get_task(task_id)
            if task is not None:
                self._sync_search_index(task)
        return record

    def list_events(self, task_id: str) -> tuple[TaskEventRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def create_checkpoint(self, checkpoint: CheckpointRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, task_id, session_id, goal, constraints_json,
                current_phase, completed_json, current_state_json,
                decisions_json, rejected_alternatives_json, known_issues_json,
                artifacts_json, next_actions_json, content_checksum, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.task_id,
                checkpoint.session_id,
                checkpoint.goal,
                _json_dump(checkpoint.constraints),
                checkpoint.current_phase,
                _json_dump(checkpoint.completed),
                _json_dump(checkpoint.current_state),
                _json_dump(checkpoint.decisions),
                _json_dump(checkpoint.rejected_alternatives),
                _json_dump(checkpoint.known_issues),
                _json_dump(checkpoint.artifacts),
                _json_dump(checkpoint.next_actions),
                checkpoint.content_checksum,
                checkpoint.created_at,
            ),
        )
        task = self.get_task(checkpoint.task_id)
        if task is not None:
            self._sync_search_index(task)

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        return self._checkpoint_from_row(row) if row is not None else None

    def get_latest_checkpoint(self, task_id: str) -> CheckpointRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE task_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return self._checkpoint_from_row(row) if row is not None else None

    def create_segment(self, segment: ContextSegmentRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO context_segments(
                context_segment_id, session_id, task_id, parent_segment_id,
                checkpoint_id, start_message_index, end_message_index,
                start_time, end_time, handoff_reason, handoff_policy_snapshot,
                archived_context_reference
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment.context_segment_id,
                segment.session_id,
                segment.task_id,
                segment.parent_segment_id,
                segment.checkpoint_id,
                segment.start_message_index,
                segment.end_message_index,
                segment.start_time,
                segment.end_time,
                segment.handoff_reason,
                segment.handoff_policy_snapshot,
                segment.archived_context_reference,
            ),
        )

    def get_segment(self, segment_id: str) -> ContextSegmentRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM context_segments WHERE context_segment_id = ?",
                (segment_id,),
            ).fetchone()
        return self._segment_from_row(row) if row is not None else None

    def get_latest_segment(self, task_id: str) -> ContextSegmentRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM context_segments WHERE task_id = ?
                ORDER BY start_time DESC, rowid DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return self._segment_from_row(row) if row is not None else None

    def close_segment(
        self,
        segment_id: str,
        *,
        end_time: str,
        handoff_reason: str,
        end_message_index: int | None = None,
    ) -> ContextSegmentRecord:
        cursor = self._connection.execute(
            """
            UPDATE context_segments
            SET end_time = ?, handoff_reason = ?, end_message_index = ?
            WHERE context_segment_id = ? AND end_time IS NULL
            """,
            (end_time, handoff_reason, end_message_index, segment_id),
        )
        if cursor.rowcount != 1:
            raise ConcurrentUpdateError(
                f"Context Segment {segment_id!r} is already closed or changed."
            )
        segment = self.get_segment(segment_id)
        if segment is None:
            raise KeyError(f"Unknown context segment: {segment_id}")
        return segment

    def create_session_state(self, state: SessionContextState) -> None:
        self._connection.execute(
            """
            INSERT INTO session_context_state(
                session_id, active_task_id, active_context_segment_id,
                handoff_pending, pending_checkpoint_id, last_handoff_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.session_id,
                state.active_task_id,
                state.active_context_segment_id,
                int(state.handoff_pending),
                state.pending_checkpoint_id,
                state.last_handoff_at,
                state.version,
            ),
        )

    def get_session_state(self, session_id: str) -> SessionContextState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM session_context_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._session_state_from_row(row) if row is not None else None

    def update_session_state(
        self,
        session_id: str,
        *,
        expected_version: int,
        active_task_id: str | None,
        active_context_segment_id: str | None,
        handoff_pending: bool,
        pending_checkpoint_id: str | None,
        last_handoff_at: str | None,
    ) -> SessionContextState:
        cursor = self._connection.execute(
            """
            UPDATE session_context_state SET
                active_task_id = ?, active_context_segment_id = ?,
                handoff_pending = ?, pending_checkpoint_id = ?,
                last_handoff_at = ?, version = version + 1
            WHERE session_id = ? AND version = ?
            """,
            (
                active_task_id,
                active_context_segment_id,
                int(handoff_pending),
                pending_checkpoint_id,
                last_handoff_at,
                session_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentUpdateError(
                f"Session {session_id!r} version changed from {expected_version}."
            )
        state = self.get_session_state(session_id)
        if state is None:  # pragma: no cover - guarded by successful update
            raise RuntimeError("Updated session state disappeared.")
        return state

    def search_tasks(
        self,
        query: str,
        *,
        statuses: Sequence[TaskStatus],
        limit: int,
    ) -> tuple[TaskSearchResult, ...]:
        normalized = " ".join(query.casefold().split())
        if not normalized:
            return tuple(
                TaskSearchResult(task=task, score=0.0)
                for task in self.list_tasks(statuses, limit)
            )
        if self.search_backend == "fts5_trigram":
            matches = self._search_fts(normalized, statuses, limit)
            if matches:
                return matches
        return self._search_fallback(normalized, statuses, limit)

    def _search_fts(
        self,
        query: str,
        statuses: Sequence[TaskStatus],
        limit: int,
    ) -> tuple[TaskSearchResult, ...]:
        terms = [term for term in re.findall(r"[\w.-]+", query) if term]
        expression = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
        )
        if not expression:
            return ()
        placeholders = ",".join("?" for _ in statuses)
        status_clause = f"AND t.status IN ({placeholders})" if statuses else ""
        params: list[object] = [expression]
        params.extend(status.value for status in statuses)
        params.append(limit)
        try:
            with self._lock:
                rows = self._connection.execute(
                    f"""
                    SELECT t.*, bm25(task_search_fts) AS search_rank
                    FROM task_search_fts
                    JOIN tasks t ON t.task_id = task_search_fts.task_id
                    WHERE task_search_fts MATCH ? {status_clause}
                    ORDER BY search_rank ASC, t.updated_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        except sqlite3.OperationalError:
            return ()
        return tuple(
            TaskSearchResult(
                task=self._task_from_row(row),
                score=1.0 / (1.0 + abs(float(row["search_rank"]))),
            )
            for row in rows
        )

    def _search_fallback(
        self,
        query: str,
        statuses: Sequence[TaskStatus],
        limit: int,
    ) -> tuple[TaskSearchResult, ...]:
        terms = [term for term in re.findall(r"[\w.-]+", query) if len(term) >= 2]
        placeholders = ",".join("?" for _ in statuses)
        status_clause = f"WHERE t.status IN ({placeholders})" if statuses else ""
        params: list[object] = [status.value for status in statuses]
        params.append(max(limit * 10, 100))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT t.*, s.search_text
                FROM task_search_fallback s
                JOIN tasks t ON t.task_id = s.task_id
                {status_clause}
                ORDER BY t.updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        scored: list[TaskSearchResult] = []
        for row in rows:
            task = self._task_from_row(row)
            document = str(row["search_text"]).casefold()
            term_hits = sum(1 for term in terms if term in document)
            if query in document:
                term_hits += max(2, len(terms))
            if term_hits:
                scored.append(TaskSearchResult(task=task, score=float(term_hits)))
        scored.sort(
            key=lambda result: (result.score, result.task.updated_at), reverse=True
        )
        return tuple(scored[:limit])

    def _sync_search_index(self, task: TaskRecord) -> None:
        search_text = self._task_search_text(task)
        self._connection.execute(
            """
            INSERT INTO task_search_fallback(task_id, search_text) VALUES (?, ?)
            ON CONFLICT(task_id) DO UPDATE SET search_text = excluded.search_text
            """,
            (task.task_id, search_text),
        )
        if self.search_backend == "fts5_trigram":
            self._connection.execute(
                "DELETE FROM task_search_fts WHERE task_id = ?", (task.task_id,)
            )
            self._connection.execute(
                "INSERT INTO task_search_fts(task_id, search_text) VALUES (?, ?)",
                (task.task_id, search_text),
            )

    def _task_search_text(self, task: TaskRecord) -> str:
        values: list[str] = [
            task.title,
            task.goal,
            task.current_phase,
            *task.constraints,
            *task.completed,
            *task.in_progress,
            *task.known_issues,
            *task.next_actions,
            *task.decisions,
            *task.artifacts,
            *task.search_aliases,
            *task.tags,
        ]
        checkpoint = self.get_latest_checkpoint(task.task_id)
        if checkpoint is not None:
            values.extend(
                [
                    checkpoint.goal,
                    checkpoint.current_phase,
                    *checkpoint.constraints,
                    *checkpoint.completed,
                    *checkpoint.current_state,
                    *checkpoint.decisions,
                    *checkpoint.rejected_alternatives,
                    *checkpoint.known_issues,
                    *checkpoint.artifacts,
                    *checkpoint.next_actions,
                ]
            )
        rows = self._connection.execute(
            """
            SELECT payload_json FROM task_events
            WHERE task_id = ? AND event_type IN (?, ?, ?, ?)
            ORDER BY sequence DESC LIMIT 50
            """,
            (
                task.task_id,
                EventType.GOAL_CHANGED.value,
                EventType.DECISION_MADE.value,
                EventType.DECISION_REVOKED.value,
                EventType.PHASE_COMPLETED.value,
            ),
        ).fetchall()
        values.extend(str(row["payload_json"]) for row in rows)
        return "\n".join(value for value in values if value)

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=str(row["task_id"]),
            parent_task_id=row["parent_task_id"],
            title=str(row["title"]),
            created_session_id=str(row["created_session_id"]),
            last_session_id=str(row["last_session_id"]),
            goal=str(row["goal"]),
            constraints=_tuple_from_json(row["constraints_json"]),
            current_phase=str(row["current_phase"]),
            completed=_tuple_from_json(row["completed_json"]),
            in_progress=_tuple_from_json(row["in_progress_json"]),
            known_issues=_tuple_from_json(row["known_issues_json"]),
            next_actions=_tuple_from_json(row["next_actions_json"]),
            decisions=_tuple_from_json(row["decisions_json"]),
            artifacts=_tuple_from_json(row["artifacts_json"]),
            status=TaskStatus(row["status"]),
            search_aliases=_tuple_from_json(row["search_aliases_json"]),
            tags=_tuple_from_json(row["tags_json"]),
            paused_at=row["paused_at"],
            last_resumed_at=row["last_resumed_at"],
            resume_count=int(row["resume_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=int(row["version"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> TaskEventRecord:
        return TaskEventRecord(
            event_id=str(row["event_id"]),
            task_id=str(row["task_id"]),
            sequence=int(row["sequence"]),
            event_type=EventType(row["event_type"]),
            payload=MappingProxyType(json.loads(row["payload_json"])),
            session_id=str(row["session_id"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> CheckpointRecord:
        return CheckpointRecord(
            checkpoint_id=str(row["checkpoint_id"]),
            task_id=str(row["task_id"]),
            session_id=str(row["session_id"]),
            goal=str(row["goal"]),
            constraints=_tuple_from_json(row["constraints_json"]),
            current_phase=str(row["current_phase"]),
            completed=_tuple_from_json(row["completed_json"]),
            current_state=_tuple_from_json(row["current_state_json"]),
            decisions=_tuple_from_json(row["decisions_json"]),
            rejected_alternatives=_tuple_from_json(row["rejected_alternatives_json"]),
            known_issues=_tuple_from_json(row["known_issues_json"]),
            artifacts=_tuple_from_json(row["artifacts_json"]),
            next_actions=_tuple_from_json(row["next_actions_json"]),
            content_checksum=str(row["content_checksum"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _segment_from_row(row: sqlite3.Row) -> ContextSegmentRecord:
        return ContextSegmentRecord(
            context_segment_id=str(row["context_segment_id"]),
            session_id=str(row["session_id"]),
            task_id=str(row["task_id"]),
            parent_segment_id=row["parent_segment_id"],
            checkpoint_id=row["checkpoint_id"],
            start_message_index=int(row["start_message_index"]),
            end_message_index=row["end_message_index"],
            start_time=str(row["start_time"]),
            end_time=row["end_time"],
            handoff_reason=row["handoff_reason"],
            handoff_policy_snapshot=row["handoff_policy_snapshot"],
            archived_context_reference=row["archived_context_reference"],
        )

    @staticmethod
    def _session_state_from_row(row: sqlite3.Row) -> SessionContextState:
        return SessionContextState(
            session_id=str(row["session_id"]),
            active_task_id=row["active_task_id"],
            active_context_segment_id=row["active_context_segment_id"],
            handoff_pending=bool(row["handoff_pending"]),
            pending_checkpoint_id=row["pending_checkpoint_id"],
            last_handoff_at=row["last_handoff_at"],
            version=int(row["version"]),
        )
