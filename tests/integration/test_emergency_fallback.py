from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

from chris_hermes_agent.context_engine import ContextHandoffEngine
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_models import EventType
from chris_hermes_agent.task_service import TaskService


class CompressionDelegate:
    _last_compress_aborted = False

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.usage_updates: list[dict[str, object]] = []

    def compress(self, messages: list[dict], **_: object) -> list[dict]:
        if self.fail:
            raise RuntimeError("sensitive provider error")
        system = [message for message in messages if message.get("role") == "system"]
        return [*system[:1], {"role": "user", "content": "EMERGENCY SUMMARY"}]

    def update_from_response(self, usage: dict[str, object]) -> None:
        self.usage_updates.append(usage)


def _policy() -> dict[str, object]:
    return {
        "model_policies": {
            "model-a": {
                "handoff_enabled": True,
                "sweet_zone": {"type": "absolute_tokens", "start": 500},
                "emergency": {
                    "enabled": True,
                    "type": "absolute_tokens",
                    "threshold": 1_000,
                },
            },
            "observation-only": {
                "handoff_enabled": False,
                "emergency_enabled": False,
            },
        }
    }


def _engine(
    tmp_path: Path,
    *,
    delegate: CompressionDelegate,
) -> tuple[ContextHandoffEngine, TaskRepository, str]:
    connection = sqlite3.connect(tmp_path / "data.db", check_same_thread=False)
    initialize_database(connection)
    repository = TaskRepository(connection)
    activation = TaskService(repository).create_task(
        "session-1", "P6", "Emergency fallback", task_id="task-1"
    )
    engine = ContextHandoffEngine(
        _policy(),
        repository=repository,
        compression_delegate=delegate,
        emergency_archive_directory=tmp_path / "archives",
    )
    engine.update_model("model-a", 2_000, provider="provider-a")
    engine.on_session_start("session-1")
    return engine, repository, activation.segment.context_segment_id


def _large_request() -> tuple[list[dict], list[dict]]:
    conversation = [
        {"role": "user", "content": "long task"},
        {"role": "tool", "tool_call_id": "old", "content": "TRACE-" + "x" * 8_000},
    ]
    return [{"role": "system", "content": "SYSTEM"}, *conversation], conversation


def test_emergency_is_policy_gated_and_uses_request_only_compression(
    tmp_path: Path,
) -> None:
    delegate = CompressionDelegate()
    engine, repository, segment_id = _engine(tmp_path, delegate=delegate)
    request, conversation = _large_request()
    canonical_snapshot = copy.deepcopy(conversation)

    selected = engine.select_context(request, conversation_messages=conversation)
    assert engine.estimated_prompt_tokens >= 1_000
    assert engine.threshold_tokens == 1_000
    assert engine.should_compress(engine.estimated_prompt_tokens) is True

    returned = engine.compress(
        conversation,
        current_tokens=engine.estimated_prompt_tokens,
    )
    after = engine.select_context(request, conversation_messages=conversation)

    assert returned is conversation
    assert conversation == canonical_snapshot
    assert "TRACE-" in str(selected)
    assert "TRACE-" not in str(after)
    assert "EMERGENCY SUMMARY" in str(after)
    assert "Emergency compression state: completed" in str(after[-1]["content"])
    assert engine.should_compress(engine.estimated_prompt_tokens) is False
    assert engine.compression_count == 1
    segment = repository.get_segment(segment_id)
    assert segment is not None
    assert segment.archived_context_reference is not None
    assert [
        event.event_type
        for event in repository.list_events("task-1")
        if event.event_type.name.startswith("EMERGENCY_")
    ] == [
        EventType.EMERGENCY_COMPRESSION_TRIGGERED,
        EventType.EMERGENCY_COMPRESSION_COMPLETED,
    ]

    engine.update_from_response(
        {"prompt_tokens": 300, "completion_tokens": 20, "total_tokens": 320}
    )
    assert delegate.usage_updates[-1]["prompt_tokens"] == 300

    engine.update_model("observation-only", 2_000)
    assert engine.threshold_tokens == 0
    assert engine.should_compress(1_999) is False


def test_failed_delegate_blocks_repeat_and_surfaces_safe_runtime_state(
    tmp_path: Path,
) -> None:
    engine, repository, _ = _engine(
        tmp_path,
        delegate=CompressionDelegate(fail=True),
    )
    request, conversation = _large_request()
    engine.select_context(request, conversation_messages=conversation)

    returned = engine.compress(
        conversation,
        current_tokens=engine.estimated_prompt_tokens,
    )
    retry = engine.select_context(request, conversation_messages=conversation)
    decision, reason = engine.should_compress_info(engine.estimated_prompt_tokens)

    assert returned is conversation
    assert "TRACE-" in str(retry)
    assert "Emergency compression state: failed (delegate_error)" in str(
        retry[-1]["content"]
    )
    assert decision is False
    assert reason == "emergency_failed:delegate_error"
    assert repository.list_events("task-1")[-1].event_type is (
        EventType.EMERGENCY_COMPRESSION_FAILED
    )


def test_completed_emergency_context_restores_after_engine_restart(
    tmp_path: Path,
) -> None:
    delegate = CompressionDelegate()
    engine, repository, _ = _engine(tmp_path, delegate=delegate)
    request, conversation = _large_request()
    engine.select_context(request, conversation_messages=conversation)
    engine.compress(conversation, current_tokens=engine.estimated_prompt_tokens)

    restarted = ContextHandoffEngine(
        _policy(),
        repository=repository,
        compression_delegate=CompressionDelegate(),
        emergency_archive_directory=tmp_path / "archives",
    )
    restarted.update_model("model-a", 2_000, provider="provider-a")
    restarted.on_session_start("session-1")
    restored = restarted.select_context(
        request,
        conversation_messages=conversation,
    )

    assert "TRACE-" not in str(restored)
    assert "EMERGENCY SUMMARY" in str(restored)
    assert "Emergency compression state: completed" in str(restored[-1]["content"])


def test_archive_storage_failure_keeps_canonical_history_unchanged(
    tmp_path: Path,
) -> None:
    (tmp_path / "archives").write_text("not a directory", encoding="utf-8")
    engine, repository, _ = _engine(tmp_path, delegate=CompressionDelegate())
    request, conversation = _large_request()
    canonical_snapshot = copy.deepcopy(conversation)
    engine.select_context(request, conversation_messages=conversation)

    returned = engine.compress(
        conversation,
        current_tokens=engine.estimated_prompt_tokens,
    )
    retry = engine.select_context(request, conversation_messages=conversation)

    assert returned is conversation
    assert conversation == canonical_snapshot
    assert "Emergency compression state: failed (emergency_unavailable)" in str(
        retry[-1]["content"]
    )
    assert not [
        event
        for event in repository.list_events("task-1")
        if event.event_type.name.startswith("EMERGENCY_")
    ]


def test_default_archive_directory_is_next_to_profile_database(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "data.db", check_same_thread=False)
    initialize_database(connection)
    repository = TaskRepository(connection)
    activation = TaskService(repository).create_task(
        "session-1", "P6", "Default archive", task_id="task-1"
    )
    engine = ContextHandoffEngine(
        _policy(),
        repository=repository,
        compression_delegate=CompressionDelegate(),
    )
    engine.update_model("model-a", 2_000, provider="provider-a")
    engine.on_session_start("session-1")
    request, conversation = _large_request()
    engine.select_context(request, conversation_messages=conversation)

    engine.compress(conversation, current_tokens=engine.estimated_prompt_tokens)

    segment = repository.get_segment(activation.segment.context_segment_id)
    assert segment is not None
    assert segment.archived_context_reference is not None
    assert (tmp_path / segment.archived_context_reference).is_file()
