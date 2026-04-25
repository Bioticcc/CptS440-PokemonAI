"""
State bridge between battle engine and UI layer that decouples 
the battle simulation (run_battle) from the UI server

It stores the most recent State snapshot and allows the UI 
server to retrieve it through the FastAPI backend.
"""

# Plain-English summary:
# Holds the most recent battle state in a global variable, 
# and provides functions to update and retrieve that. Very small file!

from typing import Optional
from psai.domain.state import State

_latest_state: Optional[State] = None

def update_state(state: State):
    global _latest_state
    _latest_state = state


def get_state() -> Optional[State]:
    return _latest_state