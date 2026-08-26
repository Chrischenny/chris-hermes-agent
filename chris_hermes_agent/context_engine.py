"""Hermes ContextEngine contract for agent-managed context handoff."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from agent.context_engine import ContextEngine

from .context_builder import RuntimeStatus, append_runtime_status
from .models import PolicyResolution
from .policy import PolicyResolver
from .store import TaskRepository
from .token_usage import ProviderTokenUsage

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
    """ContextEngine with P3 observation and disabled context rotation.

    Task persistence remains owned by the Task layer. P3 appends request-local
    status while context rotation and emergency behavior stay disabled.
    """

    emit_automatic_compaction_status = False

    def __init__(
        self,
        policy_config: object | None = None,
        *,
        repository: TaskRepository | None = None,
        repository_provider: Callable[[], TaskRepository] | None = None,
    ) -> None:
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0
        self.threshold_percent = 0.0
        self.current_model = ""
        self.current_provider = ""
        self.estimated_prompt_tokens = 0
        self.last_usage = ProviderTokenUsage()
        self._awaiting_usage = False
        self._session_id: str | None = None
        self._repository = repository
        self._repository_provider = repository_provider
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
        """Maintain host counters and explicit real-usage availability."""
        self.last_usage = ProviderTokenUsage.from_mapping(usage)
        self._awaiting_usage = False
        self.last_prompt_tokens = self.last_usage.prompt_tokens or 0
        self.last_completion_tokens = self.last_usage.completion_tokens or 0
        self.last_total_tokens = self.last_usage.total_tokens or 0

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
        if self.current_model and (
            model != self.current_model or provider != self.current_provider
        ):
            self._clear_usage()
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

    def select_context(
        self,
        request_messages: list[dict[str, Any]],
        *,
        conversation_messages: list[dict[str, Any]] | None = None,
        incoming_message: dict[str, Any] | None = None,
        budget_tokens: int = 0,
    ) -> list[dict[str, Any]]:
        """Append one ephemeral status while preserving the stable prefix."""
        del conversation_messages, incoming_message, budget_tokens
        if self._awaiting_usage:
            self._clear_usage()
        active_task_id, active_segment_id = self._active_pointer()
        resolution = self.policy_resolution
        selected, estimated = append_runtime_status(
            request_messages,
            RuntimeStatus(
                model=self.current_model or "unknown",
                estimated_prompt_tokens=0,
                last_prompt_tokens=self.last_usage.prompt_tokens,
                context_limit=self.context_length,
                policy_match=resolution.match_source,
                handoff_threshold_tokens=resolution.handoff_threshold_tokens,
                emergency_threshold_tokens=resolution.emergency_threshold_tokens,
                active_task_id=active_task_id,
                active_segment_id=active_segment_id,
                policy_diagnostics=tuple(error.code for error in resolution.errors),
            ),
        )
        self.estimated_prompt_tokens = estimated
        self._awaiting_usage = True
        return selected

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """Attach subsequent request observations to one Hermes Session."""
        del kwargs
        if session_id != self._session_id:
            self._clear_usage()
        self._session_id = session_id

    def on_session_reset(self) -> None:
        """Clear request observations and detach the previous Session pointer."""
        super().on_session_reset()
        self._session_id = None
        self.estimated_prompt_tokens = 0
        self.last_usage = ProviderTokenUsage()
        self._awaiting_usage = False

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

    def _clear_usage(self) -> None:
        self.last_usage = ProviderTokenUsage()
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.estimated_prompt_tokens = 0
        self._awaiting_usage = False

    def _active_pointer(self) -> tuple[str | None, str | None]:
        if self._session_id is None:
            return None, None
        try:
            repository = self._repository
            if repository is None:
                if self._repository_provider is None:
                    from .task_tools import get_default_repository

                    self._repository_provider = get_default_repository
                repository = self._repository_provider()
                self._repository = repository
            state = repository.get_session_state(self._session_id)
        except sqlite3.Error as exc:
            logger.warning("Unable to read active Context pointer: %s", exc)
            return None, None
        if state is None:
            return None, None
        return state.active_task_id, state.active_context_segment_id
