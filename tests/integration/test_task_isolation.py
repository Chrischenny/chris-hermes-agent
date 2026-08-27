from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chris_hermes_agent.checkpoint_service import CheckpointService
from chris_hermes_agent.context_engine import ContextHandoffEngine
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_models import EventType, TaskStatus
from chris_hermes_agent.task_service import TaskService


def _services(
    path: Path,
) -> tuple[TaskRepository, TaskService, CheckpointService]:
    connection = sqlite3.connect(path, check_same_thread=False)
    initialize_database(connection)
    repository = TaskRepository(connection)
    return repository, TaskService(repository), CheckpointService(repository)


def _checkpoint(goal: str, next_action: str) -> dict[str, object]:
    return {
        "goal": goal,
        "constraints": ["preserve stable prefix"],
        "current_phase": "P5",
        "completed": ["classified task intent"],
        "current_state": ["target task is active"],
        "decisions": ["isolate task execution trace"],
        "rejected_alternatives": ["copy the previous task trace"],
        "known_issues": [],
        "artifacts": ["HANDOFF.md"],
        "next_actions": [next_action],
    }


def _handoff_message(
    *, task_id: str, segment_id: str, checkpoint_id: str
) -> tuple[dict[str, str], dict[str, object]]:
    arguments = {
        "checkpoint_reference": checkpoint_id,
        "handoff_reason": "task boundary is durable",
        "target_task_id": task_id,
        "expected_active_task_id": task_id,
        "expected_active_segment_id": segment_id,
    }
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "p5-handoff",
                "type": "function",
                "function": {
                    "name": "handoff_context",
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }
    return arguments, message


def test_current_task_continuation_updates_without_new_task_or_rotation(
    tmp_path: Path,
) -> None:
    repository, tasks, _ = _services(tmp_path / "continuation.db")
    active = tasks.create_task(
        "session-1", "P5", "Implement task workflow", task_id="task-current"
    )

    updated = tasks.update_task(
        active.task.task_id,
        "session-1",
        expected_version=active.task.version,
        changes={"current_phase": "write Skill", "next_actions": ["write refs"]},
    )
    state = repository.get_session_state("session-1")

    assert updated.task_id == active.task.task_id
    assert state is not None
    assert state.active_context_segment_id == active.segment.context_segment_id
    assert len(repository.list_tasks((), 10)) == 1
    assert all(
        event.event_type is not EventType.HANDOFF_COMPLETED
        for event in repository.list_events(active.task.task_id)
    )


@pytest.mark.parametrize(
    ("relationship", "parent_task_id"),
    (("subtask", "task-parent"), ("new_task", None)),
)
def test_target_task_rotation_excludes_previous_task_trace(
    tmp_path: Path,
    relationship: str,
    parent_task_id: str | None,
) -> None:
    repository, tasks, checkpoints = _services(tmp_path / f"{relationship}.db")
    parent = tasks.create_task(
        "session-1",
        "Parent",
        "Finish parent goal",
        state={
            "constraints": ["shared constraint", "parent-only constraint"],
            "decisions": ["shared decision", "parent-only decision"],
            "artifacts": ["shared.md", "parent-debug.log"],
        },
        task_id="task-parent",
    )
    checkpoints.create_checkpoint(
        parent.task.task_id,
        "session-1",
        _checkpoint(parent.task.goal, "resume parent later"),
    )

    selected_state = (
        {
            "constraints": ["shared constraint"],
            "decisions": ["shared decision"],
            "artifacts": ["shared.md"],
        }
        if relationship == "subtask"
        else None
    )
    target = tasks.create_task(
        "session-1",
        "Target",
        "Finish isolated target goal",
        state=selected_state,
        parent_task_id=parent_task_id,
        task_id="task-target",
    )
    target_checkpoint = checkpoints.create_checkpoint(
        target.task.task_id,
        "session-1",
        _checkpoint(target.task.goal, "continue isolated target"),
    )

    arguments, handoff_message = _handoff_message(
        task_id=target.task.task_id,
        segment_id=target.segment.context_segment_id,
        checkpoint_id=target_checkpoint.checkpoint_id,
    )
    conversation = [
        {"role": "user", "content": "PARENT USER HISTORY"},
        {"role": "tool", "tool_call_id": "parent-tool", "content": "PARENT TRACE"},
        handoff_message,
    ]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")
    handoff = json.loads(
        engine.handle_tool_call("handoff_context", arguments, messages=conversation)
    )
    conversation.append(
        {
            "role": "tool",
            "tool_call_id": "p5-handoff",
            "content": json.dumps(handoff),
        }
    )
    selected = engine.select_context(
        [{"role": "system", "content": "SYSTEM + SOUL + SKILLS"}, *conversation],
        conversation_messages=conversation,
    )

    paused_parent = repository.get_task(parent.task.task_id)
    persisted_target = repository.get_task(target.task.task_id)
    assert paused_parent is not None
    assert paused_parent.status is TaskStatus.PAUSED
    assert persisted_target is not None
    assert persisted_target.parent_task_id == parent_task_id
    assert handoff["data"]["handoff_applied"] is True
    assert "PARENT TRACE" not in str(selected)
    assert "PARENT USER HISTORY" not in str(selected)
    assert "continue isolated target" in str(selected)
    if relationship == "subtask":
        assert persisted_target.constraints == ("shared constraint",)
        assert persisted_target.decisions == ("shared decision",)
        assert persisted_target.artifacts == ("shared.md",)
    else:
        assert persisted_target.constraints == ()
        assert persisted_target.decisions == ()
        assert persisted_target.artifacts == ()


def test_resumed_task_requires_explicit_rotation_after_pointer_update(
    tmp_path: Path,
) -> None:
    repository, tasks, checkpoints = _services(tmp_path / "resume.db")
    paused_target = tasks.create_task(
        "session-old", "Paused", "Resume this work", task_id="task-paused"
    )
    checkpoint = checkpoints.create_checkpoint(
        paused_target.task.task_id,
        "session-old",
        _checkpoint(paused_target.task.goal, "resume from checkpoint"),
    )
    paused = tasks.pause_task(
        paused_target.task.task_id,
        "session-old",
        expected_version=paused_target.task.version,
    )

    resumed = tasks.resume_task(
        paused.task_id,
        "session-new",
        expected_version=paused.version,
    )

    assert resumed.checkpoint_id == checkpoint.checkpoint_id
    assert resumed.session_state.active_task_id == paused.task_id
    assert resumed.segment.checkpoint_id == checkpoint.checkpoint_id
    assert not any(
        event.event_type is EventType.HANDOFF_COMPLETED
        for event in repository.list_events(paused.task_id)
    )

    arguments, handoff_message = _handoff_message(
        task_id=resumed.task.task_id,
        segment_id=resumed.segment.context_segment_id,
        checkpoint_id=checkpoint.checkpoint_id,
    )
    conversation = [
        {"role": "tool", "tool_call_id": "old", "content": "CURRENT TASK TRACE"},
        handoff_message,
    ]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-new")
    handoff = json.loads(
        engine.handle_tool_call("handoff_context", arguments, messages=conversation)
    )

    assert handoff["data"]["handoff_applied"] is True
    assert any(
        event.event_type is EventType.HANDOFF_COMPLETED
        for event in repository.list_events(paused.task_id)
    )
