from __future__ import annotations

import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "plugin.yaml"
SKILL_PATH = PROJECT_ROOT / "skills" / "context-handoff" / "SKILL.md"
REFERENCE_PATHS = {
    name: SKILL_PATH.parent / "references" / name
    for name in (
        "checkpoint-template.md",
        "new-task-detection.md",
        "task-state-rules.md",
    )
}
SOUL_PATH = PROJECT_ROOT / "soul" / "SOUL-snippet.md"


def _read_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, _ = text.split("---", 2)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed


def test_manifest_declares_native_standalone_plugin() -> None:
    assert MANIFEST_PATH.is_file()

    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["name"] == "chris-hermes-agent"
    assert manifest["kind"] == "standalone"
    assert manifest["version"] == "0.7.0"
    assert manifest["manifest_version"] == 1
    assert manifest["api_version"] == 1
    assert manifest["skill_namespace"] == "chris-hermes-agent"
    assert manifest["config_schema"]["handoff"]["type"] == "dict"
    assert manifest["config_schema"]["handoff"]["required"] is False


def test_manifest_declares_tools_registered_by_plugin_context() -> None:
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["provides_tools"] == [
        "task_state_manage",
        "task_event_append",
        "checkpoint_create",
    ]


def test_bundled_skill_has_valid_identity() -> None:
    assert SKILL_PATH.is_file()

    frontmatter = _read_frontmatter(SKILL_PATH)

    assert frontmatter["name"] == "context-handoff"
    assert frontmatter["version"] == "0.4.0"
    assert "Context" in frontmatter["description"]


def test_bundled_skill_routes_every_p5_workflow_reference() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")

    for name, path in REFERENCE_PATHS.items():
        assert path.is_file()
        assert f"references/{name}" in skill

    for tool_name in (
        "task_state_manage",
        "task_event_append",
        "checkpoint_create",
        "handoff_context",
    ):
        assert tool_name in skill


def test_task_workflow_references_define_classification_and_isolation_contracts() -> (
    None
):
    detection = REFERENCE_PATHS["new-task-detection.md"].read_text(encoding="utf-8")
    state_rules = REFERENCE_PATHS["task-state-rules.md"].read_text(encoding="utf-8")

    for classification in (
        "continuation",
        "subtask",
        "new_task",
        "ambiguous",
    ):
        assert classification in detection
    assert "before any state-changing tool call" in detection

    for inheritable_field in ("constraints", "decisions", "artifacts"):
        assert inheritable_field in state_rules
    assert "Tool Trace" in state_rules
    assert "context_rotation_required" in state_rules
    assert "handoff_applied" in state_rules


def test_checkpoint_reference_covers_runtime_required_fields() -> None:
    checkpoint_reference = REFERENCE_PATHS["checkpoint-template.md"].read_text(
        encoding="utf-8"
    )

    for field in (
        "goal",
        "constraints",
        "current_phase",
        "completed",
        "current_state",
        "decisions",
        "rejected_alternatives",
        "known_issues",
        "artifacts",
        "next_actions",
    ):
        assert field in checkpoint_reference
    assert "checksum" in checkpoint_reference.lower()


def test_soul_migration_uses_runtime_policy_without_fixed_thresholds() -> None:
    soul = SOUL_PATH.read_text(encoding="utf-8")

    assert "Runtime Status" in soul
    assert "chris-hermes-agent:context-handoff" in soul
    assert "handoff_context" in soul
    assert "Next Actions" in soul
    assert re.search(r"\b(?:gpt|claude|gemini|glm)-", soul, re.IGNORECASE) is None
    assert re.search(r"\b\d+(?:\.\d+)?\s*(?:%|tokens?)\b", soul, re.IGNORECASE) is None


def test_soul_requires_normal_handoff_after_completed_emergency() -> None:
    soul = SOUL_PATH.read_text(encoding="utf-8")

    assert "Emergency Fallback" in soul
    assert "completed" in soul
    assert "Checkpoint" in soul
    assert "normal explicit Handoff" in soul
    assert "do not retry" in soul
