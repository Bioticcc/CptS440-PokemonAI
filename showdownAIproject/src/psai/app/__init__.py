"""Runtime application scaffolding and orchestration utilities."""

# Plain-English summary:
# This package exposes the battle-loop intake and chooser handoff flow.

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


def __getattr__(name: str):
    if name in __all__:
        from psai.app import main as main_module

        return getattr(main_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
