"""Immutable data models for model-specific handoff policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import PolicyError


class ThresholdKind(StrEnum):
    """Supported user-configured threshold representations."""

    RATIO = "ratio"
    ABSOLUTE_TOKENS = "absolute_tokens"


@dataclass(frozen=True, slots=True)
class Threshold:
    """A validated threshold value with no implicit default."""

    kind: ThresholdKind
    value: float | int

    def to_tokens(self, context_limit: int) -> int:
        if self.kind is ThresholdKind.RATIO:
            return int(context_limit * float(self.value))
        return int(self.value)


@dataclass(frozen=True, slots=True)
class HandoffPolicy:
    """Validated actions and thresholds for one model resolution."""

    handoff_enabled: bool
    sweet_zone: Threshold | None
    emergency_enabled: bool
    emergency: Threshold | None


@dataclass(frozen=True, slots=True)
class PolicyResolution:
    """Policy and diagnostics resolved for the active model/provider pair."""

    model: str
    provider: str
    context_limit: int
    match_source: str | None
    policy: HandoffPolicy | None
    errors: tuple[PolicyError, ...] = ()

    @property
    def observation_only(self) -> bool:
        return self.policy is None or not (
            self.policy.handoff_enabled or self.policy.emergency_enabled
        )

    @property
    def handoff_threshold_tokens(self) -> int | None:
        if (
            self.policy is None
            or not self.policy.handoff_enabled
            or self.policy.sweet_zone is None
        ):
            return None
        return self.policy.sweet_zone.to_tokens(self.context_limit)

    @property
    def emergency_threshold_tokens(self) -> int | None:
        if (
            self.policy is None
            or not self.policy.emergency_enabled
            or self.policy.emergency is None
        ):
            return None
        return self.policy.emergency.to_tokens(self.context_limit)
