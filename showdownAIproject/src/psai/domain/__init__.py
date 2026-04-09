"""Shared domain/state data structures used across the project."""

# Plain-English summary:
# This package defines the shared battle-state objects every subsystem uses.

from psai.domain.state import (
    LegalAction,
    PokemonSnapshot,
    State,
    parse_battle_to_state,
)

__all__ = ["LegalAction", "PokemonSnapshot", "State", "parse_battle_to_state"]
