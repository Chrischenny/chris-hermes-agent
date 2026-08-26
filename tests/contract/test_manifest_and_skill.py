from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "plugin.yaml"
SKILL_PATH = PROJECT_ROOT / "skills" / "context-handoff" / "SKILL.md"


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
    assert manifest["version"] == "0.1.0"
    assert manifest["skill_namespace"] == "chris-hermes-agent"


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
    assert frontmatter["version"] == "0.1.0"
    assert "Context" in frontmatter["description"]
