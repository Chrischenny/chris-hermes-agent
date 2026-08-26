from __future__ import annotations

import sqlite3
from pathlib import Path

from chris_hermes_agent.context_engine import ContextHandoffEngine
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_service import TaskService


def _repository(path: Path) -> TaskRepository:
    connection = sqlite3.connect(path, check_same_thread=False)
    initialize_database(connection)
    return TaskRepository(connection)


def _policy() -> dict[str, object]:
    return {
        "model_policies": {
            "model-a": {
                "handoff_enabled": True,
                "sweet_zone": {"type": "ratio", "start": 0.5},
                "emergency": {
                    "enabled": True,
                    "type": "ratio",
                    "threshold": 0.9,
                },
            },
            "model-b": {
                "handoff_enabled": True,
                "sweet_zone": {
                    "type": "absolute_tokens",
                    "start": 30_000,
                },
            },
        }
    }


def _content(selected: list[dict[str, object]]) -> str:
    return str(selected[-1]["content"])


def test_tool_loop_gets_fresh_request_only_status_and_last_actual_usage(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "data.db")
    engine = ContextHandoffEngine(_policy(), repository=repository)
    engine.update_model("model-a", 100_000, provider="provider-a")
    engine.on_session_start("session-1")
    history: list[dict[str, object]] = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "run a tool"},
    ]
    history_snapshot = [dict(message) for message in history]

    first = engine.select_context(history, conversation_messages=history)
    engine.update_from_response(
        {
            "prompt_tokens": 1_200,
            "completion_tokens": 40,
            "total_tokens": 1_240,
        }
    )
    tool_loop_request = [
        *history,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    second = engine.select_context(
        tool_loop_request,
        conversation_messages=tool_loop_request,
    )

    assert first is not None
    assert second is not None
    assert history == history_snapshot
    assert (
        sum(
            str(message.get("content", "")).startswith("[Runtime Status]")
            for message in first
        )
        == 1
    )
    assert (
        sum(
            str(message.get("content", "")).startswith("[Runtime Status]")
            for message in second
        )
        == 1
    )
    assert "Prompt tokens, last actual: unavailable" in _content(first)
    assert "Prompt tokens, last actual: 1200" in _content(second)
    assert engine.should_compress(99_999) is False


def test_retry_recomputes_one_status_without_mutating_stable_prefix(
    tmp_path: Path,
) -> None:
    engine = ContextHandoffEngine(_policy(), repository=_repository(tmp_path / "db"))
    engine.update_model("model-a", 100_000)
    engine.on_session_start("session-retry")
    request: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "retry me"},
    ]

    first = engine.select_context(request)
    retry = engine.select_context(first)

    assert first is not None
    assert retry is not None
    assert first[:-1] == retry[:-1] == request
    assert all(first[index] is request[index] for index in range(len(request)))
    assert all(retry[index] is request[index] for index in range(len(request)))
    assert len(first) == len(request) + 1
    assert len(retry) == len(request) + 1


def test_next_request_marks_previous_response_without_usage_unavailable(
    tmp_path: Path,
) -> None:
    engine = ContextHandoffEngine(_policy(), repository=_repository(tmp_path / "db"))
    engine.update_model("model-a", 100_000)
    engine.on_session_start("session-no-usage")
    request: list[dict[str, object]] = [{"role": "user", "content": "status"}]
    engine.update_from_response(
        {"prompt_tokens": 800, "completion_tokens": 20, "total_tokens": 820}
    )

    with_actual = engine.select_context(request)
    without_usage = engine.select_context(request)

    assert "Prompt tokens, last actual: 800" in _content(with_actual)
    assert "Prompt tokens, last actual: unavailable" in _content(without_usage)


def test_model_switch_and_missing_usage_are_immediately_diagnostic(
    tmp_path: Path,
) -> None:
    engine = ContextHandoffEngine(_policy(), repository=_repository(tmp_path / "db"))
    engine.on_session_start("session-switch")
    request: list[dict[str, object]] = [{"role": "user", "content": "status"}]

    engine.update_model("model-a", 100_000)
    engine.update_from_response({})
    first = engine.select_context(request)
    engine.update_model("model-b", 80_000)
    second = engine.select_context(request)
    engine.update_model("unknown", 60_000)
    unmatched = engine.select_context(request)

    assert first is not None
    assert second is not None
    assert unmatched is not None
    assert "Model: model-a" in _content(first)
    assert "Prompt tokens, last actual: unavailable" in _content(first)
    assert "Configured sweet zone starts at: 50000 tokens" in _content(first)
    assert "Model: model-b" in _content(second)
    assert "Context limit: 80000" in _content(second)
    assert "Configured sweet zone starts at: 30000 tokens" in _content(second)
    assert "Model: unknown" in _content(unmatched)
    assert "Handoff policy: unmatched (observation only)" in _content(unmatched)
    assert "Configured sweet zone starts at: not configured" in _content(unmatched)


def test_session_lifecycle_recovers_active_task_and_segment(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "data.db")
    activation = TaskService(repository).create_task(
        "session-active",
        "P3",
        "Expose the active pointer",
        task_id="task-active",
    )
    engine = ContextHandoffEngine(repository=repository)
    engine.update_model("unconfigured", 100_000)
    engine.on_session_start("session-active")

    selected = engine.select_context([{"role": "user", "content": "continue"}])
    engine.on_session_reset()
    after_reset = engine.select_context(
        [{"role": "user", "content": "new session not started"}]
    )

    assert selected is not None
    assert "Active task: task-active" in _content(selected)
    assert f"Context segment: {activation.segment.context_segment_id}" in _content(
        selected
    )
    assert after_reset is not None
    assert "Active task: none" in _content(after_reset)
    assert engine.last_prompt_tokens == 0
