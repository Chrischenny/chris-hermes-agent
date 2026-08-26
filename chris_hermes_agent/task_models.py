"""Immutable persistence models for task lifecycle and context segments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    TASK_CREATED = "TASK_CREATED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_BLOCKED = "TASK_BLOCKED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_CANCELLED = "TASK_CANCELLED"
    GOAL_CHANGED = "GOAL_CHANGED"
    CONSTRAINT_ADDED = "CONSTRAINT_ADDED"
    DECISION_MADE = "DECISION_MADE"
    DECISION_REVOKED = "DECISION_REVOKED"
    PHASE_COMPLETED = "PHASE_COMPLETED"
    FILE_CHANGED = "FILE_CHANGED"
    TEST_FAILED = "TEST_FAILED"
    TEST_PASSED = "TEST_PASSED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    HANDOFF_COMPLETED = "HANDOFF_COMPLETED"
    NEW_TASK_STARTED = "NEW_TASK_STARTED"
    EMERGENCY_COMPRESSION_TRIGGERED = "EMERGENCY_COMPRESSION_TRIGGERED"
    EMERGENCY_COMPRESSION_COMPLETED = "EMERGENCY_COMPRESSION_COMPLETED"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    parent_task_id: str | None
    title: str
    created_session_id: str
    last_session_id: str
    goal: str
    constraints: tuple[str, ...]
    current_phase: str
    completed: tuple[str, ...]
    in_progress: tuple[str, ...]
    known_issues: tuple[str, ...]
    next_actions: tuple[str, ...]
    decisions: tuple[str, ...]
    artifacts: tuple[str, ...]
    status: TaskStatus
    search_aliases: tuple[str, ...]
    tags: tuple[str, ...]
    paused_at: str | None
    last_resumed_at: str | None
    resume_count: int
    created_at: str
    updated_at: str
    version: int

    def as_dict(self) -> dict[str, Any]:
        """Return constructor-compatible values for immutable copying."""
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(frozen=True, slots=True)
class TaskEventRecord:
    event_id: str
    task_id: str
    sequence: int
    event_type: EventType
    payload: Mapping[str, Any]
    session_id: str
    created_at: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "payload": dict(self.payload),
            "session_id": self.session_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    checkpoint_id: str
    task_id: str
    session_id: str
    goal: str
    constraints: tuple[str, ...]
    current_phase: str
    completed: tuple[str, ...]
    current_state: tuple[str, ...]
    decisions: tuple[str, ...]
    rejected_alternatives: tuple[str, ...]
    known_issues: tuple[str, ...]
    artifacts: tuple[str, ...]
    next_actions: tuple[str, ...]
    content_checksum: str
    created_at: str

    def calculated_checksum(self) -> str:
        canonical = {
            "goal": self.goal,
            "current_phase": self.current_phase,
            "constraints": self.constraints,
            "completed": self.completed,
            "current_state": self.current_state,
            "decisions": self.decisions,
            "rejected_alternatives": self.rejected_alternatives,
            "known_issues": self.known_issues,
            "artifacts": self.artifacts,
            "next_actions": self.next_actions,
        }
        return hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def checksum_is_valid(self) -> bool:
        return self.content_checksum == self.calculated_checksum()

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContextSegmentRecord:
    context_segment_id: str
    session_id: str
    task_id: str
    parent_segment_id: str | None
    checkpoint_id: str | None
    start_message_index: int
    end_message_index: int | None
    start_time: str
    end_time: str | None
    handoff_reason: str | None
    handoff_policy_snapshot: str | None
    archived_context_reference: str | None

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SessionContextState:
    session_id: str
    active_task_id: str | None
    active_context_segment_id: str | None
    handoff_pending: bool
    pending_checkpoint_id: str | None
    last_handoff_at: str | None
    version: int

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TaskSearchResult:
    task: TaskRecord
    score: float

    def to_json_dict(self) -> dict[str, Any]:
        return {"task": self.task.to_json_dict(), "score": self.score}


@dataclass(frozen=True, slots=True)
class TaskActivation:
    task: TaskRecord
    segment: ContextSegmentRecord
    session_state: SessionContextState
    checkpoint_id: str | None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_json_dict(),
            "segment": self.segment.to_json_dict(),
            "session_state": self.session_state.to_json_dict(),
            "checkpoint_id": self.checkpoint_id,
        }
