import asyncio
from fastapi import FastAPI
from psai.mechanics.api import MechanicsAPI
from psai.app.ui_payload import build_ui_payload
from psai.app.mock_stream import make_mock_state

# using FastAPI for simple API endpoints
app = FastAPI()
mechanics = MechanicsAPI()


# ex: http://127.0.0.1:8000/state?turn=5
# this will show the payloads from the mock stream's turn 5
@app.get("/state")
def get_state(turn: int = 1):
    # single snapshot of battle state
    state = make_mock_state(turn)
    return build_ui_payload(state, mechanics)

# streaming endpoint 
@app.get("/stream")
async def stream_states():
    
    async def generator():
        turn = 1
        while True:
            state = make_mock_state(turn)
            payload = build_ui_payload(state, mechanics)

            yield payload

            await asyncio.sleep(2)
            turn += 1

    return {"message": "Placeholder"}