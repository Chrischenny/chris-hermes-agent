"""Build the request-local Runtime Status without changing stable history."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agent.model_metadata import estimate_messages_tokens_rough

from .task_models import CheckpointRecord, TaskRecord

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
    emergency_state: str = "not_triggered"
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
            f"Emergency compression state: {status.emergency_state}",
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


def _list_section(title: str, values: tuple[str, ...]) -> tuple[str, ...]:
    items = values or ("none",)
    return (f"{title}:", *(f"- {item}" for item in items))


def render_handoff_bootstrap(
    task: TaskRecord,
    checkpoint: CheckpointRecord,
) -> str:
    """Render the durable recovery state used by every rotated request."""
    return "\n".join(
        (
            "[Context Handoff Bootstrap]",
            "",
            f"Task: {task.task_id} — {task.title}",
            f"Task status: {task.status.value}",
            f"Checkpoint: {checkpoint.checkpoint_id}",
            f"Goal: {checkpoint.goal}",
            f"Current phase: {checkpoint.current_phase}",
            *_list_section("Constraints", checkpoint.constraints),
            *_list_section("Completed", checkpoint.completed),
            *_list_section("Current state", checkpoint.current_state),
            *_list_section("Decisions", checkpoint.decisions),
            *_list_section("Rejected alternatives", checkpoint.rejected_alternatives),
            *_list_section("Known issues", checkpoint.known_issues),
            *_list_section("Artifacts", checkpoint.artifacts),
            *_list_section("Next actions", checkpoint.next_actions),
        )
    )


def build_handoff_context(
    request_messages: list[dict[str, Any]],
    *,
    conversation_messages: list[dict[str, Any]],
    start_message_index: int,
    task: TaskRecord | None,
    checkpoint: CheckpointRecord | None,
    diagnostic: str | None = None,
) -> list[dict[str, Any]]:
    """Keep the Hermes stable head, durable bootstrap, and new Segment tail."""
    if not 0 <= start_message_index <= len(conversation_messages):
        raise ValueError("start_message_index is outside conversation history.")

    prefix_length = len(request_messages) - len(conversation_messages)
    if prefix_length < 0:
        raise ValueError(
            "request_messages cannot be shorter than conversation history."
        )
    stable_head = request_messages[:prefix_length]
    segment_tail = request_messages[prefix_length + start_message_index :]

    if diagnostic is None and task is not None and checkpoint is not None:
        bootstrap_content = render_handoff_bootstrap(task, checkpoint)
    else:
        reason = diagnostic or "task_or_checkpoint_missing"
        bootstrap_content = "\n".join(
            (
                "[Context Handoff Bootstrap Error]",
                "",
                f"Recovery state unavailable: {reason}",
                "Do not infer missing checkpoint state or reintroduce archived trace.",
            )
        )

    return [
        *stable_head,
        {"role": "user", "content": bootstrap_content},
        *segment_tail,
    ]
