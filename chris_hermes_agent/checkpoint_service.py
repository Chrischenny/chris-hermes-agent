"""Validation and persistence for resumable task checkpoints."""

from __future__ import annotations

from collections.abc import Mapping

from .store import TaskRepository
from .task_models import CheckpointRecord, EventType
from .task_service import TaskServiceError, new_id, string_tuple, utc_now


class CheckpointService:
    REQUIRED_FIELDS = frozenset(
        {
            "goal",
            "constraints",
            "current_phase",
            "completed",
            "current_state",
            "decisions",
            "rejected_alternatives",
            "known_issues",
            "artifacts",
            "next_actions",
        }
    )
    LIST_FIELDS = REQUIRED_FIELDS - {"goal", "current_phase"}

    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    def create_checkpoint(
        self,
        task_id: str,
        session_id: str,
        payload: Mapping[str, object],
    ) -> CheckpointRecord:
        missing = self.REQUIRED_FIELDS - set(payload)
        unknown = set(payload) - self.REQUIRED_FIELDS
        if missing or unknown:
            field_summary = f"missing={sorted(missing)} unknown={sorted(unknown)}"
            raise TaskServiceError(
                "invalid_checkpoint",
                f"Checkpoint fields {field_summary}.",
            )
        goal = payload["goal"]
        current_phase = payload["current_phase"]
        if not isinstance(goal, str) or not goal.strip():
            raise TaskServiceError("invalid_checkpoint", "goal must be non-empty.")
        if not isinstance(current_phase, str):
            raise TaskServiceError(
                "invalid_checkpoint", "current_phase must be a string."
            )
        values: dict[str, tuple[str, ...]] = {}
        try:
            for field in self.LIST_FIELDS:
                values[field] = string_tuple(payload[field], field)
        except TaskServiceError as exc:
            raise TaskServiceError("invalid_checkpoint", exc.message) from exc
        if not values["next_actions"]:
            raise TaskServiceError(
                "invalid_checkpoint", "next_actions must contain at least one action."
            )
        task = self.repository.get_task(task_id)
        if task is None:
            raise TaskServiceError(
                "task_not_found", f"Task {task_id!r} does not exist."
            )
        now = utc_now()
        checkpoint = CheckpointRecord(
            checkpoint_id=new_id("checkpoint"),
            task_id=task_id,
            session_id=session_id,
            goal=goal.strip(),
            constraints=values["constraints"],
            current_phase=current_phase.strip(),
            completed=values["completed"],
            current_state=values["current_state"],
            decisions=values["decisions"],
            rejected_alternatives=values["rejected_alternatives"],
            known_issues=values["known_issues"],
            artifacts=values["artifacts"],
            next_actions=values["next_actions"],
            content_checksum="",
            created_at=now,
        )
        checkpoint = CheckpointRecord(
            **{
                **checkpoint.to_json_dict(),
                "content_checksum": checkpoint.calculated_checksum(),
            }
        )
        with self.repository.transaction():
            self.repository.create_checkpoint(checkpoint)
            self.repository.append_event(
                task_id=task_id,
                event_type=EventType.CHECKPOINT_CREATED,
                payload={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "content_checksum": checkpoint.content_checksum,
                },
                session_id=session_id,
                created_at=now,
                event_id=new_id("event"),
            )
        return checkpoint
