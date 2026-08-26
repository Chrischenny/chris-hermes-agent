from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from chris_hermes_agent.models import ThresholdKind
from chris_hermes_agent.policy import PolicyResolver


def _ratio_policy(
    start: float,
    *,
    emergency_threshold: float | None = None,
) -> dict[str, object]:
    policy: dict[str, object] = {
        "handoff_enabled": True,
        "sweet_zone": {"type": "ratio", "start": start},
    }
    if emergency_threshold is not None:
        policy["emergency"] = {
            "enabled": True,
            "type": "ratio",
            "threshold": emergency_threshold,
        }
    return policy


def _absolute_policy(
    start: int,
    *,
    emergency_threshold: int | None = None,
) -> dict[str, object]:
    policy: dict[str, object] = {
        "handoff_enabled": True,
        "sweet_zone": {"type": "absolute_tokens", "start": start},
    }
    if emergency_threshold is not None:
        policy["emergency"] = {
            "enabled": True,
            "type": "absolute_tokens",
            "threshold": emergency_threshold,
        }
    return policy


def test_resolves_ratio_policy_to_immutable_model() -> None:
    resolver = PolicyResolver({"model_policies": {"gpt-5.6-sol": _ratio_policy(0.55)}})

    resolution = resolver.resolve(
        model="gpt-5.6-sol",
        provider="openai-codex",
        context_limit=200_000,
    )

    assert resolution.errors == ()
    assert resolution.match_source == "exact:model:gpt-5.6-sol"
    assert resolution.policy is not None
    assert resolution.policy.sweet_zone.kind is ThresholdKind.RATIO
    assert resolution.policy.sweet_zone.value == 0.55
    assert resolution.handoff_threshold_tokens == 110_000
    assert resolution.emergency_threshold_tokens is None
    with pytest.raises(FrozenInstanceError):
        resolution.policy.handoff_enabled = False  # type: ignore[misc]


def test_resolves_absolute_token_policy_and_emergency_threshold() -> None:
    resolver = PolicyResolver(
        {
            "model_policies": {
                "glm-5.3": _absolute_policy(
                    400_000,
                    emergency_threshold=700_000,
                )
            }
        }
    )

    resolution = resolver.resolve(
        model="glm-5.3",
        provider="some-provider",
        context_limit=1_000_000,
    )

    assert resolution.errors == ()
    assert resolution.policy is not None
    assert resolution.policy.sweet_zone.kind is ThresholdKind.ABSOLUTE_TOKENS
    assert resolution.handoff_threshold_tokens == 400_000
    assert resolution.policy.emergency is not None
    assert resolution.policy.emergency.kind is ThresholdKind.ABSOLUTE_TOKENS
    assert resolution.emergency_threshold_tokens == 700_000


def test_match_priority_is_exact_then_longest_pattern_then_provider_then_default() -> (
    None
):
    resolver = PolicyResolver(
        {
            "model_policies": {
                "gpt": _ratio_policy(0.2),
                "gpt-5.6": _ratio_policy(0.3),
                "vendor/gpt-5.6-sol": _ratio_policy(0.4),
            },
            "provider_policies": {
                "openai-codex": _ratio_policy(0.5),
            },
            "default_policy": _ratio_policy(0.6),
        }
    )

    exact = resolver.resolve("vendor/gpt-5.6-sol", "openai-codex", 100_000)
    pattern = resolver.resolve("vendor/gpt-5.6-plus", "openai-codex", 100_000)
    provider = resolver.resolve("claude-sonnet", "openai-codex", 100_000)
    default = resolver.resolve("claude-sonnet", "anthropic", 100_000)

    assert exact.match_source == "exact:model:vendor/gpt-5.6-sol"
    assert exact.handoff_threshold_tokens == 40_000
    assert pattern.match_source == "pattern:model:gpt-5.6"
    assert pattern.handoff_threshold_tokens == 30_000
    assert provider.match_source == "provider:openai-codex"
    assert provider.handoff_threshold_tokens == 50_000
    assert default.match_source == "default"
    assert default.handoff_threshold_tokens == 60_000


def test_equal_length_model_patterns_fail_closed_as_ambiguous() -> None:
    resolver = PolicyResolver(
        {
            "model_policies": {
                "abc": _ratio_policy(0.4),
                "bcd": _ratio_policy(0.5),
            }
        }
    )

    resolution = resolver.resolve("abcd", "provider", 100_000)

    assert resolution.policy is None
    assert resolution.match_source is None
    assert resolution.errors[0].code == "ambiguous_model_pattern"


def test_default_observation_policy_can_disable_both_actions_without_thresholds() -> (
    None
):
    resolver = PolicyResolver(
        {
            "default_policy": {
                "handoff_enabled": False,
                "emergency_enabled": False,
            }
        }
    )

    resolution = resolver.resolve("unknown-model", "unknown-provider", 100_000)

    assert resolution.errors == ()
    assert resolution.match_source == "default"
    assert resolution.policy is not None
    assert resolution.policy.handoff_enabled is False
    assert resolution.policy.emergency_enabled is False
    assert resolution.observation_only is True
    assert resolution.handoff_threshold_tokens is None
    assert resolution.emergency_threshold_tokens is None


def test_missing_policy_is_observation_only_without_guessing_thresholds() -> None:
    resolution = PolicyResolver({}).resolve(
        "unconfigured-model",
        "unconfigured-provider",
        100_000,
    )

    assert resolution.policy is None
    assert resolution.match_source is None
    assert resolution.errors == ()
    assert resolution.observation_only is True
    assert resolution.handoff_threshold_tokens is None
    assert resolution.emergency_threshold_tokens is None


@pytest.mark.parametrize(
    ("policy", "expected_code"),
    [
        (_ratio_policy(0), "ratio_out_of_range"),
        (_ratio_policy(1), "ratio_out_of_range"),
        (_absolute_policy(0), "absolute_tokens_invalid"),
        (_absolute_policy(100_000), "threshold_at_or_above_context_limit"),
        (
            {
                "handoff_enabled": True,
                "sweet_zone": {"type": "unknown", "start": 10},
            },
            "unknown_threshold_type",
        ),
        ({"handoff_enabled": True}, "missing_sweet_zone"),
        (
            {
                "handoff_enabled": False,
                "sweet_zone": {"type": "ratio", "start": 0.5},
            },
            "disabled_policy_has_threshold",
        ),
        (
            {
                "handoff_enabled": True,
                "sweet_zone": {"type": "ratio", "start": 0.6},
                "emergency": {
                    "enabled": True,
                    "type": "ratio",
                    "threshold": 0.6,
                },
            },
            "emergency_not_above_sweet_zone",
        ),
        (
            {
                "handoff_enabled": True,
                "sweet_zone": {"type": "ratio", "start": 0.5},
                "emergency_enabled": False,
                "emergency": {
                    "enabled": True,
                    "type": "ratio",
                    "threshold": 0.9,
                },
            },
            "conflicting_emergency_enabled",
        ),
    ],
)
def test_invalid_selected_policy_fails_closed_with_diagnostic_error(
    policy: dict[str, object],
    expected_code: str,
) -> None:
    resolver = PolicyResolver({"model_policies": {"model-a": policy}})

    resolution = resolver.resolve("model-a", "provider", 100_000)

    assert resolution.policy is None
    assert resolution.match_source == "exact:model:model-a"
    assert resolution.observation_only is True
    assert resolution.handoff_threshold_tokens is None
    assert resolution.emergency_threshold_tokens is None
    assert resolution.errors[0].code == expected_code
    assert resolution.errors[0].path.startswith("handoff.model_policies.model-a")


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"unknown": {}},
        {"model_policies": []},
        {"provider_policies": {"provider": "invalid"}},
    ],
)
def test_invalid_config_shape_fails_closed(config: object) -> None:
    resolution = PolicyResolver(config).resolve("model", "provider", 100_000)

    assert resolution.policy is None
    assert resolution.observation_only is True
    assert resolution.errors


def test_non_positive_context_limit_fails_closed() -> None:
    resolver = PolicyResolver({"model_policies": {"model": _ratio_policy(0.5)}})

    resolution = resolver.resolve("model", "provider", 0)

    assert resolution.policy is None
    assert resolution.errors[0].code == "invalid_context_limit"
