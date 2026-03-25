"""Top-level move suggestion orchestrator."""

# Plain-English summary:
# This is the decision entrypoint. It asks search to rank legal moves,
# optionally applies model adjustments, and returns ranked suggestions.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psai.decision.heuristic import HeuristicWeights, format_reason_breakdown
from psai.decision.search import ScoredAction, SearchConfig, rank_actions
from psai.domain.state import LegalAction, State
from psai.mechanics.api import MechanicsAPI


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
    model: Any = None,
) -> list[ScoredAction]:

    # Once the model is implemented, here you want to apply that to our ranked actions. 
    # essentially, we use heuristics to get a bunch of neat, ranked moves, which we will then give to model
    # who will then adjust based on our priority value search algorithm. returns updated ranked actions. 

    return ranked_actions


def build_move_suggestions(
    ranked_actions: list[ScoredAction],
    *,
    top_k: int = 3,
) -> list[MoveSuggestion]:
    
    suggestions: list[MoveSuggestion] = []
    
    # Here we take our ranked actions from the previous functions, and convert them into MoveSuggestion objects,
    # which we defined at the top of the page. Return a list of said opjects, keeping only the top_k options.

    return suggestions


def choose_actions(
    state: State,
    mechanics: MechanicsAPI,
    *,
    top_k: int = 3,
    weights: HeuristicWeights | None = None,
    search_config: SearchConfig | None = None,
    opponent_response_fn: Any = None,
    model: Any = None,
) -> list[MoveSuggestion]:
    
    # This is the main function of the chooser file. Doesnt need anything else right now, 
    # as it just calls the helper functions you made above. Will need additional error handling
    # and specifics once we get the model stuff going proper, so keep an eye on it. 
    
    ranked_actions = get_ranked_actions(
        state,
        mechanics,
        weights=weights,
        search_config=search_config,
        opponent_response_fn=opponent_response_fn,
    )
    ranked_actions = apply_model_bonus(ranked_actions, state=state, model=model)
    return build_move_suggestions(ranked_actions, top_k=top_k)
