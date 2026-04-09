"""Shallow search/ranking for candidate move actions."""

# Plain-English summary:
# This file evaluates legal moves with heuristic scoring and can optionally
# apply a simple depth-2 adjustment callback.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psai.decision.heuristic import HeuristicWeights, ReasonBreakdown, score_action
from psai.domain.state import LegalAction, State
from psai.mechanics.api import ActionOutcome, MechanicsAPI


def get_opponent_response_adjustment(
    state: State,
    action: LegalAction,
    outcome: ActionOutcome,
) -> float:

    # We want to estimate the opponents OPTIMAL response to our action,
    # looking two turns ahead. OPTIMAL is important here, i know theres a specific name
    # for it, but the type of algo we use will be based on the idea that the opponent will
    # choose the best possible move. Worth noting however, in pokemon there are mindgames
    # partiuclarly in switching, so we might see the "optimal" move being the most damage
    # and then get suprised by the opponent doing something else we deem unoptimal but is 
    # still better. I am betting this will be the main cause of our agent losing to people. 

    weights = HeuristicWeights()
    incoming_ko_penalty = weights.self_ko_penalty * outcome.ko_probability_to_self
    incoming_damage_penalty = weights.incoming_damage_weight * outcome.expected_damage_to_self
    tempo_penalty = 0.0 if outcome.move_first else (weights.move_first_bonus * 0.5)

    return -(incoming_ko_penalty + incoming_damage_penalty + tempo_penalty)


@dataclass(slots=True)
# This could just be two variables, but keeping to the theme, we put it in a dataclass for neatness.
# Also, if ever need to add more specifics, can just add to the dataclass and access it from there. 
class SearchConfig:

    depth: int = 1 # Howw deep we search down the tree of possible moves and actions.
    top_k: int = 3 # How many of the top moves to keep. (maybe make this universal? we also set in main)


@dataclass(slots=True)
# This is just a box for holding one move after it is evaluated. (ie, how was using quickattack here socred?)
class ScoredAction:

    action: LegalAction  # which move is being scored
    outcome: ActionOutcome # mechanics engine output for the move
    score: float # score! from heuristics. or model later? confusing for now, but will learn later
    breakdown: ReasonBreakdown # reasons, these are the top 4 (default) terms for the actions score.


def rank_actions(
    state: State, 
    mechanics: MechanicsAPI,
    *,
    weights: HeuristicWeights | None = None,
    config: SearchConfig | None = None, # 1 of 2 new args here not mentioned in other comments, this is the config from above
    opponent_response_fn: Any = None, # the estimated optimal moves
) -> list[ScoredAction]:

    # This is the big function for the search file. 

    cfg = config or SearchConfig() # use given for testing, or default specified above
    non_switch_actions = [action for action in state.legal_actions if not action.is_switch]
    candidates = non_switch_actions or list(state.legal_actions)
    scored: list[ScoredAction] = [] # list of scores for each move

    for action in candidates: # for each possible move, determine the outcome and score with heuristics!
        outcome = mechanics.evaluate_action(state, action) # mechanics engine output for the move
        base_score, breakdown = score_action(state, action, outcome, weights=weights) # score
        
        # Here is where we test our heurisitic further, by doing a search algo and checking future
        # moves and how they effect our scores and mechanics engine results. Later this will be 
        # replaced with the actual learning model, but I havent even BEGUN to add that, so this will do.
        if cfg.depth >= 2: # if depth is 2 or more, we look ahead at the opponents response
            response_adjustment_fn = opponent_response_fn or get_opponent_response_adjustment
            depth2_adjustment = float(response_adjustment_fn(state, action, outcome))
            breakdown = dict(breakdown) # get the top terms from our score
            breakdown["depth2_adjustment"] = depth2_adjustment # Now we add the adjusted score for that state based on the opponents response. 

            base_score += depth2_adjustment

        scored.append(ScoredAction(action=action, outcome=outcome, score=base_score, breakdown=breakdown))

    scored.sort(key=lambda entry: (entry.score, entry.action.move_name), reverse=True)
    return scored[: max(0, cfg.top_k)]
