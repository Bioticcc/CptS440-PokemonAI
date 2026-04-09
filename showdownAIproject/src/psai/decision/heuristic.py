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

    # To add: PP, revealed opp team, setup context (MAYBE, THIS WILL BE HARD), etc.


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
