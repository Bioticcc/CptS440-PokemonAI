import time
import json

from psai.domain.state import State, PokemonSnapshot, LegalAction
from psai.app.ui_payload import build_ui_payload
from psai.mechanics.api import MechanicsAPI


# creating some fake data for testing
# didn't want to mess with the real battle engine since we're still training
# so this file is to make sure our payloads are working correctly and we can stream data through the API
# without needing to set up a real battle yet

def make_mock_state(turn: int) -> State:
    
    # our team (just pikachu)
    # --- [ OUR TEAM! ] ---
    pikachu = PokemonSnapshot(
        species="pikachu",
        hp_fraction=max(0.1, 1.0 - turn * 0.1), # simulate HP going down
        status=None,
        boosts={},
        types=("electric",),
        known_moves=("thunderbolt", "quick_attack", "growl", "spark"),
        fainted=False,
    )
    
    bulbasaur = PokemonSnapshot(
        species="bulbasaur",
        hp_fraction=max(0.1, 1.0 - turn * 0.1), # simulate HP going down
        status=None,
        boosts={},
        types=("grass",),
        known_moves=("vine_whip"),
        fainted=True,
    )
    
    squirtle = PokemonSnapshot(
        species="squirtle",
        hp_fraction=max(0.1, 1.0 - turn * 0.1), # simulate HP going down
        status="paralyzed",
        boosts={},
        types=("water",),
        known_moves=("water_gun"),
        fainted=False,
    )
    
    charizard = PokemonSnapshot(
        species="charizard",
        hp_fraction=max(0.1, 1.0 - turn * 0.1), # simulate HP going down
        status="burned",
        boosts={},
        types=("fire", "flying"),
        known_moves=("flamethrower"),
        fainted=False,
    )
    
    pideotto = PokemonSnapshot(
        species="pidgeotto",
        hp_fraction=max(0.1, 1.0 - turn * 0.1), # simulate HP going down
        status="asleep",
        boosts={},
        types=("normal", "flying"),
        known_moves=("gust"),
        fainted=False,
    )

    tauros = PokemonSnapshot(
        species="tauros",
        hp_fraction=max(0.1, 1.0 - turn * 0.1), # simulate HP going down
        status=None,
        boosts={},
        types=("normal",),
        known_moves=("tackle"),
        fainted=False,
    )
    
    # --- [ OPPONENT TEAM! ] ---
    
    # opponent team (just charmander)
    charmander = PokemonSnapshot(
        species="charmander",
        hp_fraction=max(0.1, 1.0 - turn * 0.12), # simulate HP going down
        status=None,
        boosts={},
        types=("fire",),
        known_moves=("scratch",),
        fainted=False,
    )

    # legal action (not sure if these are fully correct but testing payload structure for now)
    thunderbolt = LegalAction(
        action_id="thunderbolt",
        move_name="Thunderbolt",
        move_type="electric",
        base_power=90,
        damage_class="special",
        accuracy=1.0,
        priority=0,
        current_pp=15,
        max_pp=15,
        is_switch=False,
        raw_move=None,
    )

    quick_attack = LegalAction(
        action_id="quick_attack",
        move_name="Quick Attack",
        move_type="normal",
        base_power=40,
        damage_class="physical",
        accuracy=1.0,
        priority=1,
        current_pp=30,
        max_pp=30,
        is_switch=False,
        raw_move=None,
    )
    
    growl = LegalAction(
        action_id="growl",
        move_name="Growl",
        move_type="normal",
        base_power=0,
        damage_class="status",
        accuracy=1.0,
        priority=0,
        current_pp=40,
        max_pp=40,
        is_switch=False,
        raw_move=None,
    )
    
    spark = LegalAction(
        action_id="spark",
        move_name="Spark",
        move_type="electric",
        base_power=65,
        damage_class="physical",
        accuracy=1.0,
        priority=0,
        current_pp=20,
        max_pp=20,
        is_switch=False,
        raw_move=None,
    )

    # actual battle state!
    return State(
        battle_tag="mock_battle",
        turn_number=turn,
        friendly_active=pikachu,
        opponent_active=charmander,
        friendly_team=(pikachu, bulbasaur, squirtle, charizard, pideotto, tauros),
        opponent_team=(charmander,),
        legal_actions=(thunderbolt, quick_attack, growl, spark),
        raw_battle=None,
    )


def run_mock_stream():
    mechanics = MechanicsAPI()

    # starting turn
    turn = 1

    while True:
        state = make_mock_state(turn)
        payload = build_ui_payload(state, mechanics)

        print("\n--- [ UI PAYLOAD (MOCK STREAM) ] ---")
        print(json.dumps(payload, indent=2))

        # every two seconds we update our state
        time.sleep(2)
        turn += 1


# for testing purposes, you can keep running this mock_stream along with ui_server.py
# shows the connection between the mock stream and the online api endpoint
# see readme.md for frontend to know how to run this
if __name__ == "__main__":
    run_mock_stream()