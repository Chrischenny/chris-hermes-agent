"""Registration wiring for the native Hermes plugin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .context_engine import ContextHandoffEngine
from .task_tools import TASK_TOOL_REGISTRATIONS

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = PLUGIN_ROOT / "skills" / "context-handoff" / "SKILL.md"


def register(ctx: Any) -> None:
    """Register the safe P0 surface with Hermes."""
    ctx.register_context_engine(ContextHandoffEngine())

    for name, schema, handler in TASK_TOOL_REGISTRATIONS:
        ctx.register_tool(
            name=name,
            toolset="context_handoff",
            schema=schema,
            handler=handler,
            description=schema["description"],
            emoji="🔄",
        )

    ctx.register_skill("context-handoff", SKILL_PATH)
