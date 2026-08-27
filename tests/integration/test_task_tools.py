from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_tools import TaskToolHandlers, _default_repository


def _handlers(path: Path) -> TaskToolHandlers:
    connection = sqlite3.connect(path, check_same_thread=False)
    initialize_database(connection)
    return TaskToolHandlers(TaskRepository(connection))


def _checkpoint(goal: str) -> dict[str, object]:
    return {
        "goal": goal,
        "constraints": [],
        "current_phase": "P2",
        "completed": ["P1"],
        "current_state": ["P2 in progress"],
        "decisions": ["pause by default"],
        "rejected_alternatives": [],
        "known_issues": [],
        "artifacts": ["HANDOFF.md"],
        "next_actions": ["continue P2"],
    }


def test_tool_flow_creates_checkpoints_searches_and_resumes_tasks(
    tmp_path: Path,
) -> None:
    handlers = _handlers(tmp_path / "data.db")
    created = json.loads(
        handlers.task_state_manage(
            {
                "action": "create",
                "state": {
                    "title": "Hermes Policy",
                    "goal": "实现 Hermes Context Handoff",
                    "search_aliases": ["上下文交接"],
                },
            },
            session_id="session-1",
        )
    )
    task_id = created["data"]["task"]["task_id"]

    checkpoint = json.loads(
        handlers.checkpoint_create(
            {
                "task_id": task_id,
                "checkpoint": _checkpoint(created["data"]["task"]["goal"]),
            },
            session_id="session-1",
        )
    )
    paused = json.loads(
        handlers.task_state_manage(
            {
                "action": "pause",
                "task_id": task_id,
                "expected_version": created["data"]["task"]["version"],
            },
            session_id="session-1",
        )
    )
    search = json.loads(
        handlers.task_state_manage(
            {"action": "search", "query": "之前 Hermes 上下文任务"},
            session_id="session-2",
        )
    )
    resumed = json.loads(
        handlers.task_state_manage(
            {
                "action": "resume",
                "task_id": task_id,
                "expected_version": paused["data"]["task"]["version"],
            },
            session_id="session-2",
        )
    )

    assert created["ok"] is True
    assert checkpoint["ok"] is True
    assert search["data"]["candidates"][0]["task"]["task_id"] == task_id
    assert resumed["ok"] is True
    assert (
        resumed["data"]["checkpoint_id"]
        == checkpoint["data"]["checkpoint"]["checkpoint_id"]
    )
    assert resumed["data"]["context_rotation_applied"] is False
    assert resumed["data"]["context_rotation_required"] is True
    assert resumed["data"]["next_required_action"] == "call_handoff_context"


def test_event_tool_appends_supported_event_and_rejects_unknown_type(
    tmp_path: Path,
) -> None:
    handlers = _handlers(tmp_path / "data.db")
    created = json.loads(
        handlers.task_state_manage(
            {"action": "create", "state": {"title": "Task", "goal": "Goal"}},
            session_id="session-1",
        )
    )
    task_id = created["data"]["task"]["task_id"]

    appended = json.loads(
        handlers.task_event_append(
            {
                "task_id": task_id,
                "event_type": "DECISION_MADE",
                "payload": {"decision": "SQLite"},
            },
            session_id="session-1",
        )
    )
    rejected = json.loads(
        handlers.task_event_append(
            {"task_id": task_id, "event_type": "UNKNOWN", "payload": {}},
            session_id="session-1",
        )
    )

    assert appended["ok"] is True
    assert appended["data"]["event"]["sequence"] == 2
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "invalid_event_type"


def test_tools_fail_closed_for_missing_runtime_session_or_bad_arguments(
    tmp_path: Path,
) -> None:
    handlers = _handlers(tmp_path / "data.db")

    missing_session = json.loads(
        handlers.task_state_manage(
            {"action": "create", "state": {"title": "Task", "goal": "Goal"}}
        )
    )
    bad_action = json.loads(
        handlers.task_state_manage({"action": "unknown"}, session_id="session-1")
    )

    assert missing_session["error"]["code"] == "missing_session_id"
    assert bad_action["error"]["code"] == "invalid_action"


def test_task_state_error_recovers_from_checkpoint_field_confusion(
    tmp_path: Path,
) -> None:
    handlers = _handlers(tmp_path / "data.db")

    result = json.loads(
        handlers.task_state_manage(
            {
                "action": "create",
                "state": {
                    "title": "Task",
                    "goal": "Goal",
                    "current_state": ["ready"],
                    "rejected_alternatives": [],
                },
            },
            session_id="desktop-session",
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_state"
    message = result["error"]["message"]
    assert "Supported Task State fields" in message
    assert "in_progress" in message
    assert "current_state" in message
    assert "checkpoint_create" in message


def test_task_tool_supports_get_update_list_and_complete(tmp_path: Path) -> None:
    handlers = _handlers(tmp_path / "data.db")
    created = json.loads(
        handlers.task_state_manage(
            {"action": "create", "state": {"title": "Task", "goal": "Goal"}},
            session_id="session-1",
        )
    )
    task = created["data"]["task"]

    active = json.loads(
        handlers.task_state_manage({"action": "get"}, session_id="session-1")
    )
    updated = json.loads(
        handlers.task_state_manage(
            {
                "action": "update",
                "task_id": task["task_id"],
                "expected_version": task["version"],
                "state": {
                    "current_phase": "P2 Store",
                    "next_actions": ["verify"],
                },
            },
            session_id="session-1",
        )
    )
    listed = json.loads(
        handlers.task_state_manage(
            {"action": "list", "statuses": ["active"], "limit": 5},
            session_id="session-1",
        )
    )
    completed = json.loads(
        handlers.task_state_manage(
            {
                "action": "complete",
                "task_id": task["task_id"],
                "expected_version": updated["data"]["task"]["version"],
            },
            session_id="session-1",
        )
    )

    assert active["data"]["task"]["task_id"] == task["task_id"]
    assert updated["data"]["task"]["current_phase"] == "P2 Store"
    assert listed["data"]["tasks"][0]["task_id"] == task["task_id"]
    assert completed["data"]["task"]["status"] == "completed"


def test_default_runtime_uses_profile_scoped_hermes_plugin_database(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "profile"
    token = set_hermes_home_override(str(hermes_home))
    try:
        handlers = TaskToolHandlers()
        created = json.loads(
            handlers.task_state_manage(
                {
                    "action": "create",
                    "state": {"title": "Task", "goal": "Goal"},
                },
                session_id="session-profile",
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert created["ok"] is True
    data_directory = hermes_home / "plugin-data" / "chris-hermes-agent"
    database_path = data_directory / "data.db"
    assert database_path.is_file()
    assert stat.S_IMODE(data_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600


def test_default_repository_rejects_symlinked_plugin_state_directory(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "profile"
    plugin_data = hermes_home / "plugin-data"
    plugin_data.mkdir(parents=True)
    target = tmp_path / "unexpected-target"
    target.mkdir()
    (plugin_data / "chris-hermes-agent").symlink_to(
        target,
        target_is_directory=True,
    )
    token = set_hermes_home_override(str(hermes_home))
    try:
        with pytest.raises(RuntimeError, match="must not be a symbolic link"):
            _default_repository()
    finally:
        reset_hermes_home_override(token)
