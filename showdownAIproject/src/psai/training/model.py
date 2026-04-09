"""Policy+value neural model definitions for the learning system."""

# Plain-English summary:
# This file defines the neural network that predicts move probabilities
# and a scalar value for the current state.

from __future__ import annotations

from typing import Sequence

import torch # BEHOLD! the ai model import. :P
from torch import Tensor, nn

# ALRIGHT, so this is the model file. It mainly defines the shape of the models "brain",
# but doenst do the heavy lifting quite yet. 
# The goal of this file is to take those numeric battle states we made in dataset.py,
# and output which action slots look best (policy) and how good the state looks overall (value).

class PolicyValueMLP(nn.Module): # makes the neural network model class

    # This is the model class. More explanation later.

    def __init__(
        self,
        *,
        input_dim: int, # how many numbers come in per state object. numerical size of state 
        hidden_sizes: Sequence[int] = (128, 64), # 
        action_dim: int = 4,
    ) -> None:
        super().__init__()
        self.input_dim = int(input_dim)
        self.action_dim = int(action_dim)

        layers: list[nn.Module] = [] # holds the pytorch layer objects
        previous_dim = self.input_dim # current feature width so each new Linear knows input/output sizes
        for hidden_dim in hidden_sizes:
            layers.append(nn.Linear(previous_dim, int(hidden_dim)))
            layers.append(nn.ReLU())
            previous_dim = int(hidden_dim)

        # These are the 3 main parts of the model:
        # 1. trunk: shared feature extractor (general thinking)
        # 2. policy_head: which move slot looks best
        # 3. value_head: how good is this position overall
        self.trunk = nn.Sequential(*layers) if layers else nn.Identity()
        self.policy_head = nn.Linear(previous_dim, self.action_dim)
        self.value_head = nn.Linear(previous_dim, 1)

    def forward(self, state_tensor: Tensor, action_mask: Tensor | None = None) -> tuple[Tensor, Tensor]:
        
        # This is just the function that takes in the state, and outputs the policy logits and value.

        features = self.trunk(state_tensor) 
        policy_logits = self.policy_head(features) # raw scores for each action slot

        if action_mask is not None: # this is how we use the mask to get rid of illegal actions
            invalid = action_mask <= 0
            policy_logits = policy_logits.masked_fill(invalid, -1e9) # SUPER negative value, so never chosen

        value = torch.tanh(self.value_head(features)).squeeze(-1)
        return policy_logits, value

    @torch.no_grad()
    def predict(self, state_tensor: Tensor, action_mask: Tensor | None = None) -> tuple[Tensor, Tensor]:

        # main function for the final, calls forward and turns policy logits into probabilities instead.

        logits, value = self.forward(state_tensor, action_mask)
        probabilities = torch.softmax(logits, dim=-1)
        return probabilities, value
