"""Runtime application scaffolding and orchestration utilities."""

# Plain-English summary:
# This package exposes the battle-loop intake and chooser handoff flow.

from importlib import import_module

__all__ = [
    "get_battle",
    "get_turn_suggestions",
    "get_state",
    "get_user_choice",
    "main",
    "pokeEnvPlayerInfo",
    "run_battle",
    "run_UI_battle",
    "send_confirmed_move",
]


def __getattr__(name: str):
    if name in __all__:
        main_module = import_module("psai.app.main")

        return getattr(main_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
