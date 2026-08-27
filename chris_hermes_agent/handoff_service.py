"""Atomic Context Segment rotation over durable task state."""

from __future__ import annotations

from dataclasses import dataclass

from .store import ConcurrentUpdateError, TaskRepository
from .task_models import (
    CheckpointRecord,
    ContextSegmentRecord,
    EventType,
    SessionContextState,
    TaskRecord,
    TaskStatus,
)
from .task_service import new_id, utc_now


class HandoffServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class HandoffResult:
    task: TaskRecord
    checkpoint: CheckpointRecord
    segment: ContextSegmentRecord
    session_state: SessionContextState

    @property
    def next_actions(self) -> tuple[str, ...]:
        return self.checkpoint.next_actions

    def to_json_dict(self) -> dict[str, object]:
        return {
            "handoff_applied": True,
            "context_segment_id": self.segment.context_segment_id,
            "checkpoint_id": self.checkpoint.checkpoint_id,
            "task_id": self.task.task_id,
            "next_actions": list(self.next_actions),
        }


class HandoffService:
    """Validate and commit one compare-and-swap Context rotation."""

    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    def rotate(
        self,
        *,
        session_id: str,
        checkpoint_reference: str,
        handoff_reason: str,
        target_task_id: str,
        expected_active_task_id: str,
        expected_active_segment_id: str,
        triggering_message_index: int,
        policy_snapshot: str,
    ) -> HandoffResult:
        for value, field in (
            (session_id, "session_id"),
            (checkpoint_reference, "checkpoint_reference"),
            (handoff_reason, "handoff_reason"),
            (target_task_id, "target_task_id"),
            (expected_active_task_id, "expected_active_task_id"),
            (expected_active_segment_id, "expected_active_segment_id"),
        ):
            self._require_text(value, field)
        if triggering_message_index < 0:
            raise HandoffServiceError(
                "invalid_message_index",
                "triggering_message_index must be non-negative.",
            )

        now = utc_now()
        try:
            with self.repository.transaction():
                checkpoint = self.repository.get_checkpoint(checkpoint_reference)
                if checkpoint is None:
                    raise HandoffServiceError(
                        "checkpoint_not_found",
                        f"Checkpoint {checkpoint_reference!r} does not exist.",
                    )
                if checkpoint.task_id != target_task_id:
                    raise HandoffServiceError(
                        "checkpoint_task_mismatch",
                        "Checkpoint does not belong to target_task_id.",
                    )
                if not checkpoint.next_actions:
                    raise HandoffServiceError(
                        "invalid_checkpoint",
                        "Checkpoint must contain at least one Next Action.",
                    )
                if not checkpoint.checksum_is_valid():
                    raise HandoffServiceError(
                        "checkpoint_corrupt",
                        f"Checkpoint {checkpoint_reference!r} failed "
                        "checksum validation.",
                    )

                task = self.repository.get_task(target_task_id)
                if task is None:
                    raise HandoffServiceError(
                        "task_not_found", f"Task {target_task_id!r} does not exist."
                    )
                if task.status is not TaskStatus.ACTIVE:
                    raise HandoffServiceError(
                        "target_task_not_active",
                        f"Task {target_task_id!r} is not active.",
                    )

                state = self.repository.get_session_state(session_id)
                if state is None:
                    raise HandoffServiceError(
                        "session_state_not_found",
                        f"Session {session_id!r} has no active Context state.",
                    )
                if (
                    state.active_task_id != expected_active_task_id
                    or state.active_context_segment_id != expected_active_segment_id
                ):
                    raise HandoffServiceError(
                        "active_pointer_changed",
                        "Active Task or Context Segment changed before handoff.",
                    )
                if state.active_task_id != target_task_id:
                    raise HandoffServiceError(
                        "target_task_not_active",
                        "P4 rotation requires target_task_id to be the active task.",
                    )

                previous = self.repository.get_segment(expected_active_segment_id)
                if previous is None:
                    raise HandoffServiceError(
                        "active_segment_not_found",
                        f"Segment {expected_active_segment_id!r} does not exist.",
                    )
                if (
                    previous.session_id != session_id
                    or previous.task_id != expected_active_task_id
                    or previous.end_time is not None
                ):
                    raise HandoffServiceError(
                        "active_pointer_changed",
                        "Expected Context Segment is not the open active segment.",
                    )

                self.repository.close_segment(
                    previous.context_segment_id,
                    end_time=now,
                    handoff_reason=handoff_reason.strip(),
                    end_message_index=triggering_message_index,
                )
                segment = ContextSegmentRecord(
                    context_segment_id=new_id("segment"),
                    session_id=session_id,
                    task_id=target_task_id,
                    parent_segment_id=previous.context_segment_id,
                    checkpoint_id=checkpoint.checkpoint_id,
                    start_message_index=triggering_message_index,
                    end_message_index=None,
                    start_time=now,
                    end_time=None,
                    handoff_reason=None,
                    handoff_policy_snapshot=policy_snapshot,
                    archived_context_reference=None,
                )
                self.repository.create_segment(segment)
                updated_state = self.repository.update_session_state(
                    session_id,
                    expected_version=state.version,
                    active_task_id=target_task_id,
                    active_context_segment_id=segment.context_segment_id,
                    handoff_pending=False,
                    pending_checkpoint_id=None,
                    last_handoff_at=now,
                )
                self.repository.append_event(
                    task_id=target_task_id,
                    event_type=EventType.HANDOFF_COMPLETED,
                    payload={
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "previous_segment_id": previous.context_segment_id,
                        "new_segment_id": segment.context_segment_id,
                        "handoff_reason": handoff_reason.strip(),
                    },
                    session_id=session_id,
                    created_at=now,
                    event_id=new_id("event"),
                )
        except ConcurrentUpdateError as exc:
            raise HandoffServiceError("active_pointer_changed", str(exc)) from exc

        return HandoffResult(task, checkpoint, segment, updated_state)

    @staticmethod
    def _require_text(value: object, field: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise HandoffServiceError(
                "invalid_argument", f"{field} must be a non-empty string."
            )
