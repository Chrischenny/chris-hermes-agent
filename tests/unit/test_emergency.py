from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from chris_hermes_agent.emergency import (
    EmergencyCompressionService,
    EmergencyState,
    conversation_checksum,
)
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_models import EventType
from chris_hermes_agent.task_service import TaskService


class SuccessfulDelegate:
    _last_compress_aborted = False

    def __init__(self, compressed: list[dict[str, object]]) -> None:
        self.compressed = compressed
        self.calls: list[list[dict[str, object]]] = []

    def compress(
        self,
        messages: list[dict[str, object]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        memory_context: str = "",
    ) -> list[dict[str, object]]:
        del current_tokens, focus_topic, force, memory_context
        self.calls.append(messages)
        return self.compressed


class FailingDelegate:
    _last_compress_aborted = False

    def compress(self, messages: list[dict[str, object]], **_: object) -> list:
        del messages
        raise RuntimeError("provider leaked-detail")


class MutatingDelegate:
    _last_compress_aborted = False

    def compress(self, messages: list[dict[str, object]], **_: object) -> list:
        messages[0]["content"] = "delegate mutation"
        return [{"role": "user", "content": "compressed"}]


def _service(
    tmp_path: Path,
) -> tuple[TaskRepository, EmergencyCompressionService, str, str]:
    connection = sqlite3.connect(tmp_path / "data.db", check_same_thread=False)
    initialize_database(connection)
    repository = TaskRepository(connection)
    activation = TaskService(repository).create_task(
        "session-1", "Emergency", "Protect active context", task_id="task-1"
    )
    service = EmergencyCompressionService(
        repository,
        archive_directory=tmp_path / "archives",
    )
    return (
        repository,
        service,
        activation.task.task_id,
        activation.segment.context_segment_id,
    )


def test_success_archives_full_context_with_restricted_permissions_and_events(
    tmp_path: Path,
) -> None:
    repository, service, task_id, segment_id = _service(tmp_path)
    secret_marker = "secret-context-value"
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "tool", "content": secret_marker, "tool_call_id": "call-1"},
        {"role": "user", "content": "x" * 8_000},
    ]
    delegate = SuccessfulDelegate(
        [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "compressed recovery state"},
        ]
    )

    result = service.compress(
        session_id="session-1",
        task_id=task_id,
        context_segment_id=segment_id,
        active_messages=messages,
        conversation_message_count=2,
        conversation_checksum=conversation_checksum(messages, count=2),
        request_tokens=2_100,
        emergency_threshold_tokens=2_000,
        policy_snapshot='{"source":"exact:model:model-a"}',
        delegate=delegate,
    )

    assert result.applied is True
    assert result.status.state is EmergencyState.COMPLETED
    assert result.post_request_tokens < 2_000
    assert delegate.calls[0] == messages
    segment = repository.get_segment(segment_id)
    assert segment is not None
    assert segment.archived_context_reference == result.status.archive_reference
    archive_path = tmp_path / result.status.archive_reference
    archive = service.load_archive(result.status.archive_reference)
    assert archive["active_messages"] == messages
    assert archive["compressed_messages"] == delegate.compressed
    assert archive["state"] == "completed"
    assert archive["content_checksum"] == result.status.archive_checksum
    assert stat.S_IMODE(archive_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(archive_path.stat().st_mode) == 0o600

    events = repository.list_events(task_id)
    emergency_events = [
        event
        for event in events
        if event.event_type
        in {
            EventType.EMERGENCY_COMPRESSION_TRIGGERED,
            EventType.EMERGENCY_COMPRESSION_COMPLETED,
        }
    ]
    assert [event.event_type for event in emergency_events] == [
        EventType.EMERGENCY_COMPRESSION_TRIGGERED,
        EventType.EMERGENCY_COMPRESSION_COMPLETED,
    ]
    assert secret_marker not in json.dumps(
        [event.to_json_dict() for event in emergency_events]
    )


def test_delegate_exception_is_persisted_without_exposing_error_details(
    tmp_path: Path,
) -> None:
    repository, service, task_id, segment_id = _service(tmp_path)
    messages = [{"role": "user", "content": "x" * 8_000}]

    result = service.compress(
        session_id="session-1",
        task_id=task_id,
        context_segment_id=segment_id,
        active_messages=messages,
        conversation_message_count=1,
        conversation_checksum=conversation_checksum(messages),
        request_tokens=2_100,
        emergency_threshold_tokens=2_000,
        policy_snapshot="{}",
        delegate=FailingDelegate(),
    )

    assert result.applied is False
    assert result.compressed_messages == messages
    assert result.status.state is EmergencyState.FAILED
    assert result.status.error_code == "delegate_error"
    archive = service.load_archive(result.status.archive_reference)
    assert archive["state"] == "failed"
    assert archive["error_code"] == "delegate_error"
    assert "provider leaked-detail" not in json.dumps(archive)
    events = repository.list_events(task_id)
    assert events[-1].event_type is EventType.EMERGENCY_COMPRESSION_FAILED
    assert "provider leaked-detail" not in json.dumps(events[-1].to_json_dict())


def test_archive_retains_pre_delegate_snapshot_when_delegate_mutates_input(
    tmp_path: Path,
) -> None:
    _, service, task_id, segment_id = _service(tmp_path)
    messages = [{"role": "user", "content": "original" + "x" * 8_000}]
    expected = json.loads(json.dumps(messages))

    result = service.compress(
        session_id="session-1",
        task_id=task_id,
        context_segment_id=segment_id,
        active_messages=messages,
        conversation_message_count=1,
        conversation_checksum=conversation_checksum(messages),
        request_tokens=2_100,
        emergency_threshold_tokens=2_000,
        policy_snapshot="{}",
        delegate=MutatingDelegate(),
    )

    archive = service.load_archive(result.status.archive_reference)
    assert result.applied is True
    assert archive["active_messages"] == expected


def test_unsafe_or_no_progress_result_fails_closed(tmp_path: Path) -> None:
    _, service, task_id, segment_id = _service(tmp_path)
    messages = [{"role": "user", "content": "x" * 8_000}]
    delegate = SuccessfulDelegate(messages)

    result = service.compress(
        session_id="session-1",
        task_id=task_id,
        context_segment_id=segment_id,
        active_messages=messages,
        conversation_message_count=1,
        conversation_checksum=conversation_checksum(messages),
        request_tokens=2_100,
        emergency_threshold_tokens=2_000,
        policy_snapshot="{}",
        delegate=delegate,
    )

    assert result.applied is False
    assert result.status.state is EmergencyState.FAILED
    assert result.status.error_code == "no_progress"
    assert result.compressed_messages == messages


def test_result_that_remains_over_threshold_fails_closed(tmp_path: Path) -> None:
    _, service, task_id, segment_id = _service(tmp_path)
    messages = [{"role": "user", "content": "x" * 8_000}]

    result = service.compress(
        session_id="session-1",
        task_id=task_id,
        context_segment_id=segment_id,
        active_messages=messages,
        conversation_message_count=1,
        conversation_checksum=conversation_checksum(messages),
        request_tokens=2_100,
        emergency_threshold_tokens=2_000,
        policy_snapshot="{}",
        delegate=SuccessfulDelegate([{"role": "user", "content": "y" * 7_900}]),
    )

    assert result.applied is False
    assert result.status.state is EmergencyState.FAILED
    assert result.status.error_code == "threshold_not_recovered"
    assert result.compressed_messages == messages


def test_tampered_archive_is_rejected_during_restore(tmp_path: Path) -> None:
    _, service, task_id, segment_id = _service(tmp_path)
    messages = [{"role": "user", "content": "x" * 8_000}]
    result = service.compress(
        session_id="session-1",
        task_id=task_id,
        context_segment_id=segment_id,
        active_messages=messages,
        conversation_message_count=1,
        conversation_checksum=conversation_checksum(messages),
        request_tokens=2_100,
        emergency_threshold_tokens=2_000,
        policy_snapshot="{}",
        delegate=SuccessfulDelegate([{"role": "user", "content": "compressed"}]),
    )
    archive_path = tmp_path / result.status.archive_reference
    archive_path.write_text('{"tampered":true}', encoding="utf-8")

    restored, status = service.restore(
        task_id,
        segment_id,
        conversation_messages=messages,
    )

    assert restored is None
    assert status.state is EmergencyState.FAILED
    assert status.error_code == "archive_corrupt"


def test_archive_is_rejected_after_conversation_branch_changes(tmp_path: Path) -> None:
    _, service, task_id, segment_id = _service(tmp_path)
    conversation = [{"role": "user", "content": "original branch"}]
    result = service.compress(
        session_id="session-1",
        task_id=task_id,
        context_segment_id=segment_id,
        active_messages=[{"role": "user", "content": "x" * 8_000}],
        conversation_message_count=1,
        conversation_checksum=conversation_checksum(conversation),
        request_tokens=2_100,
        emergency_threshold_tokens=2_000,
        policy_snapshot="{}",
        delegate=SuccessfulDelegate([{"role": "user", "content": "compressed"}]),
    )

    restored, status = service.restore(
        task_id,
        segment_id,
        conversation_messages=[{"role": "user", "content": "rewritten branch"}],
    )

    assert result.applied is True
    assert restored is None
    assert status.state is EmergencyState.FAILED
    assert status.error_code == "conversation_changed"


def test_legacy_archive_is_not_restored_after_selection_rules_change(
    tmp_path: Path,
) -> None:
    _, service, task_id, segment_id = _service(tmp_path)
    conversation = [{"role": "user", "content": "canonical"}]
    result = service.compress(
        session_id="session-1",
        task_id=task_id,
        context_segment_id=segment_id,
        active_messages=[{"role": "user", "content": "selected context"}],
        conversation_message_count=1,
        conversation_checksum=conversation_checksum(conversation),
        request_tokens=2_100,
        emergency_threshold_tokens=2_000,
        policy_snapshot="{}",
        delegate=SuccessfulDelegate([{"role": "user", "content": "compressed"}]),
    )
    archive = service.load_archive(result.status.archive_reference)
    archive.pop("content_checksum")
    archive.pop("conversation_checksum")
    archive["format_version"] = 1
    service._replace_archive(result.status.archive_reference, archive)

    restored, status = service.restore(
        task_id,
        segment_id,
        conversation_messages=conversation,
    )

    assert restored is None
    assert status.state is EmergencyState.FAILED
    assert status.error_code == "archive_version_unsupported"


def test_archive_reference_cannot_escape_restricted_directory(
    tmp_path: Path,
) -> None:
    _, service, _, _ = _service(tmp_path)

    with pytest.raises(ValueError, match="outside"):
        service.load_archive("../outside.json")
