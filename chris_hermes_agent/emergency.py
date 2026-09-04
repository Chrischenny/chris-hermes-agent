"""Durable, fail-closed Emergency Context compression orchestration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from agent.model_metadata import estimate_messages_tokens_rough

from .store import TaskRepository
from .task_models import EventType
from .task_service import new_id, utc_now


class CompressionDelegate(Protocol):
    """Subset of Hermes ContextCompressor used by Emergency Fallback."""

    _last_compress_aborted: bool

    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        memory_context: str = "",
    ) -> list[dict[str, Any]]: ...


class EmergencyState(StrEnum):
    NOT_TRIGGERED = "not_triggered"
    TRIGGERED = "triggered"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EmergencyCompressionStatus:
    state: EmergencyState = EmergencyState.NOT_TRIGGERED
    attempt_id: str | None = None
    archive_reference: str | None = None
    archive_checksum: str | None = None
    error_code: str | None = None

    @property
    def runtime_label(self) -> str:
        if self.state is EmergencyState.FAILED and self.error_code:
            return f"failed ({self.error_code})"
        return self.state.value


@dataclass(frozen=True, slots=True)
class RestoredEmergencyContext:
    messages: list[dict[str, Any]]
    conversation_message_count: int
    conversation_checksum: str


@dataclass(frozen=True, slots=True)
class EmergencyCompressionResult:
    applied: bool
    compressed_messages: list[dict[str, Any]]
    post_request_tokens: int
    status: EmergencyCompressionStatus


def conversation_checksum(
    messages: list[dict[str, Any]],
    *,
    count: int | None = None,
) -> str:
    """Bind an archive to one exact canonical conversation prefix."""
    prefix_count = len(messages) if count is None else count
    if not 0 <= prefix_count <= len(messages):
        raise ValueError("Conversation checksum count is outside message history.")
    digest = hashlib.sha256(b"chris-hermes-conversation-v1\0")
    for message in messages[:prefix_count]:
        normalized = {
            key: value
            for key, value in message.items()
            if not key.startswith("_") and key != "timestamp" and key != "tool_name"
        }
        if "name" not in normalized and isinstance(message.get("tool_name"), str):
            normalized["name"] = message["tool_name"]
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


class EmergencyCompressionService:
    """Archive the selected request, invoke Hermes, and persist safe status."""

    def __init__(
        self,
        repository: TaskRepository,
        *,
        archive_directory: Path,
    ) -> None:
        self.repository = repository
        self.archive_directory = archive_directory.resolve()
        self._archive_root = self.archive_directory.parent

    def compress(
        self,
        *,
        session_id: str,
        task_id: str,
        context_segment_id: str,
        active_messages: list[dict[str, Any]],
        conversation_message_count: int,
        conversation_checksum: str,
        request_tokens: int,
        emergency_threshold_tokens: int,
        policy_snapshot: str,
        delegate: CompressionDelegate,
    ) -> EmergencyCompressionResult:
        attempt_id = new_id("emergency")
        archive_reference = self._new_archive_reference()
        base_document: dict[str, Any] = {
            "format_version": 2,
            "attempt_id": attempt_id,
            "state": EmergencyState.TRIGGERED.value,
            "task_id": task_id,
            "context_segment_id": context_segment_id,
            "session_id": session_id,
            "conversation_message_count": conversation_message_count,
            "conversation_checksum": conversation_checksum,
            "request_tokens": request_tokens,
            "emergency_threshold_tokens": emergency_threshold_tokens,
            "policy_snapshot": policy_snapshot,
            "active_messages": copy.deepcopy(active_messages),
            "compressed_messages": None,
            "post_request_tokens": None,
            "error_code": None,
            "created_at": utc_now(),
        }
        checksum = self._write_new_archive(archive_reference, base_document)
        active_message_tokens = estimate_messages_tokens_rough(active_messages)
        with self.repository.transaction():
            self.repository.update_segment_archive_reference(
                context_segment_id, archive_reference
            )
            self._append_event(
                task_id=task_id,
                session_id=session_id,
                event_type=EventType.EMERGENCY_COMPRESSION_TRIGGERED,
                payload={
                    "attempt_id": attempt_id,
                    "context_segment_id": context_segment_id,
                    "archive_reference": archive_reference,
                    "archive_checksum": checksum,
                    "request_tokens": request_tokens,
                    "threshold_tokens": emergency_threshold_tokens,
                },
            )

        try:
            compressed = delegate.compress(
                active_messages,
                current_tokens=request_tokens,
                force=True,
            )
        except Exception:
            return self._fail(
                base_document,
                archive_reference,
                active_messages,
                request_tokens,
                "delegate_error",
            )

        if getattr(delegate, "_last_compress_aborted", False):
            return self._fail(
                base_document,
                archive_reference,
                active_messages,
                request_tokens,
                "delegate_aborted",
            )
        if not self._valid_messages(compressed):
            return self._fail(
                base_document,
                archive_reference,
                active_messages,
                request_tokens,
                "invalid_result",
            )
        if compressed == active_messages:
            return self._fail(
                base_document,
                archive_reference,
                active_messages,
                request_tokens,
                "no_progress",
            )

        request_overhead = max(
            0,
            request_tokens - active_message_tokens,
        )
        post_tokens = estimate_messages_tokens_rough(compressed) + request_overhead
        if post_tokens >= emergency_threshold_tokens:
            return self._fail(
                base_document,
                archive_reference,
                active_messages,
                post_tokens,
                "threshold_not_recovered",
            )

        compressed_snapshot = copy.deepcopy(compressed)
        completed = {
            **base_document,
            "state": EmergencyState.COMPLETED.value,
            "compressed_messages": compressed_snapshot,
            "post_request_tokens": post_tokens,
        }
        completed_checksum = self._replace_archive(archive_reference, completed)
        with self.repository.transaction():
            self._append_event(
                task_id=task_id,
                session_id=session_id,
                event_type=EventType.EMERGENCY_COMPRESSION_COMPLETED,
                payload={
                    "attempt_id": attempt_id,
                    "context_segment_id": context_segment_id,
                    "archive_reference": archive_reference,
                    "archive_checksum": completed_checksum,
                    "request_tokens": request_tokens,
                    "post_request_tokens": post_tokens,
                    "threshold_tokens": emergency_threshold_tokens,
                },
            )
        status = EmergencyCompressionStatus(
            state=EmergencyState.COMPLETED,
            attempt_id=attempt_id,
            archive_reference=archive_reference,
            archive_checksum=completed_checksum,
        )
        return EmergencyCompressionResult(
            True, compressed_snapshot, post_tokens, status
        )

    def restore(
        self,
        task_id: str,
        context_segment_id: str,
        *,
        conversation_messages: list[dict[str, Any]],
    ) -> tuple[RestoredEmergencyContext | None, EmergencyCompressionStatus]:
        segment = self.repository.get_segment(context_segment_id)
        if (
            segment is None
            or segment.task_id != task_id
            or segment.archived_context_reference is None
        ):
            return None, EmergencyCompressionStatus()
        reference = segment.archived_context_reference
        try:
            document = self.load_archive(reference)
        except (OSError, ValueError, json.JSONDecodeError):
            return None, EmergencyCompressionStatus(
                state=EmergencyState.FAILED,
                archive_reference=reference,
                error_code="archive_corrupt",
            )
        if (
            document.get("task_id") != task_id
            or document.get("context_segment_id") != context_segment_id
        ):
            return None, EmergencyCompressionStatus(
                state=EmergencyState.FAILED,
                archive_reference=reference,
                error_code="archive_mismatch",
            )
        if document.get("format_version") != 2:
            return None, EmergencyCompressionStatus(
                state=EmergencyState.FAILED,
                archive_reference=reference,
                error_code="archive_version_unsupported",
            )
        try:
            state = EmergencyState(str(document.get("state")))
        except ValueError:
            return None, EmergencyCompressionStatus(
                state=EmergencyState.FAILED,
                archive_reference=reference,
                error_code="archive_corrupt",
            )
        status = EmergencyCompressionStatus(
            state=state,
            attempt_id=self._optional_text(document.get("attempt_id")),
            archive_reference=reference,
            archive_checksum=self._optional_text(document.get("content_checksum")),
            error_code=self._optional_text(document.get("error_code")),
        )
        compressed = document.get("compressed_messages")
        count = document.get("conversation_message_count")
        expected_conversation_checksum = document.get("conversation_checksum")
        if state is not EmergencyState.COMPLETED:
            return None, status
        if (
            not self._valid_messages(compressed)
            or not isinstance(count, int)
            or not isinstance(expected_conversation_checksum, str)
        ):
            return None, EmergencyCompressionStatus(
                state=EmergencyState.FAILED,
                archive_reference=reference,
                error_code="archive_corrupt",
            )
        if not 0 <= count <= len(conversation_messages):
            return None, EmergencyCompressionStatus(
                state=EmergencyState.FAILED,
                archive_reference=reference,
                error_code="conversation_anchor_invalid",
            )
        if (
            conversation_checksum(conversation_messages, count=count)
            != expected_conversation_checksum
        ):
            return None, EmergencyCompressionStatus(
                state=EmergencyState.FAILED,
                archive_reference=reference,
                error_code="conversation_changed",
            )
        return RestoredEmergencyContext(
            cast(list[dict[str, Any]], compressed),
            count,
            expected_conversation_checksum,
        ), status

    def load_archive(self, archive_reference: str | None) -> dict[str, Any]:
        if archive_reference is None:
            raise ValueError("Archive reference is missing.")
        path = self._resolve_reference(archive_reference)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Archive is not a regular file.")
            raw = stream.read()
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("Archive root must be an object.")
        expected = loaded.get("content_checksum")
        document = dict(loaded)
        document.pop("content_checksum", None)
        if not isinstance(expected, str) or expected != self._checksum(document):
            raise ValueError("Archive checksum does not match.")
        return loaded

    def _fail(
        self,
        base_document: dict[str, Any],
        archive_reference: str,
        original_messages: list[dict[str, Any]],
        post_tokens: int,
        error_code: str,
    ) -> EmergencyCompressionResult:
        failed = {
            **base_document,
            "state": EmergencyState.FAILED.value,
            "post_request_tokens": post_tokens,
            "error_code": error_code,
        }
        checksum = self._replace_archive(archive_reference, failed)
        task_id = str(base_document["task_id"])
        session_id = str(base_document["session_id"])
        with self.repository.transaction():
            self._append_event(
                task_id=task_id,
                session_id=session_id,
                event_type=EventType.EMERGENCY_COMPRESSION_FAILED,
                payload={
                    "attempt_id": base_document["attempt_id"],
                    "context_segment_id": base_document["context_segment_id"],
                    "archive_reference": archive_reference,
                    "archive_checksum": checksum,
                    "error_code": error_code,
                    "request_tokens": base_document["request_tokens"],
                    "threshold_tokens": base_document["emergency_threshold_tokens"],
                },
            )
        status = EmergencyCompressionStatus(
            state=EmergencyState.FAILED,
            attempt_id=str(base_document["attempt_id"]),
            archive_reference=archive_reference,
            archive_checksum=checksum,
            error_code=error_code,
        )
        return EmergencyCompressionResult(False, original_messages, post_tokens, status)

    def _append_event(
        self,
        *,
        task_id: str,
        session_id: str,
        event_type: EventType,
        payload: Mapping[str, Any],
    ) -> None:
        self.repository.append_event(
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            session_id=session_id,
            created_at=utc_now(),
            event_id=new_id("event"),
        )

    def _new_archive_reference(self) -> str:
        filename = f"{uuid4().hex}.json"
        return str(Path(self.archive_directory.name) / filename)

    def _resolve_reference(self, reference: str) -> Path:
        candidate = (self._archive_root / reference).resolve()
        if candidate.parent != self.archive_directory:
            raise ValueError("Archive reference is outside the archive directory.")
        if candidate.suffix != ".json" or len(candidate.stem) != 32:
            raise ValueError("Archive reference format is invalid.")
        try:
            int(candidate.stem, 16)
        except ValueError as exc:
            raise ValueError("Archive reference format is invalid.") from exc
        return candidate

    def _prepare_directory(self) -> None:
        self.archive_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.archive_directory, 0o700)

    def _write_new_archive(self, reference: str, document: dict[str, Any]) -> str:
        self._prepare_directory()
        path = self._resolve_reference(reference)
        encoded, checksum = self._encoded(document)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._fsync_archive_directory()
        return checksum

    def _replace_archive(self, reference: str, document: dict[str, Any]) -> str:
        destination = self._resolve_reference(reference)
        temporary = destination.with_name(f"{uuid4().hex}.tmp")
        encoded, checksum = self._encoded(document)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            self._fsync_archive_directory()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return checksum

    def _fsync_archive_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.archive_directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _encoded(self, document: dict[str, Any]) -> tuple[bytes, str]:
        clean = dict(document)
        clean.pop("content_checksum", None)
        checksum = self._checksum(clean)
        complete = {**clean, "content_checksum": checksum}
        return (
            json.dumps(
                complete,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
            checksum,
        )

    @staticmethod
    def _checksum(document: dict[str, Any]) -> str:
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _valid_messages(value: object) -> bool:
        return isinstance(value, list) and all(
            isinstance(message, dict) for message in value
        )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) else None
