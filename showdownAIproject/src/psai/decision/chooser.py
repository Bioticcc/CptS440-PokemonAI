"""Top-level move suggestion orchestrator."""

# Plain-English summary:
# This is the decision entrypoint. It asks search to rank legal moves,
# optionally applies model adjustments, and returns ranked suggestions.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from psai.decision.heuristic import HeuristicWeights, format_reason_breakdown
from psai.decision.search import ScoredAction, SearchConfig, rank_actions
from psai.domain.state import LegalAction, State
from psai.mechanics.api import MechanicsAPI

ModelBonusFn = Callable[[State, LegalAction], float]


@dataclass(slots=True)
# This holds a move suggestion, its rank, and additional info.
class MoveSuggestion:
    """Simple move suggestion payload for UI and logs."""

    rank: int # Rank of the move. rank 1 is the best move, rank 2 is second best, etc.
    action: LegalAction # name of move
    score: float # score of the move from heuristics
    reasons: list[str] # heuristic breakdown, (ie, top 4 terms and their values)
    breakdown: dict[str, float] # breakdown. reasons is just this but prettier. can remove reasons if prefer technical.


def get_ranked_actions(
    state: State,
    mechanics: MechanicsAPI,
    *,
    weights: HeuristicWeights | None = None,
    search_config: SearchConfig | None = None,
    opponent_response_fn: Any = None,
) -> list[ScoredAction]:

    # Okay, this is where we get the ranked moves. You pass in the arguments above, 
    # and return a list of scored actions, sorted by rank. How you do this is up to you, 
    # but it should involve search.py and heuristic.py as its methods of decision making.

    return rank_actions(
        state,
        mechanics,
        weights=weights,
        config=search_config,
        opponent_response_fn=opponent_response_fn,
    )


def apply_model_bonus(
    ranked_actions: list[ScoredAction],
    *,
    state: State,
    model: ModelBonusFn | None = None,
) -> list[ScoredAction]:

    # Once the model is implemented, here you want to apply that to our ranked actions. 
    # essentially, we use heuristics to get a bunch of neat, ranked moves, which we will then give to model
    # who will then adjust based on our priority value search algorithm. returns updated ranked actions. 

    if model is None:
        return ranked_actions

    adjusted_actions: list[ScoredAction] = []
    for scored_action in ranked_actions:
        bonus = float(model(state, scored_action.action))

        # as a reminder, breakdown for a scored action is a dict of heurisitic rules to the value,
        # so we can see precisely what is being changed.
        updated_breakdown = dict(scored_action.breakdown)
        updated_breakdown["model_bonus"] = bonus
        adjusted_actions.append(
            ScoredAction(
                action=scored_action.action,
                outcome=scored_action.outcome,
                score=scored_action.score + bonus,
                breakdown=updated_breakdown,
            )
        )

    # sort with one liner, first by score then by move name for tie breaking in descending order.
    adjusted_actions.sort(key=lambda entry: (entry.score, entry.action.move_name), reverse=True)
    return adjusted_actions


def build_move_suggestions(
    ranked_actions: list[ScoredAction],
    *,
    top_k: int = 3,
) -> list[MoveSuggestion]:
    
    suggestions: list[MoveSuggestion] = []
    
    # Here we take our ranked actions from the previous functions, and convert them into MoveSuggestion objects,
    # which we defined at the top of the page. Return a list of said opjects, keeping only the top_k options.

    selected_actions = ranked_actions[:top_k]
    
    # using enumerate here as just the same thing as rank_index = 1, rank_index += 1 at end of loop.
    # this is just good python convention, so here we go. 
    for rank_index, scored_action in enumerate(selected_actions, start=1):
        suggestions.append(
            MoveSuggestion(
                rank=rank_index,
                action=scored_action.action,
                score=scored_action.score,
                reasons=format_reason_breakdown(scored_action.breakdown),
                breakdown=dict(scored_action.breakdown),
            )
        )

    return suggestions


def choose_actions(
    state: State,
    mechanics: MechanicsAPI,
    *,
    top_k: int = 3,
    weights: HeuristicWeights | None = None,
    search_config: SearchConfig | None = None,
    opponent_response_fn: Any = None,
    model: ModelBonusFn | None = None,
) -> list[MoveSuggestion]:
    
    # This is the main function of the chooser file. Doesnt need anything else right now, 
    # as it just calls the helper functions you made above. Will need additional error handling
    # and specifics once we get the model stuff going proper, so keep an eye on it. 
    
    effective_search_config = search_config or SearchConfig(top_k=top_k)

    ranked_actions = get_ranked_actions(
        state,
        mechanics,
        weights=weights,
        search_config=effective_search_config,
        opponent_response_fn=opponent_response_fn,
    )
    ranked_actions = apply_model_bonus(ranked_actions, state=state, model=model)
    return build_move_suggestions(ranked_actions, top_k=top_k)
