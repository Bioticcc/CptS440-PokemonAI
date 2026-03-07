# TO DO 3/6/2026

## POKE ENV INPUTS
So currently we have an issue with taking in the input battle data from the live game
We have two paths we can take at the moment

 1. Live battle in our browser, that we connect to our agent, it reads the state, and outputs a chat message of the optimal moves.
    - The agent is NOT playing the game, just giving suggestions.

 2. Poke-env showdown battle, where the poke-env agent that we train is the one inputting the moves, but instead of letting us manually click moves in showdown, we choose the moves directly in the python client, and poke-env is the one making the move.
    - The agent IS playing the game, we just tell it what input to do.

## POKE ENV AGENT 
Using the Poke-env agent to play for us, we have an updated list of things we need the agent to do:



    