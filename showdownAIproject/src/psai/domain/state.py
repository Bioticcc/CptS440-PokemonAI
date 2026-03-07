"""Normalized internal battle-state representation.

This module should define the `State` contract consumed by decision, mechanics,
and training code. The representation should stay independent from raw
`poke-env` battle objects so parser/runtime details do not leak into core logic.

Implementation focus:
- represent active friendly/enemy Pokemon and known team context
- track revealed opponent information and uncertainty for hidden details
- include legal actions, statuses, boosts, and turn-critical flags
- remain extensible for later support beyond Phase 1 (switching/full-team play)
"""

# Plain-English summary:
# This file is where we define our own clean battle snapshot.
# It should store what is happening right now in a way every subsystem can use,
# without tying the rest of the project to raw poke-env objects.
