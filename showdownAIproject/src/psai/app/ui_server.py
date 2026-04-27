"""FastAPI server exposing battle and UI interaction state for frontend runtime."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from psai.app.ui_bridge import get_interaction_port, get_state
from psai.app.ui_payload import build_ui_payload
from psai.mechanics.api import MechanicsAPI

app = FastAPI()
mechanics = MechanicsAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PromptResponseRequest(BaseModel):
    prompt_id: int
    choice_id: str | None = None
    value: str | None = None


@app.get("/state")
def get_state_endpoint() -> dict[str, Any]:
    state = get_state()
    if state is None:
        return {"battle_tag": "no_battle"}
    return build_ui_payload(state, mechanics)


@app.get("/ui/prompt")
def get_ui_prompt_endpoint() -> dict[str, Any]:
    interaction_port = get_interaction_port()
    if interaction_port is None or not hasattr(interaction_port, "get_prompt_snapshot"):
        return {"prompt": None}
    return {"prompt": interaction_port.get_prompt_snapshot()}


@app.post("/ui/response")
def post_ui_response_endpoint(payload: PromptResponseRequest) -> dict[str, Any]:
    interaction_port = get_interaction_port()
    if interaction_port is None or not hasattr(interaction_port, "submit_prompt_response"):
        raise HTTPException(status_code=409, detail="ui_interaction_unavailable")

    accepted, reason = interaction_port.submit_prompt_response(
        prompt_id=int(payload.prompt_id),
        choice_id=payload.choice_id,
        value=payload.value,
    )
    if not accepted:
        status_code = 409 if reason in {"stale_prompt", "no_active_prompt"} else 400
        raise HTTPException(status_code=status_code, detail=reason)

    return {"accepted": True}


@app.get("/ui/logs")
def get_ui_logs_endpoint(since: int = Query(default=0, ge=0)) -> dict[str, Any]:
    interaction_port = get_interaction_port()
    if interaction_port is None or not hasattr(interaction_port, "get_logs_since"):
        return {"cursor": int(since), "items": []}
    return interaction_port.get_logs_since(int(since))
