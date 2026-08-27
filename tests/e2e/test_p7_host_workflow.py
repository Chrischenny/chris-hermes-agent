from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from chris_hermes_agent.checkpoint_service import CheckpointService
from chris_hermes_agent.context_engine import ContextHandoffEngine
from chris_hermes_agent.migrations import initialize_database
from chris_hermes_agent.store import TaskRepository
from chris_hermes_agent.task_models import EventType
from chris_hermes_agent.task_service import TaskService


def _repository(path: Path) -> TaskRepository:
    connection = sqlite3.connect(path, check_same_thread=False)
    initialize_database(connection)
    return TaskRepository(connection)


def _checkpoint(task_goal: str, index: int) -> dict[str, object]:
    return {
        "goal": task_goal,
        "constraints": ["preserve stable prefix"],
        "current_phase": f"rotation-{index}",
        "completed": [f"boundary-{index}"],
        "current_state": [f"state-{index}"],
        "decisions": ["continue through explicit handoff"],
        "rejected_alternatives": [],
        "known_issues": [],
        "artifacts": [f"artifact-{index}"],
        "next_actions": [f"continue-{index + 1}"],
    }


def _handoff_message(
    segment_id: str,
    checkpoint_id: str,
    index: int,
) -> tuple[dict[str, str], dict[str, object]]:
    args = {
        "checkpoint_reference": checkpoint_id,
        "handoff_reason": f"stable boundary {index}",
        "target_task_id": "task-1",
        "expected_active_task_id": "task-1",
        "expected_active_segment_id": segment_id,
    }
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"handoff-{index}",
                "type": "function",
                "function": {
                    "name": "handoff_context",
                    "arguments": json.dumps(args),
                },
            }
        ],
    }
    return args, assistant


def test_ten_context_rotations_survive_engine_restart_and_preserve_prefix(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "plugin.db"
    repository = _repository(database_path)
    activation = TaskService(repository).create_task(
        "session-1", "P7", "Ten rotations", task_id="task-1"
    )
    engine = ContextHandoffEngine(repository=repository)
    engine.update_model("model-a", 100_000)
    engine.on_session_start("session-1")
    conversation: list[dict[str, object]] = [
        {"role": "user", "content": "initial durable goal"}
    ]
    stable_system = {"role": "system", "content": "SYSTEM + SOUL + SKILLS + MEMORY"}
    current_segment_id = activation.segment.context_segment_id

    for index in range(10):
        trace_call_id = f"trace-{index}"
        conversation.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": trace_call_id,
                            "type": "function",
                            "function": {"name": "terminal", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": trace_call_id,
                    "content": f"TRACE-{index}-" + "x" * 2_000,
                },
            ]
        )
        checkpoint = CheckpointService(repository).create_checkpoint(
            "task-1",
            "session-1",
            _checkpoint(activation.task.goal, index),
        )
        args, assistant = _handoff_message(
            current_segment_id,
            checkpoint.checkpoint_id,
            index,
        )
        conversation.append(assistant)
        result = json.loads(
            engine.handle_tool_call(
                "handoff_context",
                args,
                messages=conversation,
            )
        )
        assert result["ok"] is True
        current_segment_id = result["data"]["context_segment_id"]
        conversation.append(
            {
                "role": "tool",
                "name": "handoff_context",
                "tool_call_id": f"handoff-{index}",
                "content": json.dumps(result),
            }
        )
        canonical_snapshot = copy.deepcopy(conversation)
        request = [stable_system, *conversation]
        selected = engine.select_context(
            request,
            conversation_messages=conversation,
        )

        assert conversation == canonical_snapshot
        assert selected[0]["content"] == stable_system["content"]
        assert not any(f"TRACE-{prior}-" in str(selected) for prior in range(index + 1))
        assert f"continue-{index + 1}" in str(selected)

        if index == 4:
            repository.close()
            repository = _repository(database_path)
            engine = ContextHandoffEngine(repository=repository)
            engine.update_model("model-a", 100_000)
            engine.on_session_start("session-1")

    events = repository.list_events("task-1")
    assert (
        sum(event.event_type is EventType.HANDOFF_COMPLETED for event in events) == 10
    )
    state = repository.get_session_state("session-1")
    assert state is not None
    assert state.active_context_segment_id == current_segment_id

    segment_count = 0
    segment = repository.get_latest_segment("task-1")
    while segment is not None:
        segment_count += 1
        segment = (
            repository.get_segment(segment.parent_segment_id)
            if segment.parent_segment_id is not None
            else None
        )
    assert segment_count == 11


def test_real_hermes_compression_boundary_keeps_canonical_session_history(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    hermes_root = Path(
        os.environ.get("HERMES_AGENT_ROOT", Path.home() / ".hermes/hermes-agent")
    ).resolve()
    hermes_python = Path(
        os.environ.get("HERMES_AGENT_PYTHON", hermes_root / "venv/bin/python")
    )
    probe = Path(__file__).with_name("hermes_host_probe.py")
    isolated_home = tmp_path / "home"
    isolated_hermes_home = isolated_home / ".hermes"
    isolated_hermes_home.mkdir(parents=True)
    env = {
        "HOME": str(isolated_home),
        "HERMES_HOME": str(isolated_hermes_home),
        "LANG": "C.UTF-8",
        "OPENROUTER_API_KEY": "test-key",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(project_root), str(hermes_root))),
        "TZ": "UTC",
    }

    result = subprocess.run(
        [
            str(hermes_python),
            str(probe),
            str(tmp_path / "runtime"),
            "compress",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome == {
        "canonical_unchanged": True,
        "checkpoint_selected_after_emergency": True,
        "compression_count": 1,
        "emergency_replaced_by_handoff": True,
        "formal_handoff_applied": True,
        "no_child_session": True,
        "persisted_unchanged": True,
        "session_open": True,
        "stable_prefix": True,
        "summary_selected": True,
        "tool_schema_pressure_counted": True,
        "trace_removed": True,
    }
    restarted = subprocess.run(
        [
            str(hermes_python),
            str(probe),
            str(tmp_path / "runtime"),
            "restore",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    assert json.loads(restarted.stdout.strip().splitlines()[-1]) == {
        "checkpoint_selected": True,
        "emergency_not_reused": True,
        "stable_prefix": True,
        "trace_removed": True,
    }


def test_real_hermes_installer_accepts_and_loads_release_manifest(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    hermes_root = Path(
        os.environ.get("HERMES_AGENT_ROOT", Path.home() / ".hermes/hermes-agent")
    ).resolve()
    hermes_python = Path(
        os.environ.get("HERMES_AGENT_PYTHON", hermes_root / "venv/bin/python")
    )
    source = tmp_path / "source"
    shutil.copytree(
        project_root,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".coverage",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "__pycache__",
            "build",
            "dist",
            "*.egg-info",
        ),
    )
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "P7 Test"],
        ["git", "config", "user.email", "p7@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(command, cwd=source, check=True, capture_output=True, text=True)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "HERMES_HOME": str(hermes_home),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(hermes_root),
        "TZ": "UTC",
    }

    installed = subprocess.run(
        [
            str(hermes_python),
            "-m",
            "hermes_cli.main",
            "plugins",
            "install",
            source.as_uri(),
            "--enable",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert installed.returncode == 0, installed.stdout + installed.stderr
    installed_path = hermes_home / "plugins" / "chris-hermes-agent"
    doctor = subprocess.run(
        [
            str(hermes_python),
            "-m",
            "hermes_cli.main",
            "plugins",
            "doctor",
            str(installed_path),
            "--ci",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "registrations: 3 tool(s)" in doctor.stdout

    handoff_policy = json.dumps(
        {
            "model_policies": {
                "test/model": {
                    "handoff_enabled": True,
                    "sweet_zone": {
                        "type": "absolute_tokens",
                        "start": 500,
                    },
                    "emergency": {
                        "enabled": True,
                        "type": "absolute_tokens",
                        "threshold": 1_000,
                    },
                }
            }
        },
        separators=(",", ":"),
    )
    for key, value in (
        ("context.engine", "context-handoff"),
        (
            "plugins.entries.chris-hermes-agent.settings.handoff",
            handoff_policy,
        ),
    ):
        configured = subprocess.run(
            [
                str(hermes_python),
                "-m",
                "hermes_cli.main",
                "config",
                "set",
                "--force",
                key,
                value,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert configured.returncode == 0, configured.stdout + configured.stderr

    runtime_probe = Path(__file__).with_name("installed_plugin_probe.py")
    selected = subprocess.run(
        [str(hermes_python), str(runtime_probe)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**env, "OPENROUTER_API_KEY": "test-key"},
        timeout=30,
    )
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert json.loads(selected.stdout.strip().splitlines()[-1]) == {
        "emergency_threshold_tokens": 1_000,
        "engine": "context-handoff",
        "handoff_threshold_tokens": 500,
        "match_source": "exact:model:test/model",
        "model_switch_failed_closed": True,
        "policy_restored": True,
    }
