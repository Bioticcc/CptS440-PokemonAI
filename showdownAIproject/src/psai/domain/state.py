"""Normalized internal battle-state representation.

This module defines the State contract used across decision, mechanics, app,
and training code. The representation stays independent from raw `poke-env`
objects so parser/runtime details do not leak into the core pipeline.
"""

# Plain-English summary:
# This file defines our own clean snapshot of battle state.
# The rest of the project should rely on this normalized shape, not on
# raw poke-env battle internals.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PokemonSnapshot:

    # The state of a SINGLE pokemon. Should be used for the active pokemon, and the revealed ones we see
    # over the course of the battle, filling out information as we go. 
    
    species: str 
    hp_fraction: float
    status: str | None = None
    boosts: dict[str, int] = field(default_factory=dict)
    types: tuple[str, ...] = field(default_factory=tuple)
    fainted: bool = False

    # Feel free to add additional fields or remove the ones here. This is just some defaults I think could be good.
    # Some kind of error handling for invalid inputs could be good too, or automatically set info like
    # fainted being True if hp_fraction is 0. 


@dataclass(slots=True)
class LegalAction:

    # Single legal actions that the agent is allowed to take. Based on current state. 

    action_id: str
    move_name: str
    move_type: str | None = None
    accuracy: float = 1.0 # should be somewhere in the state for that turn. Surely the battle object has this somewhere
    is_switch: bool = False

    # add whatever you feel you need, same error handling could be good here too. 
    # maybe normalizing accuracy? unsure how accuracy numbers are shown, tbh. Good to check.
    # oh SP (or is PP? unsure) we should keep track of. do they have that in gen1? unsure. if they do, defi track it

@dataclass(slots=True)
class State:

    # The big one! This is state. Use the previous dataclasses to fill out everything. 

    friendly_active: PokemonSnapshot # our pokemon, currently active! use a snapshot.
    opponent_active: PokemonSnapshot # same, but opponent. Fill out info as we go. If we see it use a move, we should fill that info out for that pokemon. 
    legal_actions: tuple[LegalAction, ...] = field(default_factory=tuple) # all actions we can do this turn. 
    
    # Will definetly need more modifiers then this for the state, particularly the enemy pokemons team thus far.
    # absolutely need to keep track of that, and update it as we see more of the team. Can do in the function right below this? 
    # as in the others, some form of error handling could be good here. or not. add if you wish, I dont think we need it.

def parse_battle_to_state(battle: Any) -> State:

    # This is where we take the battle object, and turn it into a state dataclass, that we will return.

    friendly_active = parse_active_pokemon(battle, side="friendly") 
    opponent_active = parse_active_pokemon(battle, side="opponent")
    legal_actions = parse_legal_actions(battle)
    # dont forget we need the enemy team revealed thus far, helper function like the ones above could be good, or just done right here.

    return State( # fill out everything here from the state object above. parse everything needed via helper functions below or new ones.
        friendly_active=friendly_active,
        opponent_active=opponent_active,
        legal_actions=legal_actions,
    )


def parse_active_pokemon(battle: Any, *, side: str) -> PokemonSnapshot:

    # Build a PokemonSnapshot for the active mon on the requested side.
    # Needed fields: species, hp_fraction, status, boosts, types, known_moves.
    # `side` should be "friendly" or "opponent".

    # change return type as needed, or keep as it is.

    raise PokemonSnapshot


def parse_legal_actions(battle: Any) -> tuple[LegalAction, ...]:

    # Convert poke-env legal move options into our LegalAction tuple.

    # change return type as needed, or keep as it is.

    raise legal_actions

