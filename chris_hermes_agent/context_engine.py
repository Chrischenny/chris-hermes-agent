"""Hermes ContextEngine contract for agent-managed context handoff."""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.context_engine import ContextEngine

from .models import PolicyResolution
from .policy import PolicyResolver

logger = logging.getLogger(__name__)

HANDOFF_CONTEXT_SCHEMA: dict[str, Any] = {
    "name": "handoff_context",
    "description": (
        "Rotate the active model context to a previously persisted task "
        "checkpoint while keeping the current Hermes agent turn running. "
        "The tool is registered but intentionally disabled until P4."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "checkpoint_reference": {
                "type": "string",
                "minLength": 1,
                "description": "Reference returned by checkpoint_create.",
            },
            "handoff_reason": {
                "type": "string",
                "minLength": 1,
                "description": "Why this is a stable context handoff point.",
            },
            "target_task_id": {
                "type": "string",
                "minLength": 1,
                "description": "Task that owns the target checkpoint.",
            },
            "expected_active_task_id": {
                "type": "string",
                "minLength": 1,
                "description": "Task expected to be active before rotation.",
            },
            "expected_active_segment_id": {
                "type": "string",
                "minLength": 1,
                "description": "Active segment expected by the caller.",
            },
        },
        "required": [
            "checkpoint_reference",
            "handoff_reason",
            "target_task_id",
            "expected_active_task_id",
            "expected_active_segment_id",
        ],
        "additionalProperties": False,
    },
}


def _error(code: str, message: str) -> str:
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False,
        sort_keys=True,
    )


class ContextHandoffEngine(ContextEngine):  # type: ignore[misc]
    """P2-safe ContextEngine with policy resolution and disabled rotation.

    Task persistence is owned by the Task layer. Context selection and
    emergency behavior are implemented in later phases; until then this engine
    leaves every request and persisted Hermes transcript untouched.
    """

    emit_automatic_compaction_status = False

    def __init__(self, policy_config: object | None = None) -> None:
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0
        self.threshold_percent = 0.0
        self.current_model = ""
        self.current_provider = ""
        self._policy_resolver = PolicyResolver(
            {} if policy_config is None else policy_config
        )
        self.policy_resolution = PolicyResolution(
            model="",
            provider="",
            context_limit=0,
            match_source=None,
            policy=None,
        )

    @property
    def name(self) -> str:
        return "context-handoff"

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Maintain the token counters Hermes reads directly."""
        self.last_prompt_tokens = int(usage.get("prompt_tokens") or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_total_tokens = int(
            usage.get("total_tokens")
            or self.last_prompt_tokens + self.last_completion_tokens
        )

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        """Re-resolve user policy whenever Hermes changes model metadata."""
        del base_url, api_key, api_mode
        self.current_model = model
        self.current_provider = provider
        self.context_length = context_length
        self.policy_resolution = self._policy_resolver.resolve(
            model=model,
            provider=provider,
            context_limit=context_length,
        )
        threshold = self.policy_resolution.handoff_threshold_tokens
        self.threshold_tokens = threshold or 0
        self.threshold_percent = (
            self.threshold_tokens / context_length
            if self.threshold_tokens and context_length > 0
            else 0.0
        )
        if self.policy_resolution.errors:
            for error in self.policy_resolution.errors:
                logger.warning(
                    "Context handoff policy disabled: %s at %s: %s",
                    error.code,
                    error.path,
                    error.message,
                )

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """Disable automatic compression until an explicit policy exists."""
        del prompt_tokens
        return False

    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        memory_context: str = "",
    ) -> list[dict[str, Any]]:
        """Return the exact input list until Emergency Fallback is implemented."""
        del current_tokens, focus_topic, force, memory_context
        return messages

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [HANDOFF_CONTEXT_SCHEMA]

    def handle_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        del args, kwargs
        if name == "handoff_context":
            return _error(
                "phase_not_ready",
                "handoff_context is registered but disabled until P4",
            )
        return _error("unknown_tool", f"Unknown context engine tool: {name}")
