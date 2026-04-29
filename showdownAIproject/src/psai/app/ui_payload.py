"""
Converts State objects into frontend-ready JSON payloads for the UI renderer
Takes PokemonSnapshot, LegalAction, and the mechanics engine
"""

from psai.domain.state import LegalAction, State
from psai.mechanics.api import ActionOutcome, MechanicsAPI
from typing import TypedDict, List, Optional

# convert game state and mechanics outputs into JSON for frontend dynamic rendering
# suggestions is for the model information!
def build_ui_payload(state: State, mechanics: MechanicsAPI, suggestions=None) -> dict:

    friendly = {
        "active": _pokemon_to_ui(state.friendly_active),
        "team": [_pokemon_to_ui(p) for p in state.friendly_team],
    }

    opponent = {
        "active": _pokemon_to_ui(state.opponent_active),
        "team": [_pokemon_to_ui(p) for p in state.opponent_team],
    }

    # for the model suggestions, map by action id
    suggestion_map = {
        s.action.action_id: s for s in (suggestions or [])
    }
    
    # legal actions WITH model suggestions
    legal_actions = [
        _action_to_ui(state, mechanics, action, suggestion_map)
        for action in state.legal_actions
    ]

    return {
        "battle_tag": state.battle_tag,
        "turn": state.turn_number or 1,
        "friendly": friendly,
        "opponent": opponent,
        "legal_actions": legal_actions,
    }
    
    
# ---- [ PAYLOAD STRUCTURES ] ----
# TODO: add more to the UI payload structure while we decide what information to display
class PokemonUI(TypedDict):
    species: str            # species name
    hp_fraction: float      # current HP as a fraction of max HP
    status: Optional[str]   # "paralyzed", "burned", or None if no status condition
    fainted: bool           # whether the Pokemon is fainted
    types: List[str]        # list of type names
    known_moves: List[str]  # list of move names that the player has seen this Pokemon use in the battle so far


# action outcome info (dmg, etc.) from mechanics api
class ActionOutcomeUI(TypedDict):
    expected_damage_to_opponent: float
    expected_damage_to_self: float
    ko_probability_to_opponent: float
    ko_probability_to_self: float
    move_first: bool
    reliability: float
    type_effectiveness: float

# legal action info
class ActionUI(TypedDict):
    action_id: str
    move_name: str
    is_switch: bool             # whether this action is a switch (vs. a move)
    outcome: ActionOutcomeUI    # calculations
    score: float                # model's score for an action
    rank: Optional[int]         # model's rank for an action
    reasons: List[str]          # list of model's reasoning

# easier info organization for friendly and opponent 
class SideUI(TypedDict):
    active: PokemonUI
    team: List[PokemonUI]

# general battle informationi
class BattleUIPayload(TypedDict):
    battle_tag: str 
    turn: int
    friendly: SideUI
    opponent: SideUI
    legal_actions: List[ActionUI]


# ---- [ PAYLOAD BUILDING HELPERS ] ----
# we're just using these to pull the info from our game state and load them into our payloads
def _pokemon_to_ui(p) -> PokemonUI:
    return {
        "species": p.species,
        "hp_fraction": p.hp_fraction,
        "status": p.status,
        "fainted": p.fainted,
        "types": list(p.types),
        "known_moves": list(p.known_moves),
    }
    
def _outcome_to_ui(outcome: ActionOutcome) -> ActionOutcomeUI:
    return {
        "expected_damage_to_opponent": outcome.expected_damage_to_opponent,
        "expected_damage_to_self": outcome.expected_damage_to_self,
        "ko_probability_to_opponent": outcome.ko_probability_to_opponent,
        "ko_probability_to_self": outcome.ko_probability_to_self,
        "move_first": outcome.move_first,
        "reliability": outcome.reliability,
        "type_effectiveness": outcome.type_effectiveness,
    }
    
def _action_to_ui(state: State, mechanics: MechanicsAPI, action: LegalAction, suggestion_map=None) -> ActionUI:

    outcome = mechanics.evaluate_action(state, action)
    suggestion = suggestion_map.get(action.action_id) if suggestion_map else None

    return {
        "action_id": action.action_id,
        "move_name": action.move_name,
        "is_switch": action.is_switch,
        "current_pp": action.current_pp,
        "max_pp": action.max_pp,
        "outcome": _outcome_to_ui(outcome),
        
        # onto the actual model suggestions info!
        "score": suggestion.score if suggestion else 0.0,
        "rank": suggestion.rank if suggestion else None,
        "reasons": suggestion.reasons if suggestion else [],
    }