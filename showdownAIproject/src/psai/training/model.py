"""Policy+value neural model definitions.

Expected starting point is an MLP that consumes encoded internal state features
and produces:
- a policy distribution over legal actions
- a value estimate for the current state
"""

# Plain-English summary:
# This file defines the neural network shape.
# The model should output:
# 1) which actions look promising, and
# 2) how good the current state likely is.
