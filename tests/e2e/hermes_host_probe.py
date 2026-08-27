"""Run the P7 Host boundary inside Hermes' own dependency environment."""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import sys
from pathlib import Path

from agent.conversation_compression import compress_context
from agent.conversation_loop import _apply_context_engine_selection
from agent.model_metadata import (
    estimate_messages_tokens_rough,
    estimate_request_tokens_rough,
)
from hermes_state import SessionDB
from run_agent import AIAgent

from chris_hermes_agent.checkpoint_service import CheckpointService
from chris_hermes_agent.context_engine import ContextHandoffEngine
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_service import TaskService


class HostCompressionDelegate:
    _last_compress_aborted = False

    def compress(self, messages: list[dict], **_: object) -> list[dict]:
        system = [message for message in messages if message.get("role") == "system"]
        return [*system[:1], {"role": "user", "content": "HOST EMERGENCY STATE"}]


def _engine(
    repository: TaskRepository,
    runtime_directory: Path,
) -> ContextHandoffEngine:
    engine = ContextHandoffEngine(
        {
            "model_policies": {
                "test/model": {
                    "handoff_enabled": True,
                    "sweet_zone": {"type": "absolute_tokens", "start": 500},
                    "emergency": {
                        "enabled": True,
                        "type": "absolute_tokens",
                        "threshold": 4_500,
                    },
                }
            }
        },
        repository=repository,
        compression_delegate=HostCompressionDelegate(),
        emergency_archive_directory=runtime_directory / "archives",
    )
    engine.update_model("test/model", 10_000, provider="test")
    engine.on_session_start("host-session")
    return engine


def _agent(session_db: SessionDB, engine: ContextHandoffEngine) -> AIAgent:
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        session_db=session_db,
        session_id="host-session",
        skip_context_files=True,
        skip_memory=True,
    )
    agent.context_compressor = engine
    agent.tools = [
        {
            "type": "function",
            "function": {
                "name": "large_host_tool",
                "description": "schema-pressure-" + "x" * 12_000,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    agent.compression_in_place = True
    agent._compression_feasibility_checked = True
    return agent


def main(runtime_directory: Path, mode: str) -> None:
    runtime_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(
        runtime_directory / "plugin.db", check_same_thread=False
    )
    initialize_database(connection)
    repository = TaskRepository(connection)
    session_db = SessionDB(db_path=runtime_directory / "state.db")
    canonical = (
        [
            {"role": "user", "content": "perform the long task"},
            {"role": "assistant", "content": "TRACE-HOST-" + "x" * 8_000},
        ]
        if mode == "compress"
        else session_db.get_messages_as_conversation("host-session")
    )
    if mode == "compress":
        activation = TaskService(repository).create_task(
            "host-session", "P7", "Host boundary", task_id="task-1"
        )
        session_db.create_session("host-session", "cli", model="test/model")
        for message in canonical:
            session_db.append_message(
                "host-session", message["role"], message["content"]
            )
    engine = _engine(repository, runtime_directory)
    agent = _agent(session_db, engine)
    canonical_snapshot = copy.deepcopy(canonical)
    api_messages = [{"role": "system", "content": "STABLE SYSTEM"}, *canonical]
    if mode == "restore":
        restored = _apply_context_engine_selection(
            agent,
            api_messages,
            canonical,
            canonical[0],
            logger=logging.getLogger(__name__),
        )
        print(
            json.dumps(
                {
                    "checkpoint_selected": "continue after emergency" in str(restored),
                    "emergency_not_reused": "HOST EMERGENCY STATE" not in str(restored),
                    "stable_prefix": restored[0]["content"] == "STABLE SYSTEM",
                    "trace_removed": "TRACE-HOST-" not in str(restored),
                },
                sort_keys=True,
            )
        )
        return
    selected = _apply_context_engine_selection(
        agent,
        api_messages,
        canonical,
        canonical[0],
        logger=logging.getLogger(__name__),
    )
    if "TRACE-HOST-" not in str(selected):
        raise AssertionError("Initial Host selection unexpectedly removed trace.")
    message_tokens = estimate_messages_tokens_rough(selected)
    request_pressure_tokens = estimate_request_tokens_rough(
        selected,
        tools=agent.tools,
    )
    if not engine.should_compress(request_pressure_tokens):
        raise AssertionError("Emergency policy did not trigger in Host runtime.")

    returned, _ = compress_context(
        agent,
        canonical,
        "STABLE SYSTEM",
        approx_tokens=request_pressure_tokens,
    )
    after = _apply_context_engine_selection(
        agent,
        api_messages,
        canonical,
        canonical[0],
        logger=logging.getLogger(__name__),
    )
    child_count = session_db._conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE parent_session_id = ?",
        ("host-session",),
    ).fetchone()[0]
    persisted = session_db.get_messages_as_conversation("host-session")
    persisted_core = [
        {"role": message.get("role"), "content": message.get("content")}
        for message in persisted
    ]
    outcome = {
        "canonical_unchanged": returned is canonical
        and canonical == canonical_snapshot,
        "compression_count": engine.compression_count,
        "no_child_session": child_count == 0,
        "persisted_unchanged": persisted_core == canonical_snapshot,
        "session_open": session_db.get_session("host-session")["end_reason"] is None,
        "stable_prefix": after[0]["content"] == "STABLE SYSTEM",
        "summary_selected": "HOST EMERGENCY STATE" in str(after),
        "tool_schema_pressure_counted": request_pressure_tokens > message_tokens,
        "trace_removed": "TRACE-HOST-" not in str(after),
    }

    checkpoint = CheckpointService(repository).create_checkpoint(
        "task-1",
        "host-session",
        {
            "goal": "Host boundary",
            "constraints": ["preserve canonical history"],
            "current_phase": "recover-after-emergency",
            "completed": ["Emergency recovery verified"],
            "current_state": ["ready for ordinary rotation"],
            "decisions": ["use the normal Handoff workflow"],
            "rejected_alternatives": ["retry compression"],
            "known_issues": [],
            "artifacts": ["Emergency archive"],
            "next_actions": ["continue after emergency"],
        },
    )
    handoff_args = {
        "checkpoint_reference": checkpoint.checkpoint_id,
        "handoff_reason": "formal recovery after Emergency",
        "target_task_id": "task-1",
        "expected_active_task_id": "task-1",
        "expected_active_segment_id": activation.segment.context_segment_id,
    }
    handoff_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "formal-handoff",
                "type": "function",
                "function": {
                    "name": "handoff_context",
                    "arguments": json.dumps(handoff_args),
                },
            }
        ],
    }
    canonical.append(handoff_call)
    session_db.append_message(
        "host-session",
        "assistant",
        "",
        tool_calls=handoff_call["tool_calls"],
    )
    handoff_result = json.loads(
        engine.handle_tool_call(
            "handoff_context",
            handoff_args,
            messages=canonical,
        )
    )
    handoff_tool_result = {
        "role": "tool",
        "name": "handoff_context",
        "tool_call_id": "formal-handoff",
        "content": json.dumps(handoff_result),
    }
    canonical.append(handoff_tool_result)
    session_db.append_message(
        "host-session",
        "tool",
        handoff_tool_result["content"],
        tool_name="handoff_context",
        tool_call_id="formal-handoff",
    )
    after_handoff = _apply_context_engine_selection(
        agent,
        [{"role": "system", "content": "STABLE SYSTEM"}, *canonical],
        canonical,
        canonical[0],
        logger=logging.getLogger(__name__),
    )
    outcome.update(
        {
            "checkpoint_selected_after_emergency": (
                "continue after emergency" in str(after_handoff)
            ),
            "emergency_replaced_by_handoff": (
                "HOST EMERGENCY STATE" not in str(after_handoff)
            ),
            "formal_handoff_applied": (
                handoff_result.get("ok") is True
                and handoff_result.get("data", {}).get("handoff_applied") is True
            ),
        }
    )
    print(json.dumps(outcome, sort_keys=True))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), sys.argv[2])
