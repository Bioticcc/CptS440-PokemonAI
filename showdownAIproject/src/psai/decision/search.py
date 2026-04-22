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



def _opponent_can_switch(state: State) -> bool:
    
    # Guard for wether the opponent is capable of switching, we need this later for prediticin
    # optimal move from opponent, below.
    
    opponent_active_identifier = str(state.opponent_active.identifier or "").strip()
    opponent_active_species = str(state.opponent_active.species or "").strip().lower()

    for candidate in state.opponent_team:
        if candidate.fainted or float(candidate.hp_fraction) <= 0.0:
            continue

        candidate_identifier = str(candidate.identifier or "").strip()
        if candidate_identifier and opponent_active_identifier:
            if candidate_identifier == opponent_active_identifier:
                continue
            return True

        candidate_species = str(candidate.species or "").strip().lower()
        if candidate_species and candidate_species != opponent_active_species:
            return True

    return False


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

    ko_to_self = max(0.0, min(1.0, float(outcome.ko_probability_to_self)))
    incoming_damage = max(0.0, min(1.0, float(outcome.expected_damage_to_self)))
    ko_to_opponent = max(0.0, min(1.0, float(outcome.ko_probability_to_opponent)))
    outgoing_damage = max(0.0, min(1.0, float(outcome.expected_damage_to_opponent)))

    # Opponent move-response pressure derived strictly from existing mechanics terms.
    move_penalty = (
        (weights.self_ko_penalty * ko_to_self)
        + (weights.incoming_damage_weight * incoming_damage)
        + (0.0 if outcome.move_first else (weights.move_first_bonus * 0.5))
    )

    # Approximate probability that opponent prefers staying in and attacking.
    move_weight = 1.0 + (1.2 * ko_to_self) + (0.8 * incoming_damage)
    if not outcome.move_first:
        move_weight += 0.2

    # Approximate probability that opponent switches to avoid our pressure.
    switch_weight = 0.0
    if _opponent_can_switch(state):
        action_threat = (0.7 * ko_to_opponent) + (0.3 * outgoing_damage)
        switch_weight = max(0.0, min(1.0, action_threat))

    total_weight = move_weight + switch_weight
    if total_weight <= 0.0:
        return -move_penalty

    move_probability = move_weight / total_weight
    switch_probability = switch_weight / total_weight

    switch_penalty_scale = 0.25 if action.is_switch else 1.0
    switch_penalty = (
        (weights.ko_now_bonus * ko_to_opponent * 0.65 * switch_penalty_scale)
        + (weights.damage_weight * outgoing_damage * 0.45 * switch_penalty_scale)
        + (weights.move_first_bonus * 0.2)
    )

    expected_penalty = (move_probability * move_penalty) + (switch_probability * switch_penalty)
    return -expected_penalty


@dataclass(slots=True)
# This could just be two variables, but keeping to the theme, we put it in a dataclass for neatness.
# Also, if ever need to add more specifics, can just add to the dataclass and access it from there. 
class SearchConfig:

    depth: int = 3 # Howw deep we search down the tree of possible moves and actions.
    depth_decay: float = 0.55 # diminishing influence for ply>2 continuation approximation.
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

            total_depth_adjustment = depth2_adjustment
            resolved_depth = max(2, int(cfg.depth))
            resolved_decay = max(0.0, min(1.0, float(cfg.depth_decay)))

            # Continuation approximation: for deeper plies, keep applying a
            # discounted copy of the opponent-response pressure.
            # This is not full tree search, but gives depth > 2 meaningful behavior.
            if resolved_depth > 2 and resolved_decay > 0.0:
                for ply in range(3, resolved_depth + 1):
                    ply_adjustment = depth2_adjustment * (resolved_decay ** (ply - 2))
                    breakdown[f"depth{ply}_adjustment"] = ply_adjustment
                    total_depth_adjustment += ply_adjustment

            base_score += total_depth_adjustment

        scored.append(ScoredAction(action=action, outcome=outcome, score=base_score, breakdown=breakdown))

    scored.sort(key=lambda entry: (entry.score, entry.action.move_name), reverse=True)
    return scored[: max(0, cfg.top_k)]
