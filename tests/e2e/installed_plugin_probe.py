"""Verify an installed plugin is selected through the real AIAgent chain."""

from __future__ import annotations

import json

from run_agent import AIAgent


def main() -> None:
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    engine = agent.context_compressor
    resolution = engine.policy_resolution
    engine.update_model("unmatched/model", 4_000, provider="unmatched")
    unmatched = engine.policy_resolution
    engine.update_model("test/model", 4_000, provider="test")
    restored = engine.policy_resolution
    print(
        json.dumps(
            {
                "emergency_threshold_tokens": (resolution.emergency_threshold_tokens),
                "engine": engine.name,
                "handoff_threshold_tokens": resolution.handoff_threshold_tokens,
                "match_source": resolution.match_source,
                "model_switch_failed_closed": (
                    unmatched.observation_only
                    and unmatched.handoff_threshold_tokens is None
                    and unmatched.emergency_threshold_tokens is None
                ),
                "policy_restored": (
                    restored.match_source == "exact:model:test/model"
                    and restored.handoff_threshold_tokens == 500
                    and restored.emergency_threshold_tokens == 1_000
                ),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
