"""Transactional task lifecycle, search, pause, and resume operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .store import ConcurrentUpdateError, TaskRepository
from .task_models import (
    ContextSegmentRecord,
    EventType,
    SessionContextState,
    TaskActivation,
    TaskEventRecord,
    TaskRecord,
    TaskSearchResult,
    TaskStatus,
)


class TaskServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def string_tuple(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise TaskServiceError(
            "invalid_state",
            f"{field} must be a list of non-empty strings.",
        )
    return tuple(item.strip() for item in value)


class TaskService:
    """Own task state transitions while Repository owns persistence details."""

    _STATE_FIELDS = frozenset(
        {
            "title",
            "goal",
            "constraints",
            "current_phase",
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
    _LIST_FIELDS = frozenset(
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

    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    def create_task(
        self,
        session_id: str,
        title: str,
        goal: str,
        state: Mapping[str, object] | None = None,
        parent_task_id: str | None = None,
        task_id: str | None = None,
    ) -> TaskActivation:
        self._require_text(session_id, "session_id")
        self._require_text(title, "title")
        self._require_text(goal, "goal")
        if task_id is not None:
            self._require_text(task_id, "task_id")
        if parent_task_id is not None:
            self._require_text(parent_task_id, "parent_task_id")
        normalized = self._normalize_state(state or {})
        now = utc_now()
        with self.repository.transaction():
            if parent_task_id and self.repository.get_task(parent_task_id) is None:
                raise TaskServiceError(
                    "parent_task_not_found",
                    f"Parent task {parent_task_id!r} does not exist.",
                )
            session_state = self.repository.get_session_state(session_id)
            if session_state and session_state.active_task_id:
                current = self._require_task(session_state.active_task_id)
                self._pause_for_switch(current, session_state, now)

            task = TaskRecord(
                task_id=task_id or new_id("task"),
                parent_task_id=parent_task_id,
                title=str(normalized.pop("title", title)).strip(),
                created_session_id=session_id,
                last_session_id=session_id,
                goal=str(normalized.pop("goal", goal)).strip(),
                constraints=normalized.pop("constraints", ()),
                current_phase=str(normalized.pop("current_phase", "")),
                completed=normalized.pop("completed", ()),
                in_progress=normalized.pop("in_progress", ()),
                known_issues=normalized.pop("known_issues", ()),
                next_actions=normalized.pop("next_actions", ()),
                decisions=normalized.pop("decisions", ()),
                artifacts=normalized.pop("artifacts", ()),
                status=TaskStatus.ACTIVE,
                search_aliases=normalized.pop("search_aliases", ()),
                tags=normalized.pop("tags", ()),
                paused_at=None,
                last_resumed_at=None,
                resume_count=0,
                created_at=now,
                updated_at=now,
                version=0,
            )
            self.repository.create_task(task)
            segment = self._new_segment(task.task_id, session_id, now)
            self.repository.create_segment(segment)
            state_result = self._set_session_pointer(
                session_id,
                session_state,
                task.task_id,
                segment.context_segment_id,
            )
            self._append_event(
                task.task_id,
                EventType.TASK_CREATED,
                {"goal": task.goal, "title": task.title},
                session_id,
                now,
            )
            if session_state and session_state.active_task_id:
                self._append_event(
                    task.task_id,
                    EventType.NEW_TASK_STARTED,
                    {"paused_task_id": session_state.active_task_id},
                    session_id,
                    now,
                )
        return TaskActivation(task, segment, state_result, None)

    def get_task(self, task_id: str) -> TaskRecord:
        return self._require_task(task_id)

    def get_active_task(self, session_id: str) -> TaskRecord | None:
        state = self.repository.get_session_state(session_id)
        if state is None or state.active_task_id is None:
            return None
        return self.repository.get_task(state.active_task_id)

    def update_task(
        self,
        task_id: str,
        session_id: str,
        *,
        expected_version: int,
        changes: Mapping[str, object],
    ) -> TaskRecord:
        normalized = self._normalize_state(changes)
        normalized["last_session_id"] = session_id
        task = self._require_task(task_id)
        payload: dict[str, Any] = {"changed_fields": sorted(normalized)}
        if "goal" in normalized and normalized["goal"] != task.goal:
            event_type = EventType.GOAL_CHANGED
            payload["old_goal"] = task.goal
            payload["new_goal"] = normalized["goal"]
        else:
            event_type = None
        try:
            with self.repository.transaction():
                updated = self.repository.update_task(
                    task_id,
                    expected_version=expected_version,
                    changes=normalized,
                    updated_at=utc_now(),
                )
                if event_type is not None:
                    self._append_event(
                        task_id,
                        event_type,
                        payload,
                        session_id,
                        updated.updated_at,
                    )
            return updated
        except ConcurrentUpdateError as exc:
            raise TaskServiceError("concurrent_update", str(exc)) from exc

    def pause_task(
        self,
        task_id: str,
        session_id: str,
        *,
        expected_version: int,
    ) -> TaskRecord:
        now = utc_now()
        try:
            with self.repository.transaction():
                task = self._require_task(task_id)
                if task.version != expected_version:
                    raise ConcurrentUpdateError("Task version changed before pause.")
                session_state = self.repository.get_session_state(session_id)
                if session_state is None or session_state.active_task_id != task_id:
                    raise TaskServiceError(
                        "task_not_active_in_session",
                        f"Task {task_id!r} is not active in session {session_id!r}.",
                    )
                paused = self._pause_for_switch(task, session_state, now)
                self.repository.update_session_state(
                    session_id,
                    expected_version=session_state.version,
                    active_task_id=None,
                    active_context_segment_id=None,
                    handoff_pending=False,
                    pending_checkpoint_id=None,
                    last_handoff_at=session_state.last_handoff_at,
                )
            return paused
        except ConcurrentUpdateError as exc:
            raise TaskServiceError("concurrent_update", str(exc)) from exc

    def resume_task(
        self,
        task_id: str,
        session_id: str,
        *,
        expected_version: int,
    ) -> TaskActivation:
        now = utc_now()
        try:
            with self.repository.transaction():
                target = self._require_task(task_id)
                if target.version != expected_version:
                    raise ConcurrentUpdateError("Task version changed before resume.")
                if target.status not in {TaskStatus.PAUSED, TaskStatus.BLOCKED}:
                    raise TaskServiceError(
                        "task_not_resumable",
                        f"Task {task_id!r} has status {target.status.value!r}.",
                    )
                checkpoint = self.repository.get_latest_checkpoint(task_id)
                if checkpoint is None:
                    raise TaskServiceError(
                        "checkpoint_required_before_resume",
                        "A valid checkpoint is required before task resume.",
                    )
                if not checkpoint.checksum_is_valid():
                    checkpoint_id = checkpoint.checkpoint_id
                    message = (
                        f"Checkpoint {checkpoint_id!r} failed checksum validation."
                    )
                    raise TaskServiceError(
                        "checkpoint_corrupt",
                        message,
                    )
                session_state = self.repository.get_session_state(session_id)
                if session_state and session_state.active_task_id:
                    current = self._require_task(session_state.active_task_id)
                    if current.task_id == task_id:
                        raise TaskServiceError(
                            "task_already_active",
                            f"Task {task_id!r} is already active in this session.",
                        )
                    self._pause_for_switch(current, session_state, now)

                resumed = self.repository.update_task(
                    task_id,
                    expected_version=expected_version,
                    changes={
                        "status": TaskStatus.ACTIVE,
                        "last_session_id": session_id,
                        "paused_at": None,
                        "last_resumed_at": now,
                        "resume_count": target.resume_count + 1,
                    },
                    updated_at=now,
                )
                parent = self.repository.get_latest_segment(task_id)
                segment = self._new_segment(
                    task_id,
                    session_id,
                    now,
                    parent_segment_id=(
                        parent.context_segment_id if parent is not None else None
                    ),
                    checkpoint_id=checkpoint.checkpoint_id,
                )
                self.repository.create_segment(segment)
                state_result = self._set_session_pointer(
                    session_id,
                    session_state,
                    task_id,
                    segment.context_segment_id,
                )
                self._append_event(
                    task_id,
                    EventType.TASK_RESUMED,
                    {
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "context_segment_id": segment.context_segment_id,
                    },
                    session_id,
                    now,
                )
            return TaskActivation(
                resumed,
                segment,
                state_result,
                checkpoint.checkpoint_id,
            )
        except ConcurrentUpdateError as exc:
            raise TaskServiceError("concurrent_update", str(exc)) from exc

    def transition_task(
        self,
        task_id: str,
        session_id: str,
        *,
        expected_version: int,
        status: TaskStatus,
    ) -> TaskRecord:
        if status not in {
            TaskStatus.BLOCKED,
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
        }:
            raise TaskServiceError("invalid_status_transition", str(status))
        event_types = {
            TaskStatus.COMPLETED: EventType.TASK_COMPLETED,
            TaskStatus.CANCELLED: EventType.TASK_CANCELLED,
            TaskStatus.BLOCKED: EventType.TASK_BLOCKED,
        }
        now = utc_now()
        try:
            with self.repository.transaction():
                task = self._require_task(task_id)
                if task.version != expected_version:
                    raise ConcurrentUpdateError("Task version changed.")
                if (
                    status is TaskStatus.BLOCKED
                    and self.repository.get_latest_checkpoint(task_id) is None
                ):
                    raise TaskServiceError(
                        "checkpoint_required_before_pause",
                        "A checkpoint is required before blocking a task.",
                    )
                updated = self.repository.update_task(
                    task_id,
                    expected_version=expected_version,
                    changes={
                        "status": status,
                        "last_session_id": session_id,
                        "paused_at": now if status is TaskStatus.BLOCKED else None,
                    },
                    updated_at=now,
                )
                state = self.repository.get_session_state(session_id)
                if state and state.active_task_id == task_id:
                    if state.active_context_segment_id:
                        self.repository.close_segment(
                            state.active_context_segment_id,
                            end_time=now,
                            handoff_reason=status.value,
                        )
                    self.repository.update_session_state(
                        session_id,
                        expected_version=state.version,
                        active_task_id=None,
                        active_context_segment_id=None,
                        handoff_pending=False,
                        pending_checkpoint_id=None,
                        last_handoff_at=state.last_handoff_at,
                    )
                self._append_event(
                    task_id,
                    event_types[status],
                    {"status": status.value},
                    session_id,
                    now,
                )
            return updated
        except ConcurrentUpdateError as exc:
            raise TaskServiceError("concurrent_update", str(exc)) from exc

    def search_tasks(
        self,
        *,
        query: str,
        statuses: Sequence[TaskStatus],
        limit: int,
    ) -> tuple[TaskSearchResult, ...]:
        if not 1 <= limit <= 100:
            raise TaskServiceError("invalid_limit", "limit must be between 1 and 100.")
        return self.repository.search_tasks(query, statuses=statuses, limit=limit)

    def list_tasks(
        self,
        *,
        statuses: Sequence[TaskStatus],
        limit: int,
    ) -> tuple[TaskRecord, ...]:
        if not 1 <= limit <= 100:
            raise TaskServiceError("invalid_limit", "limit must be between 1 and 100.")
        return self.repository.list_tasks(statuses, limit)

    def append_event(
        self,
        task_id: str,
        session_id: str,
        event_type: EventType,
        payload: Mapping[str, Any],
    ) -> TaskEventRecord:
        self._require_task(task_id)
        now = utc_now()
        with self.repository.transaction():
            return self._append_event(task_id, event_type, payload, session_id, now)

    def _pause_for_switch(
        self,
        task: TaskRecord,
        session_state: SessionContextState,
        now: str,
    ) -> TaskRecord:
        checkpoint = self.repository.get_latest_checkpoint(task.task_id)
        if checkpoint is None or not checkpoint.next_actions:
            raise TaskServiceError(
                "checkpoint_required_before_pause",
                "Current task needs a valid checkpoint with Next Actions before pause.",
            )
        if not checkpoint.checksum_is_valid():
            checkpoint_id = checkpoint.checkpoint_id
            message = f"Checkpoint {checkpoint_id!r} failed checksum validation."
            raise TaskServiceError(
                "checkpoint_corrupt",
                message,
            )
        paused = self.repository.update_task(
            task.task_id,
            expected_version=task.version,
            changes={
                "status": TaskStatus.PAUSED,
                "paused_at": now,
                "last_session_id": session_state.session_id,
            },
            updated_at=now,
        )
        if session_state.active_context_segment_id:
            self.repository.close_segment(
                session_state.active_context_segment_id,
                end_time=now,
                handoff_reason="task_paused",
            )
        self._append_event(
            task.task_id,
            EventType.TASK_PAUSED,
            {"checkpoint_id": checkpoint.checkpoint_id},
            session_state.session_id,
            now,
        )
        return paused

    def _set_session_pointer(
        self,
        session_id: str,
        existing: SessionContextState | None,
        task_id: str,
        segment_id: str,
    ) -> SessionContextState:
        if existing is None:
            state = SessionContextState(
                session_id=session_id,
                active_task_id=task_id,
                active_context_segment_id=segment_id,
                handoff_pending=False,
                pending_checkpoint_id=None,
                last_handoff_at=None,
                version=0,
            )
            self.repository.create_session_state(state)
            return state
        return self.repository.update_session_state(
            session_id,
            expected_version=existing.version,
            active_task_id=task_id,
            active_context_segment_id=segment_id,
            handoff_pending=False,
            pending_checkpoint_id=None,
            last_handoff_at=existing.last_handoff_at,
        )

    def _append_event(
        self,
        task_id: str,
        event_type: EventType,
        payload: Mapping[str, Any],
        session_id: str,
        created_at: str,
    ) -> TaskEventRecord:
        return self.repository.append_event(
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            session_id=session_id,
            created_at=created_at,
            event_id=new_id("event"),
        )

    @staticmethod
    def _new_segment(
        task_id: str,
        session_id: str,
        now: str,
        *,
        parent_segment_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> ContextSegmentRecord:
        return ContextSegmentRecord(
            context_segment_id=new_id("segment"),
            session_id=session_id,
            task_id=task_id,
            parent_segment_id=parent_segment_id,
            checkpoint_id=checkpoint_id,
            start_message_index=0,
            end_message_index=None,
            start_time=now,
            end_time=None,
            handoff_reason=None,
            handoff_policy_snapshot=None,
            archived_context_reference=None,
        )

    def _normalize_state(self, state: Mapping[str, object]) -> dict[str, Any]:
        unknown = set(state) - self._STATE_FIELDS
        if unknown:
            raise TaskServiceError(
                "invalid_state",
                f"Unsupported state fields: {sorted(unknown)}.",
            )
        normalized: dict[str, Any] = {}
        for field, value in state.items():
            if field in self._LIST_FIELDS:
                normalized[field] = string_tuple(value, field)
            elif not isinstance(value, str):
                raise TaskServiceError("invalid_state", f"{field} must be a string.")
            else:
                stripped = value.strip()
                if field in {"title", "goal"} and not stripped:
                    raise TaskServiceError(
                        "invalid_state", f"{field} must be non-empty."
                    )
                normalized[field] = stripped
        return normalized

    def _require_task(self, task_id: str) -> TaskRecord:
        task = self.repository.get_task(task_id)
        if task is None:
            raise TaskServiceError(
                "task_not_found", f"Task {task_id!r} does not exist."
            )
        return task

    @staticmethod
    def _require_text(value: str, field: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise TaskServiceError(
                "invalid_argument", f"{field} must be a non-empty string."
            )
