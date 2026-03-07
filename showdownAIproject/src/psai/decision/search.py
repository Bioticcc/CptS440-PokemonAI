"""Shallow search over candidate actions.

This module should evaluate short-horizon action outcomes using deterministic
mechanics results plus heuristic state/action evaluation. Phase 1 focuses on
simple, reliable lookahead for mostly 1v1 play; later versions may incorporate
model-guided search priors.
"""

# Plain-English summary:
# This file looks a little ahead at possible next turns.
# It compares candidate moves by combining mechanics outcomes with heuristic
# scoring, so we do better than picking from one-step intuition alone.
