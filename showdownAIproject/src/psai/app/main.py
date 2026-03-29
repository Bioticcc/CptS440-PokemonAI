"""Runtime scaffold for poke-env battle intake and decision handoff."""

# Plain-English summary:
# This module provides a battle-loop scaffold: get battle objects,
# parse each into State, run chooser, and print move suggestions.

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any

from psai.decision.chooser import ModelBonusFn, MoveSuggestion, choose_actions
from psai.domain.state import State, parse_battle_to_state
from psai.mechanics.api import MechanicsAPI


def get_battle(player):

    # Use poke-env to get the current battle object with our created 'player'. 
    # just return the battle object, no need to fanciful extras (as far as I know thus far)

    battles = getattr(player, "battles", None)

    if not battles:
        return None

    for battle in battles.values():
        if not getattr(battle, "finished", False):
            return battle

    try:
        return next(iter(battles.values()))
    except StopIteration:
        return None


def get_state(battle):

    # Now, use the battle object we got in get_battle, and parse the current information into a state object.
    # Once we have a state object built, I will update heuristics to cover everything. For now,
    # heurisitics is just a template. can use battle.current_observation, i think.

    return parse_battle_to_state(battle)



@dataclass(slots=True) # using dataclass since its essentially a struct, no need for extra setup.
# Essentially just the settings for the agent. Doesnt connect to a live battle itself. 
class PokeEnvConfig:
    # Here we are setting up the user config, essentially what allows our agent to connect to live games.
    username: str = "PokeLearn440"
    password: str = "CPTS440"
    battle_format: str = "gen1randombattle" # forgot if this is actual name, may need to be changed
    team: str | None = None  # pre made team! if we want it. since random battle, not needed probably. testing purposes?

def build_poke_env_player(config: PokeEnvConfig) -> Any:
# Creating a poke-env player using the config above. This will allow us to connect to live games later.

    try:
        from poke_env import AccountConfiguration, ShowdownServerConfiguration
        from poke_env.player import Player
    except ImportError as error: 
        raise RuntimeError(
            "poke-env is not installed. Install dependencies before live integration."
        ) from error

    class ConsoleControlPlayer(Player):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pending_orders: dict[str, Any] = {}

        def set_pending_order(self, battle_tag: str, order: Any) -> None:
            self._pending_orders[battle_tag] = order

        def choose_move(self, battle):
            battle_tag = getattr(battle, "battle_tag", "")

            while True:
                pending_order = self._pending_orders.pop(battle_tag, None)
                if pending_order is not None:
                    return pending_order
                time.sleep(0.1)

    account_configuration = AccountConfiguration(config.username, config.password)
    player_kwargs: dict[str, Any] = { # player config from above
        "account_configuration": account_configuration,
        "server_configuration": ShowdownServerConfiguration,
        "battle_format": config.battle_format,
    }
    if config.team:
        player_kwargs["team"] = config.team

    return ConsoleControlPlayer(**player_kwargs) # and now we have a player! This is what we pass into get_battle. 


def get_turn_suggestions( 
    state: State, 
    mechanics: MechanicsAPI,
    *,
    top_k: int = 3,
    model: ModelBonusFn | None = None, # make sure to switch this to learning system when made, mainly reminder for myself since thats my area.
) -> list[MoveSuggestion]:

    # This is where we will call the model or heuristics to get the 3 move suggestions, and return them.
    # Specifically, this will be asking chooser.py (decision engine) for the top 3 moves. 

    suggestions = choose_actions(state, mechanics, top_k=top_k, model=model)
    return suggestions


def get_user_choice(turn_suggestions: list[MoveSuggestion], battle: Any) -> Any:
    
    # THIS ONE WILL BE UI HEAVY, this is where we get the user choice. 
    # Whether that be via clicking a move on our UI, or typing in a move name, or just a 
    # hardcoded option for testing purposes. 

    available_moves = list(battle.available_moves)
    available_switches = list(battle.available_switches)

    print("What would you like to do?")
    print("1. Attack")
    print("2. Switch")
    first_choice = int(input("Choose 1 or 2: ").strip())

    if first_choice == 1:
        print("Choose move:")
        for index, move in enumerate(available_moves, start=1):
            move_name = getattr(move, "id", None) or getattr(move, "move", None) or f"Move_{index}"
            print(f"{index}. {move_name}")
        move_choice = int(input("Choose move number: ").strip())
        return {"kind": "attack", "index": move_choice}

    print("Switch to:")
    for index, pokemon in enumerate(available_switches, start=1):
        poke_name = getattr(pokemon, "species", None) or getattr(pokemon, "name", None) or f"Poke_{index}"
        print(f"{index}. {poke_name}")
    switch_choice = int(input("Choose switch number: ").strip())
    return {"kind": "switch", "index": switch_choice}


def send_confirmed_move(player: Any, battle: Any, chosen_action: Any) -> Any:
    
    # works together with get move, just sends the move to our agent, which should send it to showdown.
    # unsure if poke-env has something specific for this, but dealable with!

    action_kind = chosen_action["kind"]
    action_index = int(chosen_action["index"])

    if action_kind == "attack":
        selected_move = list(battle.available_moves)[action_index - 1]
        return player.create_order(selected_move)

    selected_switch = list(battle.available_switches)[action_index - 1]
    return player.create_order(selected_switch)


def run_battle(
    # where the magic happens, here we loop from start to end of the show down battle.
    player: Any, # player object we made
    *,
    mechanics: MechanicsAPI, # mechanics engine object, used to score moves (damage n such)
    top_k: int = 3, # how many move suggestions we want. So of 4 moves, we will get top 3, ordered.
    model: ModelBonusFn | None = None, # If heuristic 1v1 mode, none. If model is done, set to that.
    max_turns: int | None = None, # in case we get an infinite loop, like spamming harden or somethin.
) -> None: # no return, should run battle from start to finish in here, taking user input and having agent make moves


    # WORTH NOTING, THIS IS THE MAIN BATTLE FUNCTION. This is the one we run when we have the model done.


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
        chosen_order = send_confirmed_move(player, battle, chosen_action) # send to agent, send to showdown.

        if hasattr(player, "set_pending_order"):
            player.set_pending_order(getattr(battle, "battle_tag", ""), chosen_order)

        turns_ran += 1
        if max_turns is not None and turns_ran >= max_turns: # preventing infinite harden loop 
            break

        time.sleep(0.1)

    return


def run_test_battle(
    player: Any,
    *,
    max_turns: int | None = None,
) -> None:

    # THIS IS THE MANUAL TEST BATTLE FUNCTION. You manually challenge the bot, and then use console
    # to determine moves. Not really needed anymore, but its here since it was used. 

    turns_ran = 0
    last_prompted_turn = -1

    while True:
        battle = get_battle(player)

        if battle is None or battle.finished:
            break

        current_turn = int(battle.turn)
        can_choose = bool(battle.available_moves or battle.available_switches)

        if can_choose and current_turn != last_prompted_turn:
            chosen_action = get_user_choice([], battle)
            chosen_order = send_confirmed_move(player, battle, chosen_action)
            player.set_pending_order(battle.battle_tag, chosen_order)
            last_prompted_turn = current_turn

        turns_ran += 1
        if max_turns is not None and turns_ran >= max_turns:
            break

        time.sleep(0.1)

    return


def run_heuristic_training_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    top_k: int = 3,
    max_turns: int | None = None,
) -> None:

    # THIS IS THE HEURISTIC AUTO TRAINING BATTLE FUNCTION. This does NOT take inputs, and runs the bot
    # making decisions based on heurisitics and search only. 

    turns_ran = 0
    last_prompted_turn = -1

    while True:
        battle = get_battle(player)
        if battle is None or battle.finished:
            break

        current_turn = int(battle.turn)
        can_choose = bool(battle.available_moves or battle.available_switches)

        if can_choose and current_turn != last_prompted_turn:
            state = get_state(battle)
            turn_suggestions = get_turn_suggestions(state, mechanics, top_k=top_k, model=None)
            best_action = turn_suggestions[0].action

            if best_action.is_switch:
                selected_switch = list(battle.available_switches)[0]
                chosen_order = player.create_order(selected_switch)
            else:
                selected_move = best_action.raw_move if best_action.raw_move is not None else list(battle.available_moves)[0]
                chosen_order = player.create_order(selected_move)

            player.set_pending_order(battle.battle_tag, chosen_order)
            last_prompted_turn = current_turn

        turns_ran += 1
        if max_turns is not None and turns_ran >= max_turns:
            break

        time.sleep(0.1)

    return


def run_model_training_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    model: ModelBonusFn,
    top_k: int = 3,
    max_turns: int | None = None,
) -> None:

    # THIS IS THE MODEL AUTO TRAINING BATTLE FUNCTION. Same as above, but for the full model, instead.

    turns_ran = 0
    last_prompted_turn = -1

    while True:
        battle = get_battle(player)
        if battle is None or battle.finished:
            break

        current_turn = int(battle.turn)
        can_choose = bool(battle.available_moves or battle.available_switches)

        if can_choose and current_turn != last_prompted_turn:
            state = get_state(battle)
            turn_suggestions = get_turn_suggestions(state, mechanics, top_k=top_k, model=model)
            best_action = turn_suggestions[0].action

            if best_action.is_switch:
                selected_switch = list(battle.available_switches)[0]
                chosen_order = player.create_order(selected_switch)
            else:
                selected_move = best_action.raw_move if best_action.raw_move is not None else list(battle.available_moves)[0]
                chosen_order = player.create_order(selected_move)

            player.set_pending_order(battle.battle_tag, chosen_order)
            last_prompted_turn = current_turn

        turns_ran += 1
        if max_turns is not None and turns_ran >= max_turns:
            break

        time.sleep(0.1)

    return


def connect_to_battle(
    player: Any,
    *,
    opponent_username: str = "PokeTeach440", # can change to ladder opponent, or run multiple battles, for now its a premade account. 
    max_turns: int | None = None,
) -> Any:

    # ASYNC GOBBLEDEEGOOK. what lets us connect to a battle.

    async def _connect() -> Any:
        accept_task = asyncio.create_task(player.accept_challenges(opponent_username, 1))
        wait_loops = max_turns or 1000

        try:
            for _ in range(wait_loops):
                battle = get_battle(player)
                if battle is not None:
                    bot_name = getattr(battle, "player_username", None) or getattr(player, "username", None) or "unknown"
                    human_name = getattr(battle, "opponent_username", None) or "unknown"
                    battle_tag = getattr(battle, "battle_tag", "unknown")
                    print(f"Live battle connected: bot='{bot_name}' vs human='{human_name}' (tag={battle_tag})")
                    return battle

                await asyncio.sleep(0.2)

            print("Timed out waiting for a live battle object.")
            return None
        finally:
            accept_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await accept_task

    return asyncio.run(_connect())


def main() -> int:
    
    # This is where we call everything. HEAVILY UI BASED, CAN DO IN CONSOLE FOR NOW.
    # Add pretty, funky UI later. 

    # TO RUN (in bash):
    #cd /home/jeezu/CptS440-PokemonAI/showdownAIproject
    #source .venv/bin/activate
    #python3 -m psai.app.main

    
    config = PokeEnvConfig() # set config (user, pass, format)
    player = build_poke_env_player(config) # builds our poke-env player
    connected_battle = connect_to_battle(player, opponent_username="PokeTeach440", max_turns=100) # SHOULD connect to showdown!
    
    # DIFFERENT BATTLE TYPES, uncomment whichever one we run.

    #run_battle(player, mechanics=MechanicsAPI(), top_k=3, model=None, max_turns=100) 
    #run_test_battle(player, max_turns=100000)
    run_heuristic_training_battle(player, mechanics=MechanicsAPI(), top_k=3, max_turns=100000)
    #run_model_training_battle(player, mechanics=MechanicsAPI(), model=None, top_k=3, max_turns=100000)


if __name__ == "__main__":
    raise SystemExit(main())
