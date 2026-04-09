"""Hand-crafted Phase 1 heuristic scoring logic."""

# Plain-English summary:
# This file turns mechanics numbers into an interpretable score with
# named terms so we can explain why one move ranked above another.

from __future__ import annotations

from dataclasses import dataclass

from psai.domain.state import LegalAction, State
from psai.mechanics.api import ActionOutcome

ReasonBreakdown = dict[str, float] # reason name like chance to win, and the numeric value of said reason.


@dataclass(slots=True)
class HeuristicWeights:

    # The heuristic weights class! This will need to be edited later to match the state object, 
    # but this is a start. We essentially rank things by importance, and give them a point value, so 
    # when making decisions, we simply add up the points for each move, and choose the move with the 
    # highest score, at least when we are using ONLY heuristics. The model will do more planning. 

    # Remmeber, base these off of the state object. Change later.
    ko_now_bonus: float = 1000.0 
    self_ko_penalty: float = 1000.0
    damage_weight: float = 250.0
    incoming_damage_weight: float = 180.0
    move_first_bonus: float = 75.0
    type_effectiveness_weight: float = 60.0
    reliability_weight: float = 40.0
    fainted_bonus: float = 2000.0
    new_status_bonus: float = 80.0
    redundant_status_penalty: float = 260.0
    status_move_base_penalty: float = 40.0
    status_on_low_hp_penalty: float = 180.0
    low_hp_finish_bonus: float = 220.0
    low_hp_finish_threshold: float = 0.35

    # To add: PP, revealed opp team, setup context (MAYBE, THIS WILL BE HARD), etc.


def _to_lower_text(value: object) -> str:
    if value is None:
        return ""
    name_value = getattr(value, "name", None)
    if name_value is not None:
        return str(name_value).lower()
    return str(value).lower()


def _is_status_move(action: LegalAction) -> bool:
    if action.is_switch:
        return False

    raw_move = action.raw_move
    category_text = _to_lower_text(getattr(raw_move, "category", None)) if raw_move is not None else ""
    if category_text == "status":
        return True

    damage_class_text = _to_lower_text(action.damage_class)
    if damage_class_text == "status":
        return True

    base_power = action.base_power
    if base_power is not None and int(base_power) <= 0:
        return True
    return False


def _move_inflicts_status(action: LegalAction) -> bool:
    if action.is_switch or action.raw_move is None:
        return False

    raw_move = action.raw_move
    if getattr(raw_move, "status", None):
        return True
    if getattr(raw_move, "volatile_status", None):
        return True

    secondary = getattr(raw_move, "secondary", None)
    if isinstance(secondary, dict):
        return bool(secondary.get("status") or secondary.get("volatileStatus"))
    if isinstance(secondary, list):
        for entry in secondary:
            if isinstance(entry, dict) and (entry.get("status") or entry.get("volatileStatus")):
                return True

    return False


def score_action(
    state: State,
    action: LegalAction,
    outcome: ActionOutcome,
    weights: HeuristicWeights | None = None, # either passed testing weights, or default from above
) -> tuple[float, ReasonBreakdown]:

    # The function where we score an action. This will be heavily reliant on theh mechanics engine, 
    # as that will be how we determine probability of things happening, and stuff like that. 
    # Returns the total score of the action, based on our heuristic rules and mechanics engine.
    # ALSO MUST CHANGE WITH STATE OBJECT, so update later. 

    w = weights or HeuristicWeights() # testing purposes, or our preestablished weights above.

    # okay, so these are the base rule "terms". Using the heuristic rules, we can determine specific
    # reasons for actions being ranked a certain way, and use that in the score count. 
    # these default terms are used every time for score calc, and the if statements below are extras.
    terms: ReasonBreakdown = {
        "ko_now": w.ko_now_bonus * outcome.ko_probability_to_opponent,
        "avoid_being_koed": -w.self_ko_penalty * outcome.ko_probability_to_self,
        "expected_damage": w.damage_weight * outcome.expected_damage_to_opponent,
        "expected_incoming_damage": -w.incoming_damage_weight * outcome.expected_damage_to_self,
        "move_order": w.move_first_bonus if outcome.move_first else 0.0,
        "type_effectiveness": w.type_effectiveness_weight * (outcome.type_effectiveness - 1.0),
        "reliability": w.reliability_weight * (outcome.reliability - 1.0),
    }

    # These are extra conditional terms. So they add extra terms depending on the situation.
    if state.opponent_active.fainted:
        terms["opponent_fainted"] = w.fainted_bonus
    if state.friendly_active.fainted:
        terms["friendly_fainted"] = -w.fainted_bonus
    if action.is_switch:
        terms["switch_penalty_phase1"] = -50.0

    status_move = _is_status_move(action)
    inflicts_status = _move_inflicts_status(action)
    opponent_has_status = bool(state.opponent_active.status)
    opponent_low_hp = float(state.opponent_active.hp_fraction) <= float(w.low_hp_finish_threshold)

    if status_move:
        terms["status_move_base_penalty"] = -w.status_move_base_penalty
    if inflicts_status and not opponent_has_status:
        terms["new_status_bonus"] = w.new_status_bonus
    if inflicts_status and opponent_has_status:
        terms["redundant_status_penalty"] = -w.redundant_status_penalty
    if status_move and opponent_low_hp:
        terms["status_on_low_hp_penalty"] = -w.status_on_low_hp_penalty
    if (not status_move) and opponent_low_hp:
        terms["low_hp_finish_bonus"] = w.low_hp_finish_bonus * outcome.expected_damage_to_opponent

    total_score = sum(terms.values()) # we then jsut find the total value of all the terms after considering conditionals.
    return total_score, terms # return that total score for the action, as well as the terms for explanation.


def format_reason_breakdown(breakdown: ReasonBreakdown, top_n: int = 4) -> list[str]:

    # We return our terms after determining the final score, 
    # giving us a nice and neat "reason" for the move being scored.

    sorted_terms = sorted(
        ((k, v) for k, v in breakdown.items() if abs(v) > 1e-6), # 1e-6 is just a tiny tiny 0.000000...
        key=lambda kv: abs(kv[1]), # gets rid of terms that are EXTREMELY tiny, and thus not worth considering.
        reverse=True, # sort by largest term to smallest.
    )
    reasons: list[str] = [] # we then keep the top n (4 by default) terms, as they had the largest impact. 
    for key, value in sorted_terms[:top_n]:
        key_label = key.replace("_", " ")
        reasons.append(f"{key_label}: {value:+.2f}")
    return reasons # return our reasons! 
