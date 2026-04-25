"""
FastAPI server exposing battle state to the frontend UI
Basically for our HTTP endpoints that reach React UI

By default it returns the latest state from the ui_bridge module,
but can possibly fall back to mock data when no live battle is active

Originally I tested with the commented out mock data stream
So for testing, we can use that endpoint instead of the actually live endpoint
"""

# Plain-English summary:
# This file is the API that the frontend uses to see current battle info

from fastapi import FastAPI
from psai.mechanics.api import MechanicsAPI
from psai.app.ui_payload import build_ui_payload
from psai.app.mock_stream import make_mock_state
from fastapi.middleware.cors import CORSMiddleware
from psai.app.ui_bridge import get_state

# using FastAPI for simple API endpoints
app = FastAPI()
mechanics = MechanicsAPI()

# due to connection errors with the frontend sending requests to the API
# added CORS middleware to allow requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# actually getting live state
@app.get("/state")
def get_state_endpoint():
    state = get_state()

    if state is None:
        return {"battle_tag": "no_battle"}

    return build_ui_payload(state, mechanics)

# OLD TESTING ENDPOINT - TESTING WITH LIVE BATTLE NOW. uncomment this one if you want to test via mock stream
# ex: http://127.0.0.1:8000/state?turn=5
# this will show the payloads from the mock stream's turn 5
# @app.get("/state")
# def get_state(turn: int = 1):
#     # single snapshot of battle state
#     state = make_mock_state(turn)
#     return build_ui_payload(state, mechanics)