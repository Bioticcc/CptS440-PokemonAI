"""Top-level decision orchestrator for move suggestions.

This module should combine:
- current internal `State`
- deterministic mechanics outputs
- Phase 1 heuristic + shallow search ranking
- later policy/value model guidance

Expected output is ranked move suggestions with structured reasoning suitable
for display in the UI/control panel before human confirmation.
"""

# Plain-English summary:
# This is the "traffic controller" for move suggestions.
# It takes the current state, asks helper modules for scores/results, and
# returns a ranked list of moves plus clear reasons for each recommendation.
