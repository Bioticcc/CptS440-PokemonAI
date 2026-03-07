"""Runtime entrypoint for the poke-env + human-confirmation loop.

This module should coordinate the end-to-end flow:
- run/connect the poke-env Showdown player
- receive live battle objects each decision turn
- convert battle objects into internal `State`
- call decision orchestration for ranked suggestions + reasoning
- present suggestions in the UI/control panel
- receive human-confirmed move choice
- send the confirmed move back through poke-env to Showdown
"""

# Plain-English summary:
# This is the main runtime flow.
# It gets live battle info, asks the decision system for best moves, shows the
# suggestions to the human, and sends the human-approved move through poke-env.
