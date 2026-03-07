"""Hand-crafted Phase 1 baseline evaluation logic.

This module should score states/actions with transparent rules, for example:
- KO now opportunities
- avoiding immediate KO risk
- expected damage pressure
- move-order/speed advantage
- reliability/risk tradeoffs

Heuristic scoring should consume mechanics outputs instead of reimplementing
battle rules. This is the initial non-learned baseline.
"""

# Plain-English summary:
# This file contains the first rule-based "common sense" scorer.
# It should reward strong moves (like likely KOs) and punish risky choices
# (like getting KO'd back), using numbers from the mechanics layer.
