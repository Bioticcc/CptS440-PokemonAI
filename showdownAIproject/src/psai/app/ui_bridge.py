"""State and runtime bridge objects shared by UI runtime endpoints."""

from __future__ import annotations

from typing import Any, Optional

from psai.domain.state import State

_latest_state: Optional[State] = None
_latest_suggestions: Any | None = None
_active_interaction_port: Any | None = None


def update_state(state: State, suggestions: Any | None = None) -> None:
    global _latest_state, _latest_suggestions
    _latest_state = state
    _latest_suggestions = suggestions


def get_state() -> Optional[State]:
    return _latest_state


# for readinng the model reasoning and suggestions
def update_suggestions(suggestions) -> None:
    global _latest_suggestions
    _latest_suggestions = suggestions


def get_suggestions():
    return _latest_suggestions


def set_interaction_port(port: Any | None) -> None:
    global _active_interaction_port
    _active_interaction_port = port


def get_interaction_port() -> Any | None:
    return _active_interaction_port
