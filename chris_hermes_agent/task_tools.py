"""Task-layer tool schemas and fail-closed Hermes handlers."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .checkpoint_service import CheckpointService
from .migrations import initialize_database
from .store import TaskRepository
from .task_models import EventType, TaskStatus
from .task_service import TaskService, TaskServiceError


def _string_list_schema(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {"type": "string", "minLength": 1},
    }


_TASK_STATE_PROPERTIES: dict[str, Any] = {
    "title": {
        "type": "string",
        "minLength": 1,
        "description": "Task title; required when action=create.",
    },
    "goal": {
        "type": "string",
        "minLength": 1,
        "description": "Task success condition; required when action=create.",
    },
    "constraints": _string_list_schema("Still-applicable Task constraints."),
    "current_phase": {
        "type": "string",
        "description": "Smallest useful current Task phase label.",
    },
    "completed": _string_list_schema("Verified completed Task outcomes."),
    "in_progress": _string_list_schema(
        "Current Task work. Use this instead of Checkpoint current_state."
    ),
    "known_issues": _string_list_schema("Unresolved Task issues."),
    "next_actions": _string_list_schema("Ordered executable Task next actions."),
    "decisions": _string_list_schema("Durable Task decisions with rationale."),
    "artifacts": _string_list_schema("Durable Task artifact identifiers."),
    "search_aliases": _string_list_schema("Alternative Task search phrases."),
    "tags": _string_list_schema("Short Task classification tags."),
}

_CHECKPOINT_PROPERTIES: dict[str, Any] = {
    "goal": {"type": "string", "minLength": 1},
    "constraints": _string_list_schema("Still-applicable requirements."),
    "current_phase": {"type": "string"},
    "completed": _string_list_schema("Verified outcomes."),
    "current_state": _string_list_schema("Facts required to resume now."),
    "decisions": _string_list_schema("Durable decisions with rationale."),
    "rejected_alternatives": {
        **_string_list_schema(
            "Only viable approaches actually considered for this Task and explicitly "
            "rejected. Each item must explain why it was rejected. Do not put "
            "constraints, authorization boundaries, general prohibitions, accepted "
            "decisions, or unresolved known_issues here; use the matching Checkpoint "
            "field. Use [] when no viable alternative was actually evaluated."
        ),
        "items": {
            "type": "string",
            "minLength": 1,
            "description": (
                "One viable approach actually considered and why it was rejected; "
                "never a general prohibition or instruction."
            ),
        },
    },
    "known_issues": _string_list_schema("Unresolved issues and impact."),
    "artifacts": _string_list_schema("Exact durable artifact identifiers."),
    "next_actions": {
        **_string_list_schema("Ordered concrete continuation actions."),
        "minItems": 1,
    },
}


TASK_STATE_MANAGE_SCHEMA: dict[str, Any] = {
    "name": "task_state_manage",
    "description": (
        "Create, inspect, update, pause, search, resume, block, complete, or "
        "cancel durable long-running tasks. Starting a new task pauses an "
        "unfinished active task only after a valid checkpoint exists. When an "
        "exact Task ID is known, use action=get; an empty search never proves that "
        "Task is absent. Search defaults to every unfinished status. Do not create "
        "a duplicate when a continuation target is active in another Session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "get",
                    "update",
                    "pause",
                    "search",
                    "list",
                    "resume",
                    "block",
                    "complete",
                    "cancel",
                    "finalize",
                ],
            },
            "task_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Exact durable Task ID. If history supplies this ID, use "
                    "action=get instead of search to verify existence."
                ),
            },
            "parent_task_id": {"type": "string", "minLength": 1},
            "expected_version": {"type": "integer", "minimum": 0},
            "query": {"type": "string"},
            "statuses": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [status.value for status in TaskStatus],
                },
                "uniqueItems": True,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "state": {
                "type": "object",
                "description": (
                    "Task State fields only. Do not use Checkpoint-only fields such "
                    "as current_state or rejected_alternatives."
                ),
                "properties": _TASK_STATE_PROPERTIES,
                "additionalProperties": False,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

TASK_EVENT_APPEND_SCHEMA: dict[str, Any] = {
    "name": "task_event_append",
    "description": "Append a supported traceable decision or execution event.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "event_type": {
                "type": "string",
                "enum": [event_type.value for event_type in EventType],
            },
            "payload": {"type": "object"},
        },
        "required": ["task_id", "event_type", "payload"],
        "additionalProperties": False,
    },
}

CHECKPOINT_CREATE_SCHEMA: dict[str, Any] = {
    "name": "checkpoint_create",
    "description": (
        "Validate and persist a complete task checkpoint with non-empty Next "
        "Actions for later pause, resume, or context handoff."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "checkpoint": {
                "type": "object",
                "description": (
                    "Complete recovery Checkpoint. current_state and "
                    "rejected_alternatives belong here, not in Task State."
                ),
                "properties": _CHECKPOINT_PROPERTIES,
                "required": list(_CHECKPOINT_PROPERTIES),
                "additionalProperties": False,
            },
        },
        "required": ["task_id", "checkpoint"],
        "additionalProperties": False,
    },
}


def _success(data: Mapping[str, Any]) -> str:
    return json.dumps(
        {"ok": True, "data": dict(data)},
        ensure_ascii=False,
        sort_keys=True,
    )


def _error(code: str, message: str) -> str:
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False,
        sort_keys=True,
    )


def _default_repository() -> TaskRepository:
    from plugins.plugin_storage import (  # type: ignore[import-not-found]
        plugin_data_dir,
    )

    directory = plugin_data_dir("chris-hermes-agent")
    if directory.is_symlink():
        raise RuntimeError("Plugin data directory must not be a symbolic link.")
    os.chmod(directory, 0o700)
    database_path = directory / "data.db"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(database_path, flags | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = os.open(database_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(
                "Plugin database must be a regular file without hard links."
            )
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)

    connection = sqlite3.connect(database_path, check_same_thread=False)
    try:
        initialize_database(connection)
    except Exception:
        connection.close()
        raise
    return TaskRepository(connection)


class TaskToolHandlers:
    """Hermes JSON adapters over one lazily-created repository."""

    def __init__(self, repository: TaskRepository | None = None) -> None:
        self._repository = repository
        self._repository_lock = threading.Lock()

    @property
    def repository(self) -> TaskRepository:
        if self._repository is None:
            with self._repository_lock:
                if self._repository is None:
                    self._repository = _default_repository()
        return self._repository

    def task_state_manage(self, args: dict[str, Any], **kwargs: Any) -> str:
        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return _error(
                "missing_session_id",
                "Hermes runtime session_id is required for task state changes.",
            )
        action = args.get("action")
        action_schema = TASK_STATE_MANAGE_SCHEMA["parameters"]["properties"]["action"]
        if action not in action_schema["enum"]:
            return _error("invalid_action", f"Unsupported task action: {action!r}")
        try:
            return self._dispatch_task_action(str(action), args, session_id)
        except TaskServiceError as exc:
            return _error(exc.code, exc.message)
        except (KeyError, TypeError, ValueError) as exc:
            return _error("invalid_argument", str(exc))
        except sqlite3.Error as exc:
            return _error("storage_error", f"SQLite operation failed: {exc}")
        except Exception as exc:  # pragma: no cover - defensive host boundary
            return _error(
                "internal_error", f"Task operation failed: {type(exc).__name__}"
            )

    def task_event_append(self, args: dict[str, Any], **kwargs: Any) -> str:
        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return _error(
                "missing_session_id", "Hermes runtime session_id is required."
            )
        try:
            task_id = self._text_arg(args, "task_id")
            payload = args.get("payload")
            if not isinstance(payload, Mapping):
                raise TaskServiceError("invalid_argument", "payload must be an object.")
            raw_event_type = args.get("event_type")
            if not isinstance(raw_event_type, str):
                raise TaskServiceError(
                    "invalid_event_type",
                    f"Unsupported event type: {raw_event_type!r}.",
                )
            try:
                event_type = EventType(raw_event_type)
            except ValueError as exc:
                raise TaskServiceError(
                    "invalid_event_type",
                    f"Unsupported event type: {raw_event_type!r}.",
                ) from exc
            event = TaskService(self.repository).append_event(
                task_id,
                session_id,
                event_type,
                payload,
            )
            return _success({"event": event.to_json_dict()})
        except TaskServiceError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error as exc:
            return _error("storage_error", f"SQLite operation failed: {exc}")

    def checkpoint_create(self, args: dict[str, Any], **kwargs: Any) -> str:
        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return _error(
                "missing_session_id", "Hermes runtime session_id is required."
            )
        try:
            task_id = self._text_arg(args, "task_id")
            checkpoint = args.get("checkpoint")
            if not isinstance(checkpoint, Mapping):
                raise TaskServiceError(
                    "invalid_argument", "checkpoint must be an object."
                )
            result = CheckpointService(self.repository).create_checkpoint(
                task_id,
                session_id,
                checkpoint,
            )
            return _success({"checkpoint": result.to_json_dict()})
        except TaskServiceError as exc:
            return _error(exc.code, exc.message)
        except sqlite3.Error as exc:
            return _error("storage_error", f"SQLite operation failed: {exc}")

    def _dispatch_task_action(
        self,
        action: str,
        args: dict[str, Any],
        session_id: str,
    ) -> str:
        service = TaskService(self.repository)
        if action == "create":
            state = self._state_arg(args)
            title = self._state_text(state, "title")
            goal = self._state_text(state, "goal")
            remaining_state = dict(state)
            remaining_state.pop("title", None)
            remaining_state.pop("goal", None)
            activation = service.create_task(
                session_id,
                title,
                goal,
                remaining_state,
                parent_task_id=args.get("parent_task_id"),
                task_id=args.get("task_id"),
            )
            return _success(activation.to_json_dict())
        if action == "get":
            task_id = args.get("task_id")
            task = (
                service.get_task(self._text_arg(args, "task_id"))
                if task_id is not None
                else service.get_active_task(session_id)
            )
            return _success({"task": task.to_json_dict() if task is not None else None})
        if action == "update":
            task = service.update_task(
                self._text_arg(args, "task_id"),
                session_id,
                expected_version=self._version_arg(args),
                changes=self._state_arg(args),
            )
            return _success({"task": task.to_json_dict()})
        if action == "pause":
            task = service.pause_task(
                self._text_arg(args, "task_id"),
                session_id,
                expected_version=self._version_arg(args),
            )
            return _success({"task": task.to_json_dict()})
        if action == "resume":
            activation = service.resume_task(
                self._text_arg(args, "task_id"),
                session_id,
                expected_version=self._version_arg(args),
            )
            data = activation.to_json_dict()
            data["context_rotation_applied"] = False
            data["context_rotation_required"] = True
            data["next_required_action"] = "call_handoff_context"
            return _success(data)
        if action == "search":
            results = service.search_tasks(
                query=str(args.get("query") or ""),
                statuses=self._status_args(
                    args,
                    default=(
                        TaskStatus.ACTIVE,
                        TaskStatus.PAUSED,
                        TaskStatus.BLOCKED,
                    ),
                ),
                limit=self._limit_arg(args),
            )
            return _success(
                {"candidates": [result.to_json_dict() for result in results]}
            )
        if action == "list":
            tasks = service.list_tasks(
                statuses=self._status_args(args, default=()),
                limit=self._limit_arg(args),
            )
            return _success({"tasks": [task.to_json_dict() for task in tasks]})
        status_by_action = {
            "block": TaskStatus.BLOCKED,
            "complete": TaskStatus.COMPLETED,
            "finalize": TaskStatus.COMPLETED,
            "cancel": TaskStatus.CANCELLED,
        }
        task = service.transition_task(
            self._text_arg(args, "task_id"),
            session_id,
            expected_version=self._version_arg(args),
            status=status_by_action[action],
        )
        return _success({"task": task.to_json_dict()})

    @staticmethod
    def _state_arg(args: Mapping[str, Any]) -> Mapping[str, object]:
        state = args.get("state")
        if not isinstance(state, Mapping):
            raise TaskServiceError("invalid_argument", "state must be an object.")
        return state

    @staticmethod
    def _state_text(state: Mapping[str, object], key: str) -> str:
        value = state.get(key)
        if not isinstance(value, str) or not value.strip():
            raise TaskServiceError(
                "invalid_argument", f"state.{key} must be a non-empty string."
            )
        return value

    @staticmethod
    def _text_arg(args: Mapping[str, Any], key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise TaskServiceError(
                "invalid_argument", f"{key} must be a non-empty string."
            )
        return value

    @staticmethod
    def _version_arg(args: Mapping[str, Any]) -> int:
        value = args.get("expected_version")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TaskServiceError(
                "invalid_argument", "expected_version must be a non-negative integer."
            )
        return value

    @staticmethod
    def _limit_arg(args: Mapping[str, Any]) -> int:
        value = args.get("limit", 10)
        if not isinstance(value, int) or isinstance(value, bool):
            raise TaskServiceError("invalid_limit", "limit must be an integer.")
        return value

    @staticmethod
    def _status_args(
        args: Mapping[str, Any],
        *,
        default: Sequence[TaskStatus],
    ) -> tuple[TaskStatus, ...]:
        raw = args.get("statuses")
        if raw is None:
            return tuple(default)
        if not isinstance(raw, list):
            raise TaskServiceError("invalid_status", "statuses must be an array.")
        try:
            return tuple(TaskStatus(value) for value in raw)
        except (TypeError, ValueError) as exc:
            raise TaskServiceError(
                "invalid_status", "statuses contains an unsupported value."
            ) from exc


_DEFAULT_HANDLERS = TaskToolHandlers()


def get_default_repository() -> TaskRepository:
    """Share the profile-scoped repository with the active ContextEngine."""
    return _DEFAULT_HANDLERS.repository


TASK_TOOL_REGISTRATIONS: tuple[tuple[str, dict[str, Any], Callable[..., str]], ...] = (
    (
        TASK_STATE_MANAGE_SCHEMA["name"],
        TASK_STATE_MANAGE_SCHEMA,
        _DEFAULT_HANDLERS.task_state_manage,
    ),
    (
        TASK_EVENT_APPEND_SCHEMA["name"],
        TASK_EVENT_APPEND_SCHEMA,
        _DEFAULT_HANDLERS.task_event_append,
    ),
    (
        CHECKPOINT_CREATE_SCHEMA["name"],
        CHECKPOINT_CREATE_SCHEMA,
        _DEFAULT_HANDLERS.checkpoint_create,
    ),
)
