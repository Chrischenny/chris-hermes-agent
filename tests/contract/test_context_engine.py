from __future__ import annotations

import json

from agent.context_engine import ContextEngine


def _engine():
    from chris_hermes_agent.context_engine import ContextHandoffEngine

    return ContextHandoffEngine()


def test_engine_satisfies_hermes_abc() -> None:
    engine = _engine()

    assert isinstance(engine, ContextEngine)
    assert engine.name == "context-handoff"


def test_engine_starts_with_isolated_zeroed_counters() -> None:
    first = _engine()
    second = _engine()
    first.last_prompt_tokens = 123

    assert second.last_prompt_tokens == 0
    assert second.last_completion_tokens == 0
    assert second.last_total_tokens == 0
    assert second.compression_count == 0


def test_engine_tracks_provider_usage_required_by_host() -> None:
    engine = _engine()

    engine.update_from_response(
        {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
        }
    )

    assert engine.last_prompt_tokens == 120
    assert engine.last_completion_tokens == 30
    assert engine.last_total_tokens == 150


def test_p0_engine_never_requests_compression() -> None:
    engine = _engine()

    assert engine.should_compress() is False
    assert engine.should_compress(10**9) is False
    assert engine.should_compress_preflight([]) is False


def test_p0_compress_is_a_strict_passthrough() -> None:
    engine = _engine()
    messages = [{"role": "user", "content": "keep me"}]

    result = engine.compress(
        messages,
        current_tokens=999_999,
        focus_topic="ignored in P0",
        force=True,
        memory_context="ignored in P0",
    )

    assert result is messages


def test_p0_context_selection_is_disabled() -> None:
    engine = _engine()
    request = [{"role": "user", "content": "unchanged"}]

    selected = engine.select_context(
        request,
        conversation_messages=list(request),
        incoming_message=request[0],
        budget_tokens=200_000,
    )

    assert selected is None


def test_engine_exposes_only_handoff_context_tool() -> None:
    engine = _engine()

    schemas = engine.get_tool_schemas()

    assert [schema["name"] for schema in schemas] == ["handoff_context"]
    assert schemas[0]["parameters"]["additionalProperties"] is False
    assert set(schemas[0]["parameters"]["required"]) == {
        "checkpoint_reference",
        "handoff_reason",
        "expected_task_id",
        "expected_segment_id",
    }


def test_handoff_tool_is_safely_disabled_during_p0() -> None:
    engine = _engine()

    result = json.loads(
        engine.handle_tool_call(
            "handoff_context",
            {
                "checkpoint_reference": "checkpoint-1",
                "handoff_reason": "test",
                "expected_task_id": "task-1",
                "expected_segment_id": "segment-1",
            },
        )
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "phase_not_ready",
            "message": "handoff_context is registered but disabled until P4",
        },
    }


def test_engine_rejects_unknown_context_tool() -> None:
    engine = _engine()

    result = json.loads(engine.handle_tool_call("unknown", {}))

    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_tool"
