"""Structured diagnostic errors for handoff policy resolution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PolicyError:
    """A configuration error that keeps policy behavior safely disabled."""

    code: str
    path: str
    message: str
