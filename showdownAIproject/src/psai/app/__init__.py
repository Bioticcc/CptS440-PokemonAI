"""Runtime application scaffolding and orchestration utilities."""

# Plain-English summary:
# This package exposes the battle-loop intake and chooser handoff flow.

from psai.app.main import (
    get_battle,
    get_turn_suggestions,
    get_state,
    get_user_choice,
    main,
    pokeEnvPlayerInfo,
    run_battle,
    send_confirmed_move,
)

__all__ = [
    "get_battle",
    "get_turn_suggestions",
    "get_state",
    "get_user_choice",
    "main",
    "pokeEnvPlayerInfo",
    "run_battle",
    "send_confirmed_move",
]
