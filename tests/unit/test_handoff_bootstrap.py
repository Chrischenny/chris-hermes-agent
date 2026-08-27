from __future__ import annotations

from chris_hermes_agent.context_builder import (
    build_handoff_context,
    render_handoff_bootstrap,
)
from chris_hermes_agent.task_models import CheckpointRecord, TaskRecord, TaskStatus


def _task() -> TaskRecord:
    return TaskRecord(
        task_id="task-1",
        parent_task_id=None,
        title="P4 Rotation",
        created_session_id="session-1",
        last_session_id="session-1",
        goal="Continue without old trace",
        constraints=("preserve system",),
        current_phase="P4",
        completed=("P3",),
        in_progress=("rotation",),
        known_issues=(),
        next_actions=("continue",),
        decisions=("keep tool pair",),
        artifacts=("context_engine.py",),
        status=TaskStatus.ACTIVE,
        search_aliases=(),
        tags=("Hermes",),
        paused_at=None,
        last_resumed_at=None,
        resume_count=0,
        created_at="2026-08-27T00:00:00+00:00",
        updated_at="2026-08-27T00:00:00+00:00",
        version=0,
    )


def _checkpoint() -> CheckpointRecord:
    draft = CheckpointRecord(
        checkpoint_id="checkpoint-1",
        task_id="task-1",
        session_id="session-1",
        goal="Continue without old trace",
        constraints=("preserve system",),
        current_phase="P4",
        completed=("P3",),
        current_state=("ready",),
        decisions=("keep tool pair",),
        rejected_alternatives=("new Hermes session",),
        known_issues=(),
        artifacts=("context_engine.py",),
        next_actions=("continue",),
        content_checksum="",
        created_at="2026-08-27T00:00:00+00:00",
    )
    return CheckpointRecord(
        **{
            **draft.to_json_dict(),
            "content_checksum": draft.calculated_checksum(),
        }
    )


def test_bootstrap_renders_complete_task_and_checkpoint_state() -> None:
    content = render_handoff_bootstrap(_task(), _checkpoint())

    assert content.startswith("[Context Handoff Bootstrap]\n")
    assert "Task: task-1 — P4 Rotation" in content
    assert "Goal: Continue without old trace" in content
    assert "Current phase: P4" in content
    assert "Constraints:\n- preserve system" in content
    assert "Completed:\n- P3" in content
    assert "Current state:\n- ready" in content
    assert "Decisions:\n- keep tool pair" in content
    assert "Rejected alternatives:\n- new Hermes session" in content
    assert "Artifacts:\n- context_engine.py" in content
    assert "Next actions:\n- continue" in content


def test_handoff_context_preserves_stable_head_and_only_new_segment_tail() -> None:
    conversation = [
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "old-call",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "old-call", "content": "OLD TRACE"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "handoff-call",
                    "type": "function",
                    "function": {"name": "handoff_context", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "handoff-call",
            "content": '{"handoff_applied":true}',
        },
    ]
    request = [
        {"role": "system", "content": "stable system / soul / skills / memory"},
        {"role": "assistant", "content": "stable prefill"},
        *conversation,
    ]

    selected = build_handoff_context(
        request,
        conversation_messages=conversation,
        start_message_index=3,
        task=_task(),
        checkpoint=_checkpoint(),
    )

    assert selected[0] is request[0]
    assert selected[1] is request[1]
    assert selected[2]["role"] == "user"
    assert str(selected[2]["content"]).startswith("[Context Handoff Bootstrap]")
    assert selected[3:] == conversation[3:]
    assert "OLD TRACE" not in str(selected)


def test_bootstrap_error_is_diagnostic_without_reintroducing_old_trace() -> None:
    conversation = [
        {"role": "user", "content": "OLD TRACE"},
        {"role": "assistant", "content": "handoff trigger"},
    ]
    request = [{"role": "system", "content": "stable"}, *conversation]

    selected = build_handoff_context(
        request,
        conversation_messages=conversation,
        start_message_index=1,
        task=None,
        checkpoint=None,
        diagnostic="checkpoint_corrupt",
    )

    assert selected[0] is request[0]
    assert "[Context Handoff Bootstrap Error]" in str(selected[1]["content"])
    assert "checkpoint_corrupt" in str(selected[1]["content"])
    assert "OLD TRACE" not in str(selected)
