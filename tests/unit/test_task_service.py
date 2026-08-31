from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chris_hermes_agent.checkpoint_service import CheckpointService
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_models import EventType, TaskStatus
from chris_hermes_agent.task_service import TaskService, TaskServiceError


def _services(path: Path) -> tuple[TaskRepository, TaskService, CheckpointService]:
    connection = sqlite3.connect(path, check_same_thread=False)
    initialize_database(connection)
    repository = TaskRepository(connection)
    return repository, TaskService(repository), CheckpointService(repository)


def _checkpoint_payload(goal: str) -> dict[str, object]:
    return {
        "goal": goal,
        "constraints": ["不修改 chris-avatar"],
        "current_phase": "P2",
        "completed": ["P1"],
        "current_state": ["持久化实现中"],
        "decisions": ["新任务默认暂存"],
        "rejected_alternatives": ["直接结束旧任务"],
        "known_issues": [],
        "artifacts": ["HANDOFF.md"],
        "next_actions": ["继续 P2"],
    }


def test_create_task_activates_task_segment_and_event(tmp_path: Path) -> None:
    repository, service, _ = _services(tmp_path / "data.db")

    result = service.create_task(
        session_id="session-1",
        title="Hermes Context Handoff",
        goal="完成 P2 SQLite",
        state={"tags": ["Hermes"], "current_phase": "P2"},
    )

    assert result.task.status is TaskStatus.ACTIVE
    assert result.task.created_session_id == "session-1"
    assert result.segment.task_id == result.task.task_id
    assert result.session_state.active_task_id == result.task.task_id
    assert (
        repository.list_events(result.task.task_id)[0].event_type
        is EventType.TASK_CREATED
    )


def test_starting_new_task_requires_checkpoint_then_pauses_old_task(
    tmp_path: Path,
) -> None:
    repository, service, checkpoints = _services(tmp_path / "data.db")
    old = service.create_task(
        session_id="session-1",
        title="Old task",
        goal="Old unfinished goal",
    )

    with pytest.raises(TaskServiceError) as exc_info:
        service.create_task(
            session_id="session-1",
            title="New task",
            goal="New independent goal",
        )
    assert exc_info.value.code == "checkpoint_required_before_pause"
    assert repository.get_task(old.task.task_id).status is TaskStatus.ACTIVE

    checkpoints.create_checkpoint(
        task_id=old.task.task_id,
        session_id="session-1",
        payload=_checkpoint_payload(old.task.goal),
    )
    new = service.create_task(
        session_id="session-1",
        title="New task",
        goal="New independent goal",
    )

    paused = repository.get_task(old.task.task_id)
    assert paused.status is TaskStatus.PAUSED
    assert paused.paused_at is not None
    assert new.session_state.active_task_id == new.task.task_id
    assert EventType.TASK_PAUSED in {
        event.event_type for event in repository.list_events(old.task.task_id)
    }


def test_independent_task_cannot_claim_another_task_artifact_namespace(
    tmp_path: Path,
) -> None:
    repository, service, _ = _services(tmp_path / "data.db")
    owner = service.create_task(
        "session-owner",
        "Owner",
        "Produce durable output",
        task_id="task-owner",
    )
    foreign_artifact = "/profile/task-artifacts/task-owner/m5-batch5g-scope.md"

    with pytest.raises(TaskServiceError) as rejected:
        service.create_task(
            "session-new",
            "Independent",
            "Produce independent output",
            state={"artifacts": [foreign_artifact]},
            task_id="task-independent",
        )

    assert rejected.value.code == "foreign_task_artifact_namespace"
    assert repository.get_task("task-independent") is None
    assert repository.get_task(owner.task.task_id) is not None


def test_subtask_may_inherit_parent_artifact_as_read_only_input(
    tmp_path: Path,
) -> None:
    _, service, _ = _services(tmp_path / "data.db")
    service.create_task(
        "session-parent",
        "Parent",
        "Produce parent input",
        task_id="task-parent",
    )
    parent_artifact = "/profile/task-artifacts/task-parent/input.md"

    child = service.create_task(
        "session-child",
        "Child",
        "Consume selected parent input",
        state={"artifacts": [parent_artifact]},
        parent_task_id="task-parent",
        task_id="task-child",
    )

    assert child.task.artifacts == (parent_artifact,)


def test_search_and_cross_session_resume_uses_latest_checkpoint(tmp_path: Path) -> None:
    repository, service, checkpoints = _services(tmp_path / "data.db")
    hermes = service.create_task(
        session_id="session-1",
        title="Hermes Policy 开发",
        goal="实现 Context Handoff Policy 和 SQLite",
        state={
            "search_aliases": ["上下文交接"],
            "tags": ["Hermes", "Policy"],
            "next_actions": ["实现 Repository"],
        },
    )
    checkpoint = checkpoints.create_checkpoint(
        task_id=hermes.task.task_id,
        session_id="session-1",
        payload=_checkpoint_payload(hermes.task.goal),
    )
    service.pause_task(
        task_id=hermes.task.task_id,
        session_id="session-1",
        expected_version=hermes.task.version,
    )

    candidates = service.search_tasks(
        query="继续之前那个 Hermes Policy 任务",
        statuses=(TaskStatus.PAUSED, TaskStatus.BLOCKED),
        limit=5,
    )
    resumed = service.resume_task(
        task_id=candidates[0].task.task_id,
        session_id="session-2",
        expected_version=candidates[0].task.version,
    )

    assert candidates[0].task.task_id == hermes.task.task_id
    assert resumed.task.status is TaskStatus.ACTIVE
    assert resumed.task.last_session_id == "session-2"
    assert resumed.task.resume_count == 1
    assert resumed.segment.checkpoint_id == checkpoint.checkpoint_id
    assert resumed.segment.parent_segment_id == hermes.segment.context_segment_id
    assert resumed.session_state.active_task_id == hermes.task.task_id
    assert EventType.TASK_RESUMED in {
        event.event_type for event in repository.list_events(hermes.task.task_id)
    }


def test_resume_in_busy_session_pauses_current_task_atomically(tmp_path: Path) -> None:
    repository, service, checkpoints = _services(tmp_path / "data.db")
    first = service.create_task("session-1", "First", "First goal")
    checkpoints.create_checkpoint(
        first.task.task_id,
        "session-1",
        _checkpoint_payload(first.task.goal),
    )
    second = service.create_task("session-1", "Second", "Second goal")
    checkpoints.create_checkpoint(
        second.task.task_id,
        "session-1",
        _checkpoint_payload(second.task.goal),
    )
    paused_first = repository.get_task(first.task.task_id)

    resumed = service.resume_task(
        paused_first.task_id,
        "session-1",
        expected_version=paused_first.version,
    )

    assert repository.get_task(second.task.task_id).status is TaskStatus.PAUSED
    assert resumed.task.status is TaskStatus.ACTIVE
    assert resumed.session_state.active_task_id == first.task.task_id


def test_checkpoint_validation_requires_complete_fields_and_next_actions(
    tmp_path: Path,
) -> None:
    _, service, checkpoints = _services(tmp_path / "data.db")
    task = service.create_task("session-1", "Task", "Goal")
    payload = _checkpoint_payload(task.task.goal)
    payload["next_actions"] = []

    with pytest.raises(TaskServiceError) as exc_info:
        checkpoints.create_checkpoint(task.task.task_id, "session-1", payload)

    assert exc_info.value.code == "invalid_checkpoint"

    valid = checkpoints.create_checkpoint(
        task.task.task_id,
        "session-1",
        _checkpoint_payload(task.task.goal),
    )
    assert valid.checksum_is_valid() is True


def test_update_requires_expected_version_and_valid_state_fields(
    tmp_path: Path,
) -> None:
    repository, service, _ = _services(tmp_path / "data.db")
    task = service.create_task("session-1", "Task", "Goal").task

    updated = service.update_task(
        task.task_id,
        "session-1",
        expected_version=task.version,
        changes={"current_phase": "P2 Store", "next_actions": ["write tests"]},
    )

    assert updated.current_phase == "P2 Store"
    assert updated.version == 1
    with pytest.raises(TaskServiceError) as stale:
        service.update_task(
            task.task_id,
            "session-1",
            expected_version=task.version,
            changes={"current_phase": "stale"},
        )
    assert stale.value.code == "concurrent_update"
    assert repository.get_task(task.task_id).current_phase == "P2 Store"


def test_block_requires_checkpoint_and_emits_distinct_event(tmp_path: Path) -> None:
    repository, service, checkpoints = _services(tmp_path / "data.db")
    task = service.create_task("session-1", "Task", "Goal").task

    with pytest.raises(TaskServiceError) as missing:
        service.transition_task(
            task.task_id,
            "session-1",
            expected_version=task.version,
            status=TaskStatus.BLOCKED,
        )
    assert missing.value.code == "checkpoint_required_before_pause"

    checkpoints.create_checkpoint(
        task.task_id,
        "session-1",
        _checkpoint_payload(task.goal),
    )
    blocked = service.transition_task(
        task.task_id,
        "session-1",
        expected_version=task.version,
        status=TaskStatus.BLOCKED,
    )

    assert blocked.status is TaskStatus.BLOCKED
    assert EventType.TASK_BLOCKED in {
        event.event_type for event in repository.list_events(task.task_id)
    }


def test_non_resumable_task_and_invalid_search_limit_fail_closed(
    tmp_path: Path,
) -> None:
    _, service, _ = _services(tmp_path / "data.db")
    task = service.create_task("session-1", "Task", "Goal").task

    with pytest.raises(TaskServiceError) as resume_error:
        service.resume_task(
            task.task_id,
            "session-1",
            expected_version=task.version,
        )
    with pytest.raises(TaskServiceError) as limit_error:
        service.search_tasks(query="task", statuses=(), limit=0)

    assert resume_error.value.code == "task_not_resumable"
    assert limit_error.value.code == "invalid_limit"


def test_corrupt_checkpoint_blocks_pause_and_resume(tmp_path: Path) -> None:
    database_path = tmp_path / "data.db"
    _, service, checkpoints = _services(database_path)
    task = service.create_task("session-1", "Task", "Goal").task
    checkpoint = checkpoints.create_checkpoint(
        task.task_id,
        "session-1",
        _checkpoint_payload(task.goal),
    )
    external = sqlite3.connect(database_path)
    external.execute(
        "UPDATE checkpoints SET content_checksum = 'corrupt' WHERE checkpoint_id = ?",
        (checkpoint.checkpoint_id,),
    )
    external.commit()
    external.close()

    with pytest.raises(TaskServiceError) as corrupt:
        service.pause_task(
            task.task_id,
            "session-1",
            expected_version=task.version,
        )

    assert corrupt.value.code == "checkpoint_corrupt"


def test_search_document_includes_checkpoint_and_decision_event(tmp_path: Path) -> None:
    _, service, checkpoints = _services(tmp_path / "data.db")
    task = service.create_task("session-1", "Generic", "Generic goal").task
    payload = _checkpoint_payload(task.goal)
    payload["artifacts"] = ["凤凰恢复标记"]
    checkpoints.create_checkpoint(task.task_id, "session-1", payload)
    service.append_event(
        task.task_id,
        "session-1",
        EventType.DECISION_MADE,
        {"decision": "使用星河索引"},
    )
    service.pause_task(
        task.task_id,
        "session-1",
        expected_version=task.version,
    )

    checkpoint_match = service.search_tasks(
        query="凤凰恢复",
        statuses=(TaskStatus.PAUSED,),
        limit=5,
    )
    event_match = service.search_tasks(
        query="星河索引",
        statuses=(TaskStatus.PAUSED,),
        limit=5,
    )

    assert checkpoint_match[0].task.task_id == task.task_id
    assert event_match[0].task.task_id == task.task_id
