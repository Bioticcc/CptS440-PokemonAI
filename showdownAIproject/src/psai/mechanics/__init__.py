"""Deterministic mechanics/rules interfaces used by decision code."""

# Plain-English summary:
# This package computes battle facts, not move strategy.

from psai.mechanics.api import (
    ActionOutcome,
    MechanicsAPI,
)

__all__ = [
    "ActionOutcome",
    "MechanicsAPI",
]
