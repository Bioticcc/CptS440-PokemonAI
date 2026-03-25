"""Runtime scaffold for poke-env battle intake and decision handoff."""

# Plain-English summary:
# This module provides a battle-loop scaffold: get battle objects,
# parse each into State, run chooser, and print move suggestions.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psai.decision.chooser import MoveSuggestion, choose_actions
from psai.domain.state import State
from psai.mechanics.api import MechanicsAPI


def get_battle(player):

    # Use poke-env to get the current battle object with our created 'player'. 
    # just return the battle object, no need to fanciful extras (as far as I know thus far)

    return battle


def get_state(battle):

    # Now, use the battle object we got in get_battle, and parse the current information into a state object.
    # Once we have a state object built, I will update heuristics to cover everything. For now,
    # heurisitics is just a template. can use battle.current_observation, i think.

    return state



@dataclass(slots=True) # using dataclass since its essentially a struct, no need for extra setup.
# Essentially just the settings for the agent. Doesnt connect to a live battle itself. 
class PokeEnvConfig:
    # Here we are setting up the user config, essentially what allows our agent to connect to live games.
    username: str = "showdown user name for the agent"
    password: str = "password for the showdown user agent"
    battle_format: str = "gen1randombattle" # forgot if this is actual name, may need to be changed
    team: str | None = None  # pre made team! if we want it. since random battle, not needed probably. testing purposes?

def build_poke_env_player(config: PokeEnvConfig) -> Any:
# Creating a poke-env player using the config above. This will allow us to connect to live games later.

    try:
        from poke_env.player import Player # importing poke-env player class. probably should do this in a wider init file? not used to python specifics format wise 
    except ImportError as error: 
        raise RuntimeError(
            "poke-env is not installed. Install dependencies before live integration."
        ) from error

    player_kwargs: dict[str, Any] = { # player config from above
        "username": config.username,
        "password": config.password,
        "battle_format": config.battle_format,
    }
    if config.team:
        player_kwargs["team"] = config.team

    return Player(**player_kwargs) # and now we have a player! This is what we pass into get_battle. 


def get_turn_suggestions( 
    state: State, 
    mechanics: MechanicsAPI,
    *,
    top_k: int = 3,
    model: Any = None, # make sure to switch this to learning system when made, mainly reminder for myself since thats my area.
) -> list[MoveSuggestion]:

    # This is where we will call the model or heuristics to get the 3 move suggestions, and return them.
    # Specifically, this will be asking chooser.py (decision engine) for the top 3 moves. 

    suggestions = choose_actions(state, mechanics, top_k=top_k, model=model)
    return suggestions


def get_user_choice(turn_suggestions: list[MoveSuggestion], battle: Any) -> Any:
    
    # THIS ONE WILL BE UI HEAVY, this is where we get the user choice. 
    # Whether that be via clicking a move on our UI, or typing in a move name, or just a 
    # hardcoded option for testing purposes. 

    return None


def send_confirmed_move(player: Any, battle: Any, chosen_action: Any) -> None:
    
    # works together with get move, just sends the move to our agent, which should send it to showdown.
    # unsure if poke-env has something specific for this, but dealable with!

    return


def run_battle(
    # where the magic happens, here we loop from start to end of the show down battle.
    player: Any, # player object we made
    *,
    mechanics: MechanicsAPI, # mechanics engine object, used to score moves (damage n such)
    top_k: int = 3, # how many move suggestions we want. So of 4 moves, we will get top 3, ordered.
    model: Any = None, # If heuristic 1v1 mode, none. If model is done, set to that.
    max_turns: int | None = None, # in case we get an infinite loop, like spamming harden or somethin.
) -> None: # no return, should run battle from start to finish in here, taking user input and having agent make moves

    # heres the loop! 
    turns_ran = 0

    while True:
        battle = get_battle(player) # gets the poke-env battle object

        # stop loop if no battle object is available
        if battle is None:
            break

        # poke-env battle object usually has .finished when the battle is done
        if getattr(battle, "finished", False):
            break

        state = get_state(battle) # state from state parser.
        turn_suggestions = get_turn_suggestions(state, mechanics, top_k=top_k, model=model)

        for suggestion in turn_suggestions: # go through each suggestion from chooser, rank and print
            print(
                f"#{suggestion.rank} {suggestion.action.move_name} "
                f"(score={suggestion.score:.2f})"
            )
            for reason in suggestion.reasons:
                print(f"  - {reason}") # this will just be a number, probably estimated %chance of victory

        chosen_action = get_user_choice(turn_suggestions, battle) # UI stuff, choose the action
        send_confirmed_move(player, battle, chosen_action) # send to agent, send to showdown.

        turns_ran += 1
        if max_turns is not None and turns_ran >= max_turns: # preventing infinite harden loop 
            break

    return


def main() -> int:
    
    # This is where we call everything. HEAVILY UI BASED, CAN DO IN CONSOLE FOR NOW.
    # Add pretty, funky UI later. 
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
