"""Provider-reported token usage normalized without invented measurements."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True, slots=True)
class ProviderTokenUsage:
    """The latest real provider measurement, or explicit unavailability."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None

    @classmethod
    def from_mapping(cls, usage: Mapping[str, object]) -> ProviderTokenUsage:
        prompt_tokens = _token_count(usage.get("prompt_tokens"))
        if prompt_tokens is None:
            prompt_tokens = _token_count(usage.get("input_tokens"))

        completion_tokens = _token_count(usage.get("completion_tokens"))
        if completion_tokens is None:
            completion_tokens = _token_count(usage.get("output_tokens"))

        total_tokens = _token_count(usage.get("total_tokens"))
        if (
            total_tokens is None
            and prompt_tokens is not None
            and completion_tokens is not None
        ):
            total_tokens = prompt_tokens + completion_tokens

        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=_token_count(usage.get("cache_read_tokens")),
            cache_write_tokens=_token_count(usage.get("cache_write_tokens")),
            reasoning_tokens=_token_count(usage.get("reasoning_tokens")),
        )

    @property
    def available(self) -> bool:
        """Whether the provider supplied a real prompt/input token count."""
        return self.prompt_tokens is not None
