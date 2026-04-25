import asyncio
from fastapi import FastAPI
from psai.mechanics.api import MechanicsAPI
from psai.app.ui_payload import build_ui_payload
from psai.app.mock_stream import make_mock_state
from fastapi.middleware.cors import CORSMiddleware

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


# ex: http://127.0.0.1:8000/state?turn=5
# this will show the payloads from the mock stream's turn 5
@app.get("/state")
def get_state(turn: int = 1):
    # single snapshot of battle state
    state = make_mock_state(turn)
    return build_ui_payload(state, mechanics)