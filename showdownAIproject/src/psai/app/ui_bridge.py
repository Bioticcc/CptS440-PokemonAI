"""State and runtime bridge objects shared by UI runtime endpoints."""

from __future__ import annotations

from typing import Any, Optional

from psai.domain.state import State

_latest_state: Optional[State] = None
_active_interaction_port: Any | None = None


def update_state(state: State) -> None:
    global _latest_state
    _latest_state = state


def get_state() -> Optional[State]:
    return _latest_state


def set_interaction_port(port: Any | None) -> None:
    global _active_interaction_port
    _active_interaction_port = port


def get_interaction_port() -> Any | None:
    return _active_interaction_port
