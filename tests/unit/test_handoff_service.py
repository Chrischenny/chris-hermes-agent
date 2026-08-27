from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from chris_hermes_agent.checkpoint_service import CheckpointService
from chris_hermes_agent.handoff_service import HandoffService, HandoffServiceError
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_models import EventType
from chris_hermes_agent.task_service import TaskService


def _repository(path: Path) -> TaskRepository:
    connection = sqlite3.connect(path, check_same_thread=False)
    initialize_database(connection)
    return TaskRepository(connection)


def _checkpoint_payload(goal: str) -> dict[str, object]:
    return {
        "goal": goal,
        "constraints": ["preserve history"],
        "current_phase": "P4",
        "completed": ["P3"],
        "current_state": ["ready to rotate"],
        "decisions": ["keep the tool pair"],
        "rejected_alternatives": ["restart the session"],
        "known_issues": [],
        "artifacts": ["context_engine.py"],
        "next_actions": ["continue in the new segment"],
    }


def _active_with_checkpoint(repository: TaskRepository):
    task = TaskService(repository).create_task(
        "session-1",
        "P4 Rotation",
        "Rotate without ending the turn",
        task_id="task-1",
    )
    checkpoint = CheckpointService(repository).create_checkpoint(
        task.task.task_id,
        "session-1",
        _checkpoint_payload(task.task.goal),
    )
    return task, checkpoint


def test_rotation_atomically_closes_segment_moves_pointer_and_emits_event(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "data.db")
    active, checkpoint = _active_with_checkpoint(repository)

    result = HandoffService(repository).rotate(
        session_id="session-1",
        checkpoint_reference=checkpoint.checkpoint_id,
        handoff_reason="stable P4 boundary",
        target_task_id="task-1",
        expected_active_task_id="task-1",
        expected_active_segment_id=active.segment.context_segment_id,
        triggering_message_index=7,
        policy_snapshot='{"match_source":"exact:model:model-a"}',
    )

    previous = repository.get_segment(active.segment.context_segment_id)
    state = repository.get_session_state("session-1")
    events = repository.list_events("task-1")

    assert previous is not None
    assert previous.end_time is not None
    assert previous.end_message_index == 7
    assert previous.handoff_reason == "stable P4 boundary"
    assert result.segment.parent_segment_id == active.segment.context_segment_id
    assert result.segment.checkpoint_id == checkpoint.checkpoint_id
    assert result.segment.start_message_index == 7
    assert result.segment.handoff_policy_snapshot == (
        '{"match_source":"exact:model:model-a"}'
    )
    assert state is not None
    assert state.active_task_id == "task-1"
    assert state.active_context_segment_id == result.segment.context_segment_id
    assert state.last_handoff_at is not None
    assert state.version == active.session_state.version + 1
    assert events[-1].event_type is EventType.HANDOFF_COMPLETED
    assert events[-1].payload["new_segment_id"] == result.segment.context_segment_id
    assert result.next_actions == ("continue in the new segment",)


@pytest.mark.parametrize(
    ("changed_field", "expected_code"),
    [
        ("checkpoint_reference", "checkpoint_not_found"),
        ("target_task_id", "checkpoint_task_mismatch"),
        ("expected_active_task_id", "active_pointer_changed"),
        ("expected_active_segment_id", "active_pointer_changed"),
    ],
)
def test_rotation_rejects_invalid_checkpoint_or_stale_pointer_without_mutation(
    tmp_path: Path,
    changed_field: str,
    expected_code: str,
) -> None:
    repository = _repository(tmp_path / f"{changed_field}.db")
    active, checkpoint = _active_with_checkpoint(repository)
    arguments = {
        "session_id": "session-1",
        "checkpoint_reference": checkpoint.checkpoint_id,
        "handoff_reason": "boundary",
        "target_task_id": "task-1",
        "expected_active_task_id": "task-1",
        "expected_active_segment_id": active.segment.context_segment_id,
        "triggering_message_index": 2,
        "policy_snapshot": "{}",
    }
    arguments[changed_field] = "wrong"

    with pytest.raises(HandoffServiceError) as exc_info:
        HandoffService(repository).rotate(**arguments)  # type: ignore[arg-type]

    state = repository.get_session_state("session-1")
    previous = repository.get_segment(active.segment.context_segment_id)
    assert exc_info.value.code == expected_code
    assert state is not None
    assert state.active_context_segment_id == active.segment.context_segment_id
    assert previous is not None
    assert previous.end_time is None
    assert all(
        event.event_type is not EventType.HANDOFF_COMPLETED
        for event in repository.list_events("task-1")
    )


def test_rotation_rejects_corrupt_checkpoint(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupt.db"
    repository = _repository(database_path)
    active, checkpoint = _active_with_checkpoint(repository)
    external = sqlite3.connect(database_path)
    external.execute(
        "UPDATE checkpoints SET content_checksum = 'corrupt' WHERE checkpoint_id = ?",
        (checkpoint.checkpoint_id,),
    )
    external.commit()
    external.close()

    with pytest.raises(HandoffServiceError) as exc_info:
        HandoffService(repository).rotate(
            session_id="session-1",
            checkpoint_reference=checkpoint.checkpoint_id,
            handoff_reason="boundary",
            target_task_id="task-1",
            expected_active_task_id="task-1",
            expected_active_segment_id=active.segment.context_segment_id,
            triggering_message_index=2,
            policy_snapshot="{}",
        )

    assert exc_info.value.code == "checkpoint_corrupt"


def test_rotation_rolls_back_if_new_segment_cannot_be_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "rollback.db")
    active, checkpoint = _active_with_checkpoint(repository)

    def fail_create(_segment: object) -> None:
        raise sqlite3.IntegrityError("injected segment failure")

    monkeypatch.setattr(repository, "create_segment", fail_create)
    with pytest.raises(sqlite3.IntegrityError):
        HandoffService(repository).rotate(
            session_id="session-1",
            checkpoint_reference=checkpoint.checkpoint_id,
            handoff_reason="boundary",
            target_task_id="task-1",
            expected_active_task_id="task-1",
            expected_active_segment_id=active.segment.context_segment_id,
            triggering_message_index=2,
            policy_snapshot="{}",
        )

    state = repository.get_session_state("session-1")
    previous = repository.get_segment(active.segment.context_segment_id)
    assert state is not None
    assert state.active_context_segment_id == active.segment.context_segment_id
    assert previous is not None
    assert previous.end_time is None


def test_repeated_rotation_with_old_expected_pointer_is_rejected(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repeat.db")
    active, checkpoint = _active_with_checkpoint(repository)
    service = HandoffService(repository)
    arguments = {
        "session_id": "session-1",
        "checkpoint_reference": checkpoint.checkpoint_id,
        "handoff_reason": "boundary",
        "target_task_id": "task-1",
        "expected_active_task_id": "task-1",
        "expected_active_segment_id": active.segment.context_segment_id,
        "triggering_message_index": 2,
        "policy_snapshot": "{}",
    }

    service.rotate(**arguments)  # type: ignore[arg-type]
    with pytest.raises(HandoffServiceError) as exc_info:
        service.rotate(**arguments)  # type: ignore[arg-type]

    assert exc_info.value.code == "active_pointer_changed"
    assert (
        sum(
            event.event_type is EventType.HANDOFF_COMPLETED
            for event in repository.list_events("task-1")
        )
        == 1
    )


def test_concurrent_rotations_allow_exactly_one_pointer_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "concurrent.db"
    setup = _repository(database_path)
    active, checkpoint = _active_with_checkpoint(setup)
    expected_segment_id = active.segment.context_segment_id
    setup.close()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def rotate(reason: str) -> None:
        repository = _repository(database_path)
        barrier.wait()
        try:
            HandoffService(repository).rotate(
                session_id="session-1",
                checkpoint_reference=checkpoint.checkpoint_id,
                handoff_reason=reason,
                target_task_id="task-1",
                expected_active_task_id="task-1",
                expected_active_segment_id=expected_segment_id,
                triggering_message_index=2,
                policy_snapshot="{}",
            )
        except HandoffServiceError as exc:
            outcomes.append(exc.code)
        else:
            outcomes.append("rotated")
        finally:
            repository.close()

    threads = [
        threading.Thread(target=rotate, args=("first",)),
        threading.Thread(target=rotate, args=("second",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    final = _repository(database_path)
    assert sorted(outcomes) == ["active_pointer_changed", "rotated"]
    assert (
        sum(
            event.event_type is EventType.HANDOFF_COMPLETED
            for event in final.list_events("task-1")
        )
        == 1
    )
