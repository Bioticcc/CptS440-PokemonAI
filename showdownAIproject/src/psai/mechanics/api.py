"""Stable deterministic mechanics API.

This layer provides factual battle outcomes (damage, KO probability,
move order, reliability, effectiveness). It does not choose moves.
"""

# Plain-English summary:
# This file answers "what happens if we click this move?" and returns numbers.
# Strategy belongs in decision/, not in mechanics/.

# MECHANICS ENGINE MUST BE ABLE TO OUTPUT (at least): 
#expected_damage_to_opponent
#expected_damage_to_self
#ko_probability_to_opponent
#ko_probability_to_self
#move_first
#reliability
#type_effectiveness
# FOR EACH LEGAL ACTION IT IS GIVEN.

# ALSO KEEP IN MIND: For offline training, we wont even use mechanics, so can assume we 
# will always have the raw poke and raw move objects from poke-env (in live battle).

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from psai.domain.state import LegalAction, State
try:
    from poke_env.calc.damage_calc_gen1_2 import calculate_damage_gen12
except ImportError:
    calculate_damage_gen12 = None


@dataclass(slots=True)
class ActionOutcome:
    
    # The stats the mechanics engine will output for each given legal action. These are the minimum,
    # Add more of the winrate belows 50% or otherwise unsatisfactory. More of these should give more
    # accuracy. Maybe. 

    expected_damage_to_opponent: float
    expected_damage_to_self: float
    ko_probability_to_opponent: float
    ko_probability_to_self: float
    move_first: bool
    reliability: float = 1.0
    type_effectiveness: float = 1.0


class MechanicsAPI:
    
    # This is the class, and in the evaluate_action function we call all the mini mechanics functions below.
    # If adding new mechanics, add them as a new helper and call in evaluate_action. 

    def evaluate_action(self, state: State, action: LegalAction) -> ActionOutcome:
        
        # Returns an ActionOutcome with all the stats filled out for this state/action.

        return ActionOutcome(
            expected_damage_to_opponent=get_expected_damage_to_opponent(state, action),
            expected_damage_to_self=get_expected_damage_to_self(state, action),
            ko_probability_to_opponent=get_ko_probability_to_opponent(state, action),
            ko_probability_to_self=get_ko_probability_to_self(state, action),
            move_first=get_move_first(state, action),
            reliability=get_reliability(state, action),
            type_effectiveness=get_type_effectiveness(state, action),
        )


def _status_is(status_value: str | None, *names: str) -> bool:

    # Given the snapshot status value, we check if it matches any of the given statuses. 
    # Just a helper for other functions later. 

    if not status_value:
        return False
    lowered = status_value.lower()
    return lowered in {name.lower() for name in names}


def _safe_getattr(obj, attr: str, default=None):

    if obj is None:
        return default
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:

    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _safe_priority_from_move(move, default: int = 0) -> int:
    return _safe_int(_safe_getattr(move, "priority", default), default)


def _is_status_category_action(action: LegalAction) -> bool:

    if action.is_switch:
        return False
    raw_move = _safe_getattr(action, "raw_move", None)
    if raw_move is not None:
        category = _safe_getattr(raw_move, "category", None)
        category_name = _safe_getattr(category, "name", None)
        if str(category_name or category or "").lower() == "status":
            return True

    damage_class = _safe_getattr(action, "damage_class", None)
    if str(damage_class or "").lower() == "status":
        return True

    base_power = getattr(action, "base_power", None)
    try:
        if base_power is not None and int(base_power) <= 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _stage_multiplier(stage: int) -> float:

    # Weird pokemon thing, stages. So essentially based on what the stat modifier is, like speed +2,
    # we first look at the 

     # stage = max(-6, min(6, int(stage))) Guard code if we get bugs for stage stuff later on, 
     # but I dont think we need this. 
    if stage >= 0:
        return (2.0 + stage) / 2.0
    return 2.0 / (2.0 - stage)


def _get_hp_points(snapshot) -> tuple[float, float]:

    # Using the poke-env pokemon object, we get the current health and max health!

    raw_pokemon = getattr(snapshot, "raw_pokemon", None)
    if raw_pokemon is not None:
        current_hp = getattr(raw_pokemon, "current_hp", None)
        max_hp = getattr(raw_pokemon, "max_hp", None)
        try:
            normalized_current = float(current_hp)
            normalized_max = float(max_hp)
            if normalized_max > 0.0:
                return normalized_current, normalized_max
        except (TypeError, ValueError):
            pass

    # Fallback for partially observed states: use HP fraction as [0.0, 1.0] points.
    try:
        hp_fraction = max(0.0, min(1.0, float(getattr(snapshot, "hp_fraction", 0.0))))
    except (TypeError, ValueError):
        hp_fraction = 0.0
    return hp_fraction, 1.0


def _calculate_damage_rolls(
    state: State,
    action: LegalAction,
    *,
    attacker_is_friendly: bool,
) -> tuple[float, float, list[float]]:

    # Here we use the poke-env built in damage calculator to get the rolls for each pokemon.

    if calculate_damage_gen12 is None:
        return 0.0, 0.0, [0.0]

    battle = state.raw_battle # Raw battle object! remember when I mentioned we need this? this is why!

    # I love this one line if else stuff, nice and neat. Do this whenever possible!
    attacker = state.friendly_active if attacker_is_friendly else state.opponent_active
    defender = state.opponent_active if attacker_is_friendly else state.friendly_active

    if (
        battle is None
        or action.raw_move is None
        or attacker.identifier is None
        or defender.identifier is None
    ):
        return 0.0, 0.0, [0.0]

    # Worth noting:
    # _min_roll = smallest possible damage roll for this move 
    # _max_roll = self explanatory via above
    # rolls = all possible damage rolls (distribution!)
    try:
        min_roll, max_roll, rolls = calculate_damage_gen12(
            attacker.identifier,
            defender.identifier,
            action.raw_move,
            battle,
            is_critical=False,
        )
    except Exception:
        return 0.0, 0.0, [0.0]

    return float(min_roll), float(max_roll), [float(roll) for roll in rolls]


def _expected_damage_fraction_from_rolls(
    min_roll: float,
    max_roll: float,
    rolls: list[float],
    max_hp: float,
) -> float:

    # Here we determine the expected damage being taken in a fraction. Main reason for this is
    # the percentage of HP lost matters a hell of lot more then the base damage. a pokemon with 
    # 100 hp losing 70 hp is a lot worse then a pokemon with 75 hp losting 70 hp.

    if max_hp <= 0.0 or max_roll <= 0.0: # invalid or no damage
        return 0.0
    if min_roll == max_roll: # if no variance in damage
        return min_roll / max_hp
    return mean(rolls) / max_hp # expected via average


def _ko_probability_from_rolls(
    min_roll: float,
    max_roll: float,
    rolls: list[float],
    current_hp: float,
) -> float:

    # What are the odds we kill here? This one checks odds of defending pokemon fainting after damage rolls.

    if current_hp <= 0.0: 
        return 1.0
    if max_roll < current_hp:
        return 0.0
    if min_roll >= current_hp:
        return 1.0
    wins = sum(1 for roll in rolls if roll >= current_hp) # +1 for each roll that would KO
    probability = wins / len(rolls) if rolls else 0.0 # total rolls vs rolls that KO
    return probability


def _make_opponent_action(move) -> LegalAction:

    # converts raw opponent action from poke-env into our LegalAction dataclass.

    move_type = _safe_getattr(move, "type", None)
    move_type_name = str(_safe_getattr(move_type, "name", "")) or None
    action_id = str(_safe_getattr(move, "id", None) or "opponent_move")
    try:
        accuracy = float(_safe_getattr(move, "accuracy", 1.0))
    except (TypeError, ValueError):
        accuracy = 1.0
    return LegalAction(
        action_id=action_id,
        move_name=action_id,
        move_type=move_type_name,
        accuracy=accuracy,
        priority=_safe_priority_from_move(move, 0),
        is_switch=False,
        raw_move=move,
    )


def _opponent_known_moves(state: State) -> list:

    # Given state, we return a list of the known opponent moves. 

    raw_pokemon = getattr(state.opponent_active, "raw_pokemon", None)
    if raw_pokemon is None:
        return []
    moves = getattr(raw_pokemon, "moves", None) or {}
    return list(dict(moves).values())


def _effective_speed(snapshot) -> float:

    # Finds pokemon speed based on modifiers and status, if any.

    raw_pokemon = getattr(snapshot, "raw_pokemon", None)
    if raw_pokemon is None:
        return 0.0

    stats = getattr(raw_pokemon, "stats", None) or {}
    try:
        base_speed = float(stats.get("spe", 0.0))
    except (TypeError, ValueError):
        base_speed = 0.0
    spe_stage = int(snapshot.boosts.get("spe", 0)) # Boosts
    speed = base_speed * _stage_multiplier(spe_stage) # Apply mult

    if _status_is(snapshot.status, "par", "paralyzed", "paralysis"): # Slows if paralyzed
        speed *= 0.25
    return speed


def get_expected_damage_to_opponent(state: State, action: LegalAction) -> float:

    # Use poke-env gen1-2 damage calc here.
    # Return expected damage dealt by this action, using all of our helpers above.

    if action.is_switch or _is_status_category_action(action):
        return 0.0

    min_roll, max_roll, rolls = _calculate_damage_rolls(state, action, attacker_is_friendly=True)
    _current_hp, max_hp = _get_hp_points(state.opponent_active)
    expected_fraction = _expected_damage_fraction_from_rolls(min_roll, max_roll, rolls, max_hp)
    return expected_fraction


def get_expected_damage_to_self(state: State, action: LegalAction) -> float:

    # How much damage are we going to take after this action? 
    # Here we just loop through every known move the opponent has, check 
    # how much damage they would do to us assuming they used the most damage one, and return that.

    if state.opponent_active.fainted:
        return 0.0

    opponent_moves = _opponent_known_moves(state)

    worst_fraction = 0.0
    _current_hp, max_hp = _get_hp_points(state.friendly_active)

    for move in opponent_moves:
        proxy_action = _make_opponent_action(move)
        min_roll, max_roll, rolls = _calculate_damage_rolls(
            state,
            proxy_action,
            attacker_is_friendly=False,
        )
        candidate_fraction = _expected_damage_fraction_from_rolls(
            min_roll,
            max_roll,
            rolls,
            max_hp,
        )
        worst_fraction = max(worst_fraction, candidate_fraction)

    return worst_fraction


def get_ko_probability_to_opponent(state: State, action: LegalAction) -> float:

    # Use damage rolls + opponent HP to estimate KO chance.

    if action.is_switch or _is_status_category_action(action):
        return 0.0

    min_roll, max_roll, rolls = _calculate_damage_rolls(state, action, attacker_is_friendly=True)
    current_hp, _max_hp = _get_hp_points(state.opponent_active)
    return _ko_probability_from_rolls(min_roll, max_roll, rolls, current_hp)


def get_ko_probability_to_self(state: State, action: LegalAction) -> float:

    # Same as the damage to us function, we loop through and find the worst case, and
    # return that as the ko chance of us dying. Could probably combine this with the damage function?

    if state.opponent_active.fainted:
        return 0.0

    opponent_moves = _opponent_known_moves(state)

    current_hp, _max_hp = _get_hp_points(state.friendly_active)
    if current_hp <= 0.0:
        return 1.0

    worst_probability = 0.0
    for move in opponent_moves:
        proxy_action = _make_opponent_action(move)
        min_roll, max_roll, rolls = _calculate_damage_rolls(
            state,
            proxy_action,
            attacker_is_friendly=False,
        )
        probability = _ko_probability_from_rolls(min_roll, max_roll, rolls, current_hp)
        worst_probability = max(worst_probability, probability)

    return worst_probability


def get_move_first(state: State, action: LegalAction) -> bool:

    # Determine if we act first (priority first, then speed).

    if action.is_switch:
        return True

    our_priority = _safe_priority_from_move(action.raw_move, action.priority)
    opponent_moves = _opponent_known_moves(state)
    opponent_priority = max((_safe_priority_from_move(move, 0) for move in opponent_moves), default=0)

    if our_priority != opponent_priority:
        return our_priority > opponent_priority

    friendly_speed = _effective_speed(state.friendly_active)
    opponent_speed = _effective_speed(state.opponent_active)
    return friendly_speed >= opponent_speed


def get_reliability(state: State, action: LegalAction) -> float:

    # Essentially, here we just take the accuracy and make it worse based on status effects.

    if action.is_switch:
        return 1.0

    raw_accuracy = _safe_getattr(action.raw_move, "accuracy", action.accuracy)
    try:
        reliability = float(raw_accuracy)
    except (TypeError, ValueError):
        reliability = float(action.accuracy)
    if reliability > 1.0:
        reliability = reliability / 100.0
    reliability = max(0.0, min(1.0, reliability))

    if _status_is(state.friendly_active.status, "slp", "sleep"):
        reliability *= 0.33
    if _status_is(state.friendly_active.status, "frz", "freeze"):
        reliability *= 0.20
    if _status_is(state.friendly_active.status, "par", "paralyzed", "paralysis"):
        reliability *= 0.75

    return reliability


def get_type_effectiveness(state: State, action: LegalAction) -> float:

    # Compute type multiplier from move type vs opponent type/types.

    if action.is_switch or _is_status_category_action(action):
        return 1.0

    defender = getattr(state.opponent_active, "raw_pokemon", None)
    if defender is None or action.raw_move is None:
        return 1.0

    damage_multiplier = getattr(defender, "damage_multiplier", None)
    if not callable(damage_multiplier):
        return 1.0

    try:
        return float(damage_multiplier(action.raw_move))
    except Exception:
        return 1.0
