# Alright, first and foremost we need to confirm weve got the required installs ready:

## Run the following in your VS code bash terminal:
- python3 --version
    - If this gives an error, run: sudo apt update sudo apt install -y python3 python3-venv python3-pip
- git --version

## run these in ~/CptS440-PokemonAI/showdownAIproject$
- python3 -m venv .venv
- source .venv/bin/activate 

## run these in (.venv) 
- python -m pip install --upgrade pip wheel setuptools
- pip install -e . 
    - This now installs the key runtime deps from `pyproject.toml`, including `poke-env` and `torch`.

# ----------------------------------------------
HOW TO RUN: 
cd /home/jeezu/CptS440-PokemonAI/showdownAIproject
source .venv/bin/activate
pip install -e .   
python3 -m psai.app.main
# ----------------------------------------------

# GIT CHANGES:
## PUSH
cd /home/jeezu/CptS440-PokemonAI
git checkout main
git status
git add -u
git commit -m "Bot automatically turns on battle timer now! will stop timeouts"
git push origin main
git rev-parse --short HEAD
## PULL
cd /home/kyle/Desktop/Adam/CptS440-PokemonAI
git checkout main
git status
git pull --ff-only origin main
git rev-parse --short HEAD
git rev-parse --short origin/main

cd showdownAIproject
source .venv/bin/activate
pip install -e .

# THEN RUN WITH:
python3 -m psai.app.main

# INVALID RUN? DELETE ALL LOGS: (THIS MEANS ALL. IF YOU WANT SPECIFIC, DO SPECIFIC.)
rm -f training/battle_logs.jsonl
rm -rf training/artifacts
mkdir -p training/artifacts/checkpoints training/artifacts/metrics


## SPECIFIC ROLES (choose one of these you want for now)
- Showdown Integration and State Parser
    - What you need to do here is connect to showdown through poke-env (the agent itself is the actual showdown player/client), receive the live battle object each turn, and have a State object class that is updated every turn. State will have parameters like HP%, status, boosts, types, moves (for the enemy pokemon, only the ones that we have seen so far), item/ability if we know it.
    - We should have a State for friendlyPokemon and enemyPokemon, and this will update with each turn. 
    - track the different pokemon we have seen so far. Save a list of the existing pokemon in that battle.
    - Coordinate with app/UI so the human-confirmed move can be passed back through poke-env to showdown each turn.
    - DONE IF: given a live showdown battle through poke-env, we can output a "state" showing the above information for each turn, and pass the confirmed move back through poke-env. 

- Mechanics Engine
    - This is the math engine essentially, that determines exactly what will happen if the selected move/choice is made. 
    - should output things like KO_probability which that uses the existing formulas to determine the % chance that a given move will kill the defender. 
    - other outputs would be things like move_order which just says who would attack first given each choice, and damage_done(attacker, defender, move, state)
    - Should match showdowns rules EXACTLY. this is where we could import that github repo mentioned in the essay to get the type tables and things like that.
    - DONE IF: we give it two pokemon and a move, we can output the expected damage and % chance it kills the defender. 

- Heuristic Search 1v1
    - This is the baseline 1v1 model (no machine learning, this one is just rules), which will use the heuristic scoring to decide moves.
        - Essentially, we want a function score(state) that can output a number that acts as the numeric "score" that state has. So a great state would be full health, type advantage and the enemy pokemon is almost dead. The higher the score, the better the state. 
        - We need to define the heuristtic terms, or the things that are actually checked when decided a score. Example scoring would be something like 
        if (ko_this_turn == true) score + 1000, if (get_koed_next_turn == true) score - 1000,
        score += 10 * expected_damage_percent, if (we_move_first == true) score + 100. 
        - for more examples, if (KO_probability >= 0.95) ko_this_turn == true. These probability and damage numbers would be coming from the mechanics engine. 
    - Once we have the ruleset, we would make a shallow search (similair thing to the one we use in the larger model) that will look 1-2 moves ahead, that will output the top 1-2 moves we could make based on the various different states those moves would generate.
    - output log, so we can see "in state x, model chose y" 
    - DONE IF: given a showdown 1v1, it consistently chooses decet moves and doesnt make obvious mistakes.

- Learning System
    - This is the "main" model. This is the policy+value network that we will train with the output logs from the heuristic 1v1's. 
    - Implement the actual MLP network. 
    - Build the training pipline, like dataset formats, and training metrics. 
    - Should be able to run self playing to let it keep learning.
    - DONE IF: Given the battle logs from the heuristic 1v1, it can then learn from these logs and essentially begin to decide and edit the manually made heuristic ruleset via training and studying the outcomes.

- Decision Engine
    - This is combining the mechanics engine with the learning system, essentially making all our different pieces work together. It should use the MLP policy to make a short list of even remotely decent moves/actions, then use the search to see the possible outcomes of those actions, then value to determine which of the possible search trees is our best bet and follows the next move in that tree before rerunning for the next step. 
    DONE IF: outputs a more structured output of the top 3 moves, and why. (MATHEMATICALLY, rather then as an LLM. I dont think we need that, but hey if someone wants to make this have an LLM included feel free as long as youre sure it would mess with things. )

- Overlay and Packaging
    - This is what turns all this other stuff into an actual downloadable application. needs art, needs to display the top moves and reasoning in a nice clean local UI/control panel for the poke-env controlled battle. Final step, but I think you can build on it from the getgo, just keeping in mind future needs. 
    - Needs proper packaging, so maybe something like Electron or Tauri. 
    - VERY IMPORTANT THAT WHOEVERS DOING THIS COORDINATES WITH THE INPUTS ROLE. The UI/control panel and the showdown integration have to work together.
    - DONE IF: We can run a showdown 1v1 through poke-env, open the control panel, get top 3 move suggestions with reasons, confirm a move, and have that move sent to showdown. 


## PRIMARY FILES TO WORK IN: 
- Showdown Integration and State Parser:
   - src/psai/domain/
   - src/psai/app/

- Mechanics Engine:
   - src/psai/mechanics/

- Heuristic Search 1v1:
   - src/psai/decision/
    
- Learning System:
   - src/psai/training/

- Decision Engine:
    - src/psai/decision/

- Overlay and Packaging:
   - src/psai/app/


## CURRENT FILE STRUCTURE EXPLANATION:
What we currently have:



First, domain/. This is just where we ask "what is the current game state" and output the answer. Here, State Parser converts the live poke-env battle object into our internal State for other sections that use that outputed data for calculations.

Mechanics/ is the folder where we decide "what happens if we do x move". If we use the prexisting engine, we use api.py to do it.

Decision/ is "given current state, what move do we pick". Initially, we use heuristic.py and search.py to do the ruleset based decision making, then later use the data from training/ in decision.py to make the more complex predictions on moves. 

Training/ is where we actually "teach" the policy value network with the output logs we got from the baseline heuristic 1v1's. This is where the actual machine learning happens.

In app/, we create the actual local UI/control panel and move confirmation flow. Right now, `main.py` is where ladder battle orchestration lives: manual product battle (`run_battle`) and unified auto ladder data collection (`run_training_battle(..., source=\"heuristic\"|\"model\")`). Connection/recovery helpers live in `app/connections.py`.

So our "pipeline" is as such:

1. poke-env agent receives a live showdown battle object when a move is requested
2. State Parser converts battle into our internal State object
3. Decision chooser.py is called with the created State
4. chooser.py uses:
    - mechanics/api.py
    - decision/heuristic.py
    - decision/search.py
    - and later the learning model in training/model.py
5. system returns top ranked move suggestions and reasoning
6. UI/control panel displays these suggestions to the human
7. human confirms which move to use
8. confirmed move is passed back to the poke-env agent
9. poke-env sends that move to showdown


## TRAINING CYCLE STABILITY NOTE 
- Current status: the training cycle is actively working in long ladder runs.
- Do NOT change training-cycle behavior unless there is a specific, reproducible runtime error.
- Training-cycle behavior includes:
    - `run_training_cycle(...)` orchestration flow
    - `run_training_battle(..., source="heuristic")` data-collection flow
    - `run_training_battle(..., source="model")` data-collection flow
- If an error occurs, apply the smallest targeted bugfix only, then rerun and verify.
- Avoid refactors, architecture moves, or “cleanup” edits to cycle logic during stable runs.

## CURRENT CODE STATUS
- `src/psai/app/main.py`
    - Has `pokeEnvPlayerInfo` player subclass with inline account/format config in its constructor.
    - Ladder connection runner and network guards are imported from `src/psai/app/connections.py`.
    - Has 2 battle runners:
        - `run_battle(...)` = product/manual pipeline (state -> chooser -> human confirm -> send order)
        - `run_training_battle(..., source="heuristic"|"model")` = auto ladder collection for both phases (model bonus optional based on source)
    - Training collection loop and JSONL logging are unified in `run_training_battle(...)`.
    - `main()` currently runs the full training orchestrator (`run_training_cycle(...)`) with:
        - `bootstrap_decisions=20_000`
        - `model_cycle_decisions=10_000`
        - `eval_games=100`
        - `max_cycles=1`
      and prints `Training status: ...`.

- `src/psai/app/connections.py`
    - Owns `AsyncConnectionRunner` and network/recovery helpers (`_resolve_runner_state`, reconnect/requeue, and finished-battle cleanup).

- `src/psai/training/train.py`
    - `TrainConfig` + `train_policy_value(...)` handle optimization/checkpointing.
    - `TrainingLoopConfig` controls orchestration (`bootstrap_decisions`, optional `heuristic_refresh_decisions`, `model_cycle_decisions`, eval gate, etc.).
    - `run_training_cycle(...)` does:
        1. resume-aware heuristic bootstrap to target count,
        2. model train on accumulated logs,
        3. model-play data collection,
        4. inline ladder evaluation,
        5. winrate gate stop/continue logic.
    - Saves per-cycle artifacts:
        - checkpoints: `training/artifacts/checkpoints/`
        - metrics: `training/artifacts/metrics/`
        - best pointer: `training/artifacts/best_model.json`

- `src/psai/decision/chooser.py`
    - Chooser path is fully wired: search ranking -> optional model bonus -> top-k `MoveSuggestion`.
    - Model bonus path is callable-based: `model(state, action) -> float`.

- `src/psai/decision/search.py`
    - Scores actions from `state.legal_actions` with depth-1 or depth-2 adjustment.
    - Uses non-switch actions first, with fallback to legal actions list if needed.

- `src/psai/mechanics/api.py`
    - `MechanicsAPI.evaluate_action(...)` builds `ActionOutcome` from helper functions.
    - Uses poke-env gen1/2 damage calc (`calculate_damage_gen12`) for expected damage and KO estimates.

- `src/psai/domain/state.py`
    - Core dataclasses exist (`PokemonSnapshot`, `LegalAction`, `State`).
    - `parse_battle_to_state(...)` exists.
    - Some parser detail helpers are still partial and should be completed/refined for stronger training quality.

- Running modes in `main.py`:
    - Product/manual usage: uncomment `run_battle(...)`.
    - Full training loop usage: keep `run_training_cycle(...)` call uncommented.
