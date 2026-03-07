"""Dataset definitions bridging runtime logs to training examples.

This module should define how battle states, chosen actions, and outcomes are
recorded and transformed into tensors/examples for supervised or RL-style
training. It is the data contract between runtime decision logs and model code.
"""

# Plain-English summary:
# This file turns battle logs into model-ready training examples.
# It should define how states, actions, and results get saved and loaded.
