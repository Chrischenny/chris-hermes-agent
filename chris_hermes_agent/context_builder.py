"""Build the request-local Runtime Status without changing stable history."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agent.model_metadata import estimate_messages_tokens_rough

RUNTIME_STATUS_HEADER = "[Runtime Status]\n"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    model: str
    estimated_prompt_tokens: int
    last_prompt_tokens: int | None
    context_limit: int
    policy_match: str | None
    handoff_threshold_tokens: int | None
    emergency_threshold_tokens: int | None
    active_task_id: str | None
    active_segment_id: str | None
    policy_diagnostics: tuple[str, ...] = ()


def _tokens(value: int | None, *, suffix: bool = False) -> str:
    if value is None:
        return "not configured" if suffix else "unavailable"
    return f"{value} tokens" if suffix else str(value)


def render_runtime_status(status: RuntimeStatus) -> str:
    """Render facts and configured policy while keeping unknowns explicit."""
    if status.context_limit > 0:
        usage = f"{status.estimated_prompt_tokens / status.context_limit:.1%}"
    else:
        usage = "unavailable"

    if status.policy_diagnostics:
        diagnostics = ", ".join(status.policy_diagnostics)
        if status.policy_match is None:
            policy = f"invalid (observation only; errors: {diagnostics})"
        else:
            policy = (
                f"{status.policy_match} "
                f"(invalid; observation only; errors: {diagnostics})"
            )
    elif status.policy_match is not None:
        policy = status.policy_match
    else:
        policy = "unmatched (observation only)"

    return "\n".join(
        (
            "[Runtime Status]",
            "",
            f"Model: {status.model}",
            f"Prompt tokens, estimated: {status.estimated_prompt_tokens}",
            f"Prompt tokens, last actual: {_tokens(status.last_prompt_tokens)}",
            f"Context limit: {status.context_limit}",
            f"Context usage: {usage}",
            f"Handoff policy: {policy}",
            "Configured sweet zone starts at: "
            f"{_tokens(status.handoff_threshold_tokens, suffix=True)}",
            "Emergency fallback threshold: "
            f"{_tokens(status.emergency_threshold_tokens, suffix=True)}",
            "Context handoff available: "
            f"{'true' if status.handoff_threshold_tokens is not None else 'false'}",
            f"Active task: {status.active_task_id or 'none'}",
            f"Context segment: {status.active_segment_id or 'none'}",
        )
    )


def append_runtime_status(
    request_messages: list[dict[str, Any]],
    status: RuntimeStatus,
) -> tuple[list[dict[str, Any]], int]:
    """Append one status message and return its self-consistent rough estimate."""
    base_messages = request_messages
    if request_messages and _is_runtime_status_message(request_messages[-1]):
        base_messages = request_messages[:-1]
    estimated = estimate_messages_tokens_rough(base_messages)
    selected: list[dict[str, Any]] = list(base_messages)

    for _ in range(32):
        current = replace(status, estimated_prompt_tokens=estimated)
        selected = [
            *base_messages,
            {"role": "user", "content": render_runtime_status(current)},
        ]
        next_estimate = estimate_messages_tokens_rough(selected)
        if next_estimate == estimated:
            return selected, estimated
        estimated = next_estimate

    current = replace(status, estimated_prompt_tokens=estimated)
    selected = [
        *base_messages,
        {"role": "user", "content": render_runtime_status(current)},
    ]
    return selected, estimate_messages_tokens_rough(selected)


def _is_runtime_status_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, str)
        and content.startswith(RUNTIME_STATUS_HEADER)
        and "\nPrompt tokens, estimated: " in content
        and "\nContext segment: " in content
    )
