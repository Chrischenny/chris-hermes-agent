"""Fail-closed resolver for user-configured model handoff policies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from .errors import PolicyError
from .models import HandoffPolicy, PolicyResolution, Threshold, ThresholdKind

_ROOT_KEYS = frozenset({"default_policy", "model_policies", "provider_policies"})
_POLICY_KEYS = frozenset(
    {"handoff_enabled", "sweet_zone", "emergency", "emergency_enabled"}
)
_EMERGENCY_KEYS = frozenset({"enabled", "type", "threshold"})


class _InvalidPolicy(ValueError):
    def __init__(self, error: PolicyError) -> None:
        super().__init__(error.message)
        self.error = error


class PolicyResolver:
    """Resolve a policy using exact, longest-substring, provider, then default."""

    def __init__(self, config: object) -> None:
        self._config, self._config_errors = self._validate_root(config)

    def resolve(
        self,
        model: str,
        provider: str,
        context_limit: int,
    ) -> PolicyResolution:
        if self._config_errors:
            return PolicyResolution(
                model=model,
                provider=provider,
                context_limit=context_limit,
                match_source=None,
                policy=None,
                errors=self._config_errors,
            )

        if (
            not isinstance(context_limit, int)
            or isinstance(context_limit, bool)
            or context_limit <= 0
        ):
            return self._error_resolution(
                model,
                provider,
                context_limit,
                None,
                PolicyError(
                    code="invalid_context_limit",
                    path="context_limit",
                    message="Context limit must be a positive integer.",
                ),
            )

        candidate = self._select_candidate(model, provider)
        if isinstance(candidate, PolicyError):
            return self._error_resolution(
                model,
                provider,
                context_limit,
                None,
                candidate,
            )
        if candidate is None:
            return PolicyResolution(
                model=model,
                provider=provider,
                context_limit=context_limit,
                match_source=None,
                policy=None,
            )

        source, path, raw_policy = candidate
        try:
            policy = self._parse_policy(raw_policy, path, context_limit)
        except _InvalidPolicy as exc:
            return self._error_resolution(
                model,
                provider,
                context_limit,
                source,
                exc.error,
            )
        return PolicyResolution(
            model=model,
            provider=provider,
            context_limit=context_limit,
            match_source=source,
            policy=policy,
        )

    @staticmethod
    def _validate_root(
        config: object,
    ) -> tuple[dict[str, Any], tuple[PolicyError, ...]]:
        if config is None:
            return {}, ()
        if not isinstance(config, Mapping):
            return {}, (
                PolicyError(
                    code="invalid_handoff_config",
                    path="handoff",
                    message="Handoff configuration must be a mapping.",
                ),
            )

        copied = dict(config)
        unknown = sorted(str(key) for key in copied if key not in _ROOT_KEYS)
        if unknown:
            unknown_names = ", ".join(unknown)
            return {}, (
                PolicyError(
                    code="unknown_config_key",
                    path="handoff",
                    message=f"Unknown handoff configuration keys: {unknown_names}.",
                ),
            )

        errors: list[PolicyError] = []
        for map_name in ("model_policies", "provider_policies"):
            raw_mapping = copied.get(map_name, {})
            if not isinstance(raw_mapping, Mapping):
                errors.append(
                    PolicyError(
                        code="invalid_policy_mapping",
                        path=f"handoff.{map_name}",
                        message=f"{map_name} must be a mapping.",
                    )
                )
                continue
            invalid_keys = [
                key for key in raw_mapping if not isinstance(key, str) or not key
            ]
            if invalid_keys:
                errors.append(
                    PolicyError(
                        code="invalid_policy_key",
                        path=f"handoff.{map_name}",
                        message="Policy keys must be non-empty strings.",
                    )
                )
            copied[map_name] = dict(raw_mapping)
        return copied, tuple(errors)

    def _select_candidate(
        self,
        model: str,
        provider: str,
    ) -> tuple[str, str, object] | PolicyError | None:
        model_policies = self._config.get("model_policies", {})
        provider_policies = self._config.get("provider_policies", {})

        if model in model_policies:
            return (
                f"exact:model:{model}",
                f"handoff.model_policies.{model}",
                model_policies[model],
            )

        matching_keys = [key for key in model_policies if key in model]
        if matching_keys:
            longest_length = max(map(len, matching_keys))
            longest = [key for key in matching_keys if len(key) == longest_length]
            if len(longest) > 1:
                return PolicyError(
                    code="ambiguous_model_pattern",
                    path="handoff.model_policies",
                    message=(
                        "Multiple equally specific model patterns match "
                        f"{model!r}: {', '.join(sorted(longest))}."
                    ),
                )
            key = longest[0]
            return (
                f"pattern:model:{key}",
                f"handoff.model_policies.{key}",
                model_policies[key],
            )

        if provider in provider_policies:
            return (
                f"provider:{provider}",
                f"handoff.provider_policies.{provider}",
                provider_policies[provider],
            )

        if "default_policy" in self._config:
            return (
                "default",
                "handoff.default_policy",
                self._config["default_policy"],
            )
        return None

    def _parse_policy(
        self,
        raw_policy: object,
        path: str,
        context_limit: int,
    ) -> HandoffPolicy:
        if not isinstance(raw_policy, Mapping):
            self._fail(
                "invalid_policy",
                path,
                "Policy must be a mapping.",
            )
        policy = dict(raw_policy)
        unknown = sorted(str(key) for key in policy if key not in _POLICY_KEYS)
        if unknown:
            self._fail(
                "unknown_policy_key",
                path,
                f"Unknown policy keys: {', '.join(unknown)}.",
            )

        handoff_enabled = self._required_bool(
            policy,
            "handoff_enabled",
            path,
            "invalid_handoff_enabled",
        )
        raw_sweet_zone = policy.get("sweet_zone")
        if handoff_enabled:
            if raw_sweet_zone is None:
                self._fail(
                    "missing_sweet_zone",
                    f"{path}.sweet_zone",
                    "Enabled handoff policy requires sweet_zone.",
                )
            sweet_zone = self._parse_threshold(
                raw_sweet_zone,
                f"{path}.sweet_zone",
                "start",
            )
            self._validate_below_context_limit(
                sweet_zone,
                f"{path}.sweet_zone.start",
                context_limit,
            )
        else:
            if raw_sweet_zone is not None:
                self._fail(
                    "disabled_policy_has_threshold",
                    f"{path}.sweet_zone",
                    "Disabled handoff policy cannot define sweet_zone.",
                )
            sweet_zone = None

        emergency_enabled, emergency = self._parse_emergency(policy, path)
        if emergency is not None:
            self._validate_below_context_limit(
                emergency,
                f"{path}.emergency.threshold",
                context_limit,
            )
            if sweet_zone is not None and emergency.to_tokens(
                context_limit
            ) <= sweet_zone.to_tokens(context_limit):
                self._fail(
                    "emergency_not_above_sweet_zone",
                    f"{path}.emergency.threshold",
                    "Emergency threshold must be above the sweet-zone start.",
                )

        return HandoffPolicy(
            handoff_enabled=handoff_enabled,
            sweet_zone=sweet_zone,
            emergency_enabled=emergency_enabled,
            emergency=emergency,
        )

    def _parse_emergency(
        self,
        policy: dict[str, Any],
        path: str,
    ) -> tuple[bool, Threshold | None]:
        alias_present = "emergency_enabled" in policy
        alias_enabled = False
        if alias_present:
            alias_enabled = self._required_bool(
                policy,
                "emergency_enabled",
                path,
                "invalid_emergency_enabled",
            )

        raw_emergency = policy.get("emergency")
        if raw_emergency is None:
            if alias_enabled:
                self._fail(
                    "missing_emergency_policy",
                    f"{path}.emergency",
                    "Enabled emergency fallback requires emergency settings.",
                )
            return False, None
        if not isinstance(raw_emergency, Mapping):
            self._fail(
                "invalid_emergency_policy",
                f"{path}.emergency",
                "Emergency settings must be a mapping.",
            )
        emergency_config = dict(raw_emergency)
        unknown = sorted(
            str(key) for key in emergency_config if key not in _EMERGENCY_KEYS
        )
        if unknown:
            self._fail(
                "unknown_emergency_key",
                f"{path}.emergency",
                f"Unknown emergency keys: {', '.join(unknown)}.",
            )
        nested_enabled = self._required_bool(
            emergency_config,
            "enabled",
            f"{path}.emergency",
            "invalid_emergency_enabled",
        )
        if alias_present and alias_enabled != nested_enabled:
            self._fail(
                "conflicting_emergency_enabled",
                f"{path}.emergency_enabled",
                "emergency_enabled conflicts with emergency.enabled.",
            )
        if not nested_enabled:
            if "type" in emergency_config or "threshold" in emergency_config:
                self._fail(
                    "disabled_policy_has_threshold",
                    f"{path}.emergency",
                    "Disabled emergency policy cannot define a threshold.",
                )
            return False, None
        return True, self._parse_threshold(
            emergency_config,
            f"{path}.emergency",
            "threshold",
            allowed_keys=_EMERGENCY_KEYS,
        )

    def _parse_threshold(
        self,
        raw_threshold: object,
        path: str,
        value_key: str,
        *,
        allowed_keys: frozenset[str] | None = None,
    ) -> Threshold:
        if not isinstance(raw_threshold, Mapping):
            self._fail(
                "invalid_threshold",
                path,
                "Threshold settings must be a mapping.",
            )
        threshold = dict(raw_threshold)
        expected_keys = allowed_keys or frozenset({"type", value_key})
        unknown = sorted(str(key) for key in threshold if key not in expected_keys)
        if unknown:
            self._fail(
                "unknown_threshold_key",
                path,
                f"Unknown threshold keys: {', '.join(unknown)}.",
            )

        raw_kind = threshold.get("type")
        if not isinstance(raw_kind, str):
            self._fail(
                "unknown_threshold_type",
                f"{path}.type",
                f"Unsupported threshold type: {raw_kind!r}.",
            )
        try:
            kind = ThresholdKind(raw_kind)
        except ValueError:
            self._fail(
                "unknown_threshold_type",
                f"{path}.type",
                f"Unsupported threshold type: {raw_kind!r}.",
            )
        value = threshold.get(value_key)
        if kind is ThresholdKind.RATIO:
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not 0 < float(value) < 1
            ):
                self._fail(
                    "ratio_out_of_range",
                    f"{path}.{value_key}",
                    "Ratio threshold must be a number strictly between 0 and 1.",
                )
            return Threshold(kind=kind, value=float(value))
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            self._fail(
                "absolute_tokens_invalid",
                f"{path}.{value_key}",
                "absolute_tokens threshold must be a positive integer.",
            )
        return Threshold(kind=kind, value=value)

    def _validate_below_context_limit(
        self,
        threshold: Threshold,
        path: str,
        context_limit: int,
    ) -> None:
        if threshold.to_tokens(context_limit) >= context_limit:
            self._fail(
                "threshold_at_or_above_context_limit",
                path,
                "Threshold must be below the active model context limit.",
            )

    @staticmethod
    def _required_bool(
        mapping: dict[str, Any],
        key: str,
        path: str,
        code: str,
    ) -> bool:
        value = mapping.get(key)
        if not isinstance(value, bool):
            PolicyResolver._fail(
                code,
                f"{path}.{key}",
                f"{key} must be a boolean.",
            )
        return value

    @staticmethod
    def _fail(code: str, path: str, message: str) -> NoReturn:
        raise _InvalidPolicy(PolicyError(code=code, path=path, message=message))

    @staticmethod
    def _error_resolution(
        model: str,
        provider: str,
        context_limit: int,
        source: str | None,
        error: PolicyError,
    ) -> PolicyResolution:
        return PolicyResolution(
            model=model,
            provider=provider,
            context_limit=context_limit,
            match_source=source,
            policy=None,
            errors=(error,),
        )
