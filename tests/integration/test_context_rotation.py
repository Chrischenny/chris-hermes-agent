from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from chris_hermes_agent.checkpoint_service import CheckpointService
from chris_hermes_agent.context_engine import ContextHandoffEngine
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_models import TaskStatus
from chris_hermes_agent.task_service import TaskService


def _repository(path: Path) -> TaskRepository:
    connection = sqlite3.connect(path, check_same_thread=False)
    initialize_database(connection)
    return TaskRepository(connection)


def _checkpoint_payload(goal: str) -> dict[str, object]:
    return {
        "goal": goal,
        "constraints": ["keep system"],
        "current_phase": "P4",
        "completed": ["P3"],
        "current_state": ["rotation ready"],
        "decisions": ["preserve handoff tool pair"],
        "rejected_alternatives": [],
        "known_issues": [],
        "artifacts": ["HANDOFF.md"],
        "next_actions": ["continue P4"],
    }


def _handoff_call(segment_id: str, checkpoint_id: str) -> tuple[dict, dict]:
    args = {
        "checkpoint_reference": checkpoint_id,
        "handoff_reason": "stable boundary",
        "target_task_id": "task-1",
        "expected_active_task_id": "task-1",
        "expected_active_segment_id": segment_id,
    }
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "handoff-call",
                "type": "function",
                "function": {
                    "name": "handoff_context",
                    "arguments": json.dumps(args),
                },
            }
        ],
    }
    return args, assistant


def _task_action_call(
    call_id: str,
    action: str,
    *,
    task_id: str | None = None,
) -> dict:
    arguments = {"action": action}
    if task_id is not None:
        arguments["task_id"] = task_id
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "task_state_manage",
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


def test_handoff_rotates_next_provider_request_without_mutating_history(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "data.db")
    active = TaskService(repository).create_task(
        "session-1", "P4", "Rotate context", task_id="task-1"
    )
    checkpoint = CheckpointService(repository).create_checkpoint(
        "task-1", "session-1", _checkpoint_payload(active.task.goal)
    )
    args, assistant_call = _handoff_call(
        active.segment.context_segment_id,
        checkpoint.checkpoint_id,
    )
    conversation = [
        {"role": "user", "content": "old user goal"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "old-call",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "old-call", "content": "OLD TRACE"},
        assistant_call,
    ]
    engine = ContextHandoffEngine(repository=repository)
    engine.update_model("model-a", 100_000)
    engine.on_session_start("session-1")

    result = json.loads(
        engine.handle_tool_call("handoff_context", args, messages=conversation)
    )
    conversation.append(
        {
            "role": "tool",
            "name": "handoff_context",
            "tool_call_id": "handoff-call",
            "content": json.dumps(result),
        }
    )
    history_snapshot = copy.deepcopy(conversation)
    request = [{"role": "system", "content": "SYSTEM + SOUL + SKILLS"}, *conversation]

    selected = engine.select_context(request, conversation_messages=conversation)

    assert result["ok"] is True
    assert result["data"]["handoff_applied"] is True
    assert result["data"]["context_segment_id"] != active.segment.context_segment_id
    assert result["data"]["checkpoint_id"] == checkpoint.checkpoint_id
    assert result["data"]["task_id"] == "task-1"
    assert result["data"]["next_actions"] == ["continue P4"]
    persisted = repository.get_segment(result["data"]["context_segment_id"])
    assert persisted is not None
    assert persisted.start_message_checksum is not None
    assert conversation == history_snapshot
    assert selected[0] is request[0]
    assert "OLD TRACE" not in str(selected)
    assert "[Context Handoff Bootstrap]" in str(selected)
    assert assistant_call in selected
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "handoff-call"
        for message in selected
    )
    assert f"Context segment: {result['data']['context_segment_id']}" in str(
        selected[-1]["content"]
    )


def test_rotated_context_remains_selected_across_tool_loop_and_restart(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "restart.db")
    active = TaskService(repository).create_task(
        "session-1", "P4", "Rotate context", task_id="task-1"
    )
    checkpoint = CheckpointService(repository).create_checkpoint(
        "task-1", "session-1", _checkpoint_payload(active.task.goal)
    )
    args, assistant_call = _handoff_call(
        active.segment.context_segment_id,
        checkpoint.checkpoint_id,
    )
    old = {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"}
    conversation = [{"role": "user", "content": "old"}, old, assistant_call]
    first_engine = ContextHandoffEngine(repository=repository)
    first_engine.on_session_start("session-1")
    result = json.loads(
        first_engine.handle_tool_call("handoff_context", args, messages=conversation)
    )
    conversation.append(
        {
            "role": "tool",
            "tool_call_id": "handoff-call",
            "content": json.dumps(result),
        }
    )
    conversation.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "new-call",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "new-call", "content": "new result"},
        ]
    )
    request = [{"role": "system", "content": "stable"}, *conversation]

    continued = first_engine.select_context(request, conversation_messages=conversation)
    repository.close()
    reopened = _repository(tmp_path / "restart.db")
    restarted_engine = ContextHandoffEngine(repository=reopened)
    restarted_engine.on_session_start("session-1")
    restarted = restarted_engine.select_context(
        request, conversation_messages=conversation
    )

    assert "OLD TRACE" not in str(continued)
    assert "OLD TRACE" not in str(restarted)
    assert "new result" in str(continued)
    assert "new result" in str(restarted)
    assert "[Context Handoff Bootstrap]" in str(continued)
    assert "[Context Handoff Bootstrap]" in str(restarted)


@pytest.mark.parametrize(
    "inactive_status",
    (
        TaskStatus.PAUSED,
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    ),
)
def test_inactive_task_keeps_latest_checkpoint_segment_as_recovery_context(
    tmp_path: Path,
    inactive_status: TaskStatus,
) -> None:
    repository = _repository(tmp_path / "blocked-recovery.db")
    tasks = TaskService(repository)
    active = tasks.create_task("session-1", "P4", "Rotate context", task_id="task-1")
    checkpoint = CheckpointService(repository).create_checkpoint(
        "task-1", "session-1", _checkpoint_payload(active.task.goal)
    )
    args, assistant_call = _handoff_call(
        active.segment.context_segment_id,
        checkpoint.checkpoint_id,
    )
    old_trace = {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"}
    conversation = [{"role": "user", "content": "old"}, old_trace, assistant_call]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")
    result = json.loads(
        engine.handle_tool_call("handoff_context", args, messages=conversation)
    )
    conversation.append(
        {
            "role": "tool",
            "tool_call_id": "handoff-call",
            "content": json.dumps(result),
        }
    )
    conversation.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "block-call",
                        "type": "function",
                        "function": {
                            "name": "task_state_manage",
                            "arguments": '{"action":"block"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "block-call",
                "content": "TASK BLOCKED — WAIT FOR USER",
            },
        ]
    )
    if inactive_status is TaskStatus.PAUSED:
        inactive = tasks.pause_task(
            "task-1",
            "session-1",
            expected_version=active.task.version,
        )
    else:
        inactive = tasks.transition_task(
            "task-1",
            "session-1",
            expected_version=active.task.version,
            status=inactive_status,
        )
    state = repository.get_session_state("session-1")
    request = [{"role": "system", "content": "stable"}, *conversation]

    selected = engine.select_context(request, conversation_messages=conversation)

    assert inactive.status is inactive_status
    assert state is not None
    assert state.active_task_id is None
    assert state.active_context_segment_id is None
    assert "OLD TRACE" not in str(selected)
    assert "[Context Handoff Bootstrap]" in str(selected)
    assert f"Task status: {inactive_status.value}" in str(selected)
    assert "TASK BLOCKED — WAIT FOR USER" in str(selected)
    assert "Active task: none" in str(selected[-1]["content"])
    assert "Context segment: none" in str(selected[-1]["content"])


@pytest.mark.parametrize("deferred", (False, True))
def test_same_session_resume_starts_at_current_turn_instead_of_full_history(
    tmp_path: Path,
    deferred: bool,
) -> None:
    repository = _repository(tmp_path / "same-session-resume.db")
    tasks = TaskService(repository)
    active = tasks.create_task("session-1", "P4", "Resume safely", task_id="task-1")
    CheckpointService(repository).create_checkpoint(
        "task-1", "session-1", _checkpoint_payload(active.task.goal)
    )
    blocked = tasks.transition_task(
        "task-1",
        "session-1",
        expected_version=active.task.version,
        status=TaskStatus.BLOCKED,
    )
    resume_arguments = {
        "action": "resume",
        "task_id": "task-1",
        "expected_version": blocked.version,
    }
    resume_function = (
        {
            "name": "tool_call",
            "arguments": json.dumps(
                {
                    "name": "task_state_manage",
                    "arguments": resume_arguments,
                }
            ),
        }
        if deferred
        else {
            "name": "task_state_manage",
            "arguments": json.dumps(resume_arguments),
        }
    )
    conversation = [
        {"role": "user", "content": "OLD USER HISTORY"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"},
        {"role": "user", "content": "CURRENT RESUME REQUEST"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "resume-call",
                    "type": "function",
                    "function": resume_function,
                }
            ],
        },
    ]
    resumed = tasks.resume_task(
        "task-1",
        "session-1",
        expected_version=blocked.version,
    )
    conversation.append(
        {
            "role": "tool",
            "tool_call_id": "resume-call",
            "content": json.dumps(resumed.to_json_dict()),
        }
    )
    request = [{"role": "system", "content": "stable"}, *conversation]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")

    selected = engine.select_context(request, conversation_messages=conversation)

    assert resumed.segment.start_message_index == 0
    assert "OLD USER HISTORY" not in str(selected)
    assert "OLD TRACE" not in str(selected)
    assert "CURRENT RESUME REQUEST" in str(selected)
    assert "resume-call" in str(selected)
    assert "[Context Handoff Bootstrap]" in str(selected)


@pytest.mark.parametrize("checkpoint_created", (False, True))
def test_new_task_creation_isolates_prior_task_before_first_handoff(
    tmp_path: Path,
    checkpoint_created: bool,
) -> None:
    repository = _repository(tmp_path / "create-boundary.db")
    tasks = TaskService(repository)
    checkpoints = CheckpointService(repository)
    old = tasks.create_task("session-1", "Old", "Old goal", task_id="task-old")
    checkpoints.create_checkpoint(
        old.task.task_id,
        "session-1",
        _checkpoint_payload(old.task.goal),
    )
    conversation = [
        {"role": "user", "content": "OLD TASK USER"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TASK TRACE"},
        {"role": "user", "content": "CREATE CURRENT TASK"},
        _task_action_call("create-call", "create", task_id="task-new"),
    ]
    created = tasks.create_task("session-1", "New", "New goal", task_id="task-new")
    conversation.append(
        {
            "role": "tool",
            "tool_call_id": "create-call",
            "content": json.dumps(created.to_json_dict()),
        }
    )
    if checkpoint_created:
        checkpoints.create_checkpoint(
            created.task.task_id,
            "session-1",
            _checkpoint_payload(created.task.goal),
        )
    request = [{"role": "system", "content": "stable"}, *conversation]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")

    selected = engine.select_context(request, conversation_messages=conversation)

    assert created.segment.start_message_index == 0
    assert created.segment.checkpoint_id is None
    assert "OLD TASK USER" not in str(selected)
    assert "OLD TASK TRACE" not in str(selected)
    assert "CREATE CURRENT TASK" in str(selected)
    assert "create-call" in str(selected)


@pytest.mark.parametrize(
    "inactive_status",
    (
        TaskStatus.PAUSED,
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    ),
)
def test_inactive_task_before_first_handoff_keeps_creation_boundary(
    tmp_path: Path,
    inactive_status: TaskStatus,
) -> None:
    repository = _repository(tmp_path / "pre-handoff-inactive.db")
    tasks = TaskService(repository)
    checkpoints = CheckpointService(repository)
    old = tasks.create_task("session-1", "Old", "Old goal", task_id="task-old")
    checkpoints.create_checkpoint(
        old.task.task_id,
        "session-1",
        _checkpoint_payload(old.task.goal),
    )
    created = tasks.create_task("session-1", "New", "New goal", task_id="task-new")
    checkpoints.create_checkpoint(
        created.task.task_id,
        "session-1",
        _checkpoint_payload(created.task.goal),
    )
    conversation = [
        {"role": "user", "content": "OLD TASK USER"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TASK TRACE"},
        {"role": "user", "content": "CREATE CURRENT TASK"},
        _task_action_call("create-call", "create", task_id="task-new"),
        {
            "role": "tool",
            "tool_call_id": "create-call",
            "content": json.dumps(created.to_json_dict()),
        },
        {"role": "user", "content": "STOP CURRENT TASK"},
        _task_action_call("transition-call", inactive_status.value, task_id="task-new"),
    ]
    current = repository.get_task(created.task.task_id)
    assert current is not None
    if inactive_status is TaskStatus.PAUSED:
        tasks.pause_task(
            current.task_id,
            "session-1",
            expected_version=current.version,
        )
    else:
        tasks.transition_task(
            current.task_id,
            "session-1",
            expected_version=current.version,
            status=inactive_status,
        )
    conversation.append(
        {
            "role": "tool",
            "tool_call_id": "transition-call",
            "content": "CURRENT TASK STOPPED",
        }
    )
    request = [{"role": "system", "content": "stable"}, *conversation]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")

    selected = engine.select_context(request, conversation_messages=conversation)

    assert "OLD TASK USER" not in str(selected)
    assert "OLD TASK TRACE" not in str(selected)
    assert "CREATE CURRENT TASK" in str(selected)
    assert "CURRENT TASK STOPPED" in str(selected)


def test_partial_context_pointer_fails_closed_to_latest_turn(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "partial-pointer.db")
    activation = TaskService(repository).create_task(
        "session-1", "Task", "Goal", task_id="task-1"
    )
    repository._connection.execute(
        "UPDATE session_context_state SET active_context_segment_id = NULL "
        "WHERE session_id = ?",
        ("session-1",),
    )
    repository._connection.commit()
    conversation = [
        {"role": "user", "content": "OLD USER"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"},
        {"role": "user", "content": "CURRENT USER"},
    ]
    request = [{"role": "system", "content": "stable"}, *conversation]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")

    selected = engine.select_context(request, conversation_messages=conversation)

    assert activation.task.task_id == "task-1"
    assert "OLD TRACE" not in str(selected)
    assert "CURRENT USER" in str(selected)
    assert "context_pointer_partial" in str(selected)


def test_mismatched_active_segment_fails_closed_to_latest_turn(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "mismatched-pointer.db")
    tasks = TaskService(repository)
    tasks.create_task("session-1", "One", "One", task_id="task-1")
    other = tasks.create_task("session-2", "Two", "Two", task_id="task-2")
    repository._connection.execute(
        "UPDATE session_context_state SET active_context_segment_id = ? "
        "WHERE session_id = ?",
        (other.segment.context_segment_id, "session-1"),
    )
    repository._connection.commit()
    conversation = [
        {"role": "user", "content": "OLD USER"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"},
        {"role": "user", "content": "CURRENT USER"},
    ]
    request = [{"role": "system", "content": "stable"}, *conversation]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")

    selected = engine.select_context(request, conversation_messages=conversation)

    assert "OLD TRACE" not in str(selected)
    assert "CURRENT USER" in str(selected)
    assert "segment_task_mismatch" in str(selected)


def test_storage_read_error_fails_closed_to_latest_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "storage-error.db")
    TaskService(repository).create_task("session-1", "Task", "Goal", task_id="task-1")

    def fail_read(_: str) -> None:
        raise sqlite3.OperationalError("simulated read failure")

    monkeypatch.setattr(repository, "get_session_state", fail_read)
    conversation = [
        {"role": "user", "content": "OLD USER"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"},
        {"role": "user", "content": "CURRENT USER"},
    ]
    request = [{"role": "system", "content": "stable"}, *conversation]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")

    selected = engine.select_context(request, conversation_messages=conversation)

    assert "OLD TRACE" not in str(selected)
    assert "CURRENT USER" in str(selected)
    assert "context_state_unavailable" in str(selected)


def test_unexpected_selection_error_fails_closed_before_host_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "unexpected-error.db")
    TaskService(repository).create_task("session-1", "Task", "Goal", task_id="task-1")

    def fail_segment(_: str) -> None:
        raise RuntimeError("simulated plugin defect")

    monkeypatch.setattr(repository, "get_segment", fail_segment)
    conversation = [
        {"role": "user", "content": "OLD USER"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"},
        {"role": "user", "content": "CURRENT USER"},
    ]
    request = [{"role": "system", "content": "stable"}, *conversation]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")

    selected = engine.select_context(request, conversation_messages=conversation)

    assert "OLD TRACE" not in str(selected)
    assert "CURRENT USER" in str(selected)
    assert "context_selection_failed" in str(selected)


def test_zero_cursor_resume_without_canonical_call_fails_closed(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "missing-resume-call.db")
    tasks = TaskService(repository)
    active = tasks.create_task("session-1", "Task", "Goal", task_id="task-1")
    CheckpointService(repository).create_checkpoint(
        active.task.task_id,
        "session-1",
        _checkpoint_payload(active.task.goal),
    )
    blocked = tasks.transition_task(
        active.task.task_id,
        "session-1",
        expected_version=active.task.version,
        status=TaskStatus.BLOCKED,
    )
    tasks.resume_task(
        blocked.task_id,
        "session-1",
        expected_version=blocked.version,
    )
    conversation = [
        {"role": "user", "content": "OLD USER"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"},
        {"role": "user", "content": "CURRENT USER AFTER REWRITE"},
    ]
    request = [{"role": "system", "content": "stable"}, *conversation]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")

    selected = engine.select_context(request, conversation_messages=conversation)

    assert "OLD TRACE" not in str(selected)
    assert "CURRENT USER AFTER REWRITE" in str(selected)
    assert "activation_boundary_missing" in str(selected)


def test_handoff_tool_fails_closed_without_session_messages_or_valid_trigger(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "fail.db")
    active = TaskService(repository).create_task(
        "session-1", "P4", "Rotate context", task_id="task-1"
    )
    checkpoint = CheckpointService(repository).create_checkpoint(
        "task-1", "session-1", _checkpoint_payload(active.task.goal)
    )
    args, _ = _handoff_call(active.segment.context_segment_id, checkpoint.checkpoint_id)
    engine = ContextHandoffEngine(repository=repository)

    no_session = json.loads(
        engine.handle_tool_call("handoff_context", args, messages=[])
    )
    engine.on_session_start("session-1")
    no_trigger = json.loads(
        engine.handle_tool_call(
            "handoff_context",
            args,
            messages=[{"role": "user", "content": "not a tool call"}],
        )
    )
    stale_trigger = json.loads(
        engine.handle_tool_call(
            "handoff_context",
            args,
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "old-handoff",
                            "type": "function",
                            "function": {
                                "name": "handoff_context",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {"role": "user", "content": "newer message"},
            ],
        )
    )

    assert no_session["error"]["code"] == "missing_session_id"
    assert no_trigger["error"]["code"] == "handoff_trigger_not_found"
    assert stale_trigger["error"]["code"] == "handoff_trigger_not_found"


def test_invalid_persisted_segment_cursor_fails_closed_without_old_trace(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "invalid-cursor.db"
    repository = _repository(database_path)
    active = TaskService(repository).create_task(
        "session-1", "P4", "Rotate context", task_id="task-1"
    )
    checkpoint = CheckpointService(repository).create_checkpoint(
        "task-1", "session-1", _checkpoint_payload(active.task.goal)
    )
    args, assistant_call = _handoff_call(
        active.segment.context_segment_id,
        checkpoint.checkpoint_id,
    )
    conversation = [
        {"role": "user", "content": "OLD TRACE"},
        assistant_call,
    ]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")
    result = json.loads(
        engine.handle_tool_call("handoff_context", args, messages=conversation)
    )
    conversation.append(
        {
            "role": "tool",
            "tool_call_id": "handoff-call",
            "content": json.dumps(result),
        }
    )
    repository._connection.execute(
        "UPDATE context_segments SET start_message_index = 999 "
        "WHERE context_segment_id = ?",
        (result["data"]["context_segment_id"],),
    )
    repository._connection.commit()
    request = [{"role": "system", "content": "stable"}, *conversation]

    selected = engine.select_context(request, conversation_messages=conversation)

    assert "OLD TRACE" not in str(selected)
    assert "[Context Handoff Bootstrap Error]" in str(selected)
    assert "segment_cursor_invalid" in str(selected)


def test_rewritten_handoff_anchor_fails_closed_to_latest_turn(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "rewritten-anchor.db")
    active = TaskService(repository).create_task(
        "session-1", "P4", "Rotate context", task_id="task-1"
    )
    checkpoint = CheckpointService(repository).create_checkpoint(
        "task-1", "session-1", _checkpoint_payload(active.task.goal)
    )
    args, assistant_call = _handoff_call(
        active.segment.context_segment_id,
        checkpoint.checkpoint_id,
    )
    conversation = [
        {"role": "user", "content": "OLD USER"},
        {"role": "tool", "tool_call_id": "old", "content": "OLD TRACE"},
        assistant_call,
    ]
    engine = ContextHandoffEngine(repository=repository)
    engine.on_session_start("session-1")
    result = json.loads(
        engine.handle_tool_call("handoff_context", args, messages=conversation)
    )
    conversation.append(
        {
            "role": "tool",
            "tool_call_id": "handoff-call",
            "content": json.dumps(result),
        }
    )
    conversation[2] = {"role": "assistant", "content": "REWRITTEN BRANCH"}
    conversation.extend(
        [
            {"role": "user", "content": "CURRENT USER"},
            {"role": "assistant", "content": "CURRENT RESPONSE"},
        ]
    )
    request = [{"role": "system", "content": "stable"}, *conversation]

    selected = engine.select_context(request, conversation_messages=conversation)

    assert "OLD TRACE" not in str(selected)
    assert "REWRITTEN BRANCH" not in str(selected)
    assert "CURRENT USER" in str(selected)
    assert "segment_anchor_mismatch" in str(selected)
