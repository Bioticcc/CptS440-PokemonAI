"""Model training loop and experiment plumbing.

This module should handle dataset loading, optimization steps, metrics, and
checkpointing. Initial training data comes from logged baseline heuristic games;
later iterations may add self-play generated data.
"""

# Plain-English summary:
# This file runs model training from start to finish:
# load data, train for many steps, track metrics, and save checkpoints.
