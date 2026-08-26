from __future__ import annotations

from chris_hermes_agent.token_usage import ProviderTokenUsage


def test_normalizes_legacy_and_canonical_provider_usage() -> None:
    usage = ProviderTokenUsage.from_mapping(
        {
            "prompt_tokens": 1_200,
            "completion_tokens": 80,
            "total_tokens": 1_280,
            "input_tokens": 1_200,
            "output_tokens": 80,
            "cache_read_tokens": 1_024,
            "cache_write_tokens": 32,
            "reasoning_tokens": 16,
        }
    )

    assert usage.prompt_tokens == 1_200
    assert usage.completion_tokens == 80
    assert usage.total_tokens == 1_280
    assert usage.cache_read_tokens == 1_024
    assert usage.cache_write_tokens == 32
    assert usage.reasoning_tokens == 16
    assert usage.available is True


def test_canonical_input_and_output_are_compatible_fallbacks() -> None:
    usage = ProviderTokenUsage.from_mapping({"input_tokens": 900, "output_tokens": 75})

    assert usage.prompt_tokens == 900
    assert usage.completion_tokens == 75
    assert usage.total_tokens == 975
    assert usage.available is True


def test_missing_or_invalid_usage_stays_unavailable() -> None:
    empty = ProviderTokenUsage.from_mapping({})
    invalid = ProviderTokenUsage.from_mapping(
        {
            "prompt_tokens": True,
            "completion_tokens": -1,
            "total_tokens": "unknown",
        }
    )

    assert empty.prompt_tokens is None
    assert empty.total_tokens is None
    assert empty.available is False
    assert invalid.prompt_tokens is None
    assert invalid.completion_tokens is None
    assert invalid.total_tokens is None
    assert invalid.available is False
