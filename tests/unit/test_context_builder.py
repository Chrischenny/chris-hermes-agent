from __future__ import annotations

from agent.model_metadata import estimate_messages_tokens_rough

from chris_hermes_agent.context_builder import (
    RuntimeStatus,
    append_runtime_status,
    render_runtime_status,
)


def _status(**changes: object) -> RuntimeStatus:
    values: dict[str, object] = {
        "model": "gpt-test",
        "estimated_prompt_tokens": 54_000,
        "last_prompt_tokens": 52_000,
        "context_limit": 100_000,
        "policy_match": "exact:model:gpt-test",
        "handoff_threshold_tokens": 55_000,
        "emergency_threshold_tokens": 90_000,
        "active_task_id": "task-1",
        "active_segment_id": "segment-2",
        "emergency_state": "not_triggered",
    }
    values.update(changes)
    return RuntimeStatus(**values)  # type: ignore[arg-type]


def test_runtime_status_renders_usage_policy_and_active_pointer() -> None:
    content = render_runtime_status(_status())

    assert content.startswith("[Runtime Status]\n")
    assert "Model: gpt-test" in content
    assert "Prompt tokens, estimated: 54000" in content
    assert "Prompt tokens, last actual: 52000" in content
    assert "Context limit: 100000" in content
    assert "Context usage: 54.0%" in content
    assert "Handoff policy: exact:model:gpt-test" in content
    assert "Configured sweet zone starts at: 55000 tokens" in content
    assert "Emergency fallback threshold: 90000 tokens" in content
    assert "Context handoff available: true" in content
    assert "Active task: task-1" in content
    assert "Context segment: segment-2" in content
    assert "Emergency compression state: not_triggered" in content


def test_runtime_status_does_not_invent_usage_policy_or_pointer() -> None:
    content = render_runtime_status(
        _status(
            last_prompt_tokens=None,
            context_limit=0,
            policy_match=None,
            handoff_threshold_tokens=None,
            emergency_threshold_tokens=None,
            active_task_id=None,
            active_segment_id=None,
        )
    )

    assert "Prompt tokens, last actual: unavailable" in content
    assert "Context usage: unavailable" in content
    assert "Handoff policy: unmatched (observation only)" in content
    assert "Configured sweet zone starts at: not configured" in content
    assert "Emergency fallback threshold: not configured" in content
    assert "Context handoff available: false" in content
    assert "Active task: none" in content
    assert "Context segment: none" in content


def test_runtime_status_surfaces_invalid_matched_policy_diagnostics() -> None:
    content = render_runtime_status(
        _status(
            policy_match="exact:model:gpt-test",
            handoff_threshold_tokens=None,
            emergency_threshold_tokens=None,
            policy_diagnostics=("ratio_out_of_range",),
        )
    )

    assert (
        "Handoff policy: exact:model:gpt-test "
        "(invalid; observation only; errors: ratio_out_of_range)" in content
    )
    assert "Context handoff available: false" in content


def test_append_is_request_local_prefix_stable_and_estimate_is_self_consistent() -> (
    None
):
    request = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "继续任务"},
    ]
    snapshot = [dict(message) for message in request]

    selected, estimated = append_runtime_status(request, _status())

    assert request == snapshot
    assert selected is not request
    assert selected[:-1] == request
    assert all(selected[index] is request[index] for index in range(len(request)))
    assert selected[-1]["role"] == "user"
    assert str(selected[-1]["content"]).startswith("[Runtime Status]")
    assert estimate_messages_tokens_rough(selected) == estimated
