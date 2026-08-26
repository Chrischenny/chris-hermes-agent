from __future__ import annotations

import importlib.util
import json
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.context_engine import ContextEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    toolset: str
    schema: dict[str, Any]
    handler: Callable[..., Any]


class RecordingPluginContext:
    def __init__(self, handoff_config: object | None = None) -> None:
        self.engines: list[ContextEngine] = []
        self.tools: list[RegisteredTool] = []
        self.skills: list[tuple[str, Path]] = []
        self.handoff_config = handoff_config
        self.config_reads: list[tuple[str, object]] = []

    def get_config(self, key: str, default: object = None) -> object:
        self.config_reads.append((key, default))
        if key == "handoff" and self.handoff_config is not None:
            return self.handoff_config
        return default

    def register_context_engine(self, engine: ContextEngine) -> None:
        self.engines.append(engine)

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        **_: Any,
    ) -> None:
        self.tools.append(RegisteredTool(name, toolset, schema, handler))

    def register_skill(self, name: str, path: Path, **_: Any) -> None:
        self.skills.append((name, path))


def _load_native_entrypoint():
    entrypoint = PROJECT_ROOT / "__init__.py"
    assert entrypoint.is_file()
    parent_name = "hermes_plugins"
    module_name = f"{parent_name}.chris_hermes_agent_contract"
    if parent_name not in sys.modules:
        parent = types.ModuleType(parent_name)
        parent.__path__ = []
        parent.__package__ = parent_name
        sys.modules[parent_name] = parent
    for loaded_name in [
        name
        for name in sys.modules
        if name == module_name or name.startswith(f"{module_name}.")
    ]:
        del sys.modules[loaded_name]
    spec = importlib.util.spec_from_file_location(
        module_name,
        entrypoint,
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(PROJECT_ROOT)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_native_entrypoint_registers_engine_tools_and_skill() -> None:
    module = _load_native_entrypoint()
    context = RecordingPluginContext()

    module.register(context)

    assert context.config_reads == [("handoff", {})]
    assert len(context.engines) == 1
    assert context.engines[0].name == "context-handoff"
    assert [tool.name for tool in context.tools] == [
        "task_state_manage",
        "task_event_append",
        "checkpoint_create",
    ]
    assert [tool.toolset for tool in context.tools] == [
        "context_handoff",
        "context_handoff",
        "context_handoff",
    ]
    assert context.skills == [
        (
            "context-handoff",
            PROJECT_ROOT / "skills" / "context-handoff" / "SKILL.md",
        )
    ]


def test_registered_task_schemas_are_strict_and_self_named() -> None:
    module = _load_native_entrypoint()
    context = RecordingPluginContext()
    module.register(context)

    for tool in context.tools:
        assert tool.schema["name"] == tool.name
        assert tool.schema["parameters"]["type"] == "object"
        assert tool.schema["parameters"]["additionalProperties"] is False


def test_p0_task_handlers_fail_closed_with_structured_json() -> None:
    module = _load_native_entrypoint()
    context = RecordingPluginContext()
    module.register(context)

    for tool in context.tools:
        result = json.loads(tool.handler({}))
        assert result == {
            "ok": False,
            "error": {
                "code": "phase_not_ready",
                "message": f"{tool.name} is registered but disabled until P2",
            },
        }


def test_registration_injects_private_handoff_config_into_engine() -> None:
    module = _load_native_entrypoint()
    context = RecordingPluginContext(
        {
            "model_policies": {
                "configured-model": {
                    "handoff_enabled": True,
                    "sweet_zone": {"type": "ratio", "start": 0.42},
                }
            }
        }
    )

    module.register(context)
    engine = context.engines[0]
    engine.update_model(
        "configured-model",
        100_000,
        provider="configured-provider",
    )

    assert engine.policy_resolution.match_source == "exact:model:configured-model"
    assert engine.policy_resolution.handoff_threshold_tokens == 42_000
