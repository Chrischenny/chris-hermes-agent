"""Task-layer tool contracts registered by the Hermes plugin."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

TASK_STATE_MANAGE_SCHEMA: dict[str, Any] = {
    "name": "task_state_manage",
    "description": (
        "Create, read, update, or finalize durable state for the active long-running "
        "task. Registered for contract stability but disabled until P2."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "get", "update", "finalize"],
            },
            "task_id": {"type": "string", "minLength": 1},
            "parent_task_id": {"type": "string", "minLength": 1},
            "state": {"type": "object"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

TASK_EVENT_APPEND_SCHEMA: dict[str, Any] = {
    "name": "task_event_append",
    "description": (
        "Append a traceable decision or execution event to a task. Registered "
        "for contract stability but disabled until P2."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "event_type": {"type": "string", "minLength": 1},
            "payload": {"type": "object"},
        },
        "required": ["task_id", "event_type", "payload"],
        "additionalProperties": False,
    },
}

CHECKPOINT_CREATE_SCHEMA: dict[str, Any] = {
    "name": "checkpoint_create",
    "description": (
        "Validate and persist a task checkpoint for a later context handoff. "
        "Registered for contract stability but disabled until P2."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "checkpoint": {"type": "object"},
        },
        "required": ["task_id", "checkpoint"],
        "additionalProperties": False,
    },
}


def _disabled_handler(tool_name: str) -> Callable[..., str]:
    def handle(args: dict[str, Any], **kwargs: Any) -> str:
        del args, kwargs
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "phase_not_ready",
                    "message": f"{tool_name} is registered but disabled until P2",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    return handle


TASK_TOOL_REGISTRATIONS: tuple[tuple[str, dict[str, Any], Callable[..., str]], ...] = (
    tuple(
        (schema["name"], schema, _disabled_handler(schema["name"]))
        for schema in (
            TASK_STATE_MANAGE_SCHEMA,
            TASK_EVENT_APPEND_SCHEMA,
            CHECKPOINT_CREATE_SCHEMA,
        )
    )
)
