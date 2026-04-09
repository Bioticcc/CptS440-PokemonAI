"""Runtime scaffold for poke-env battle intake and decision handoff."""

# Plain-English summary:
# This module provides a battle-loop scaffold: get battle objects,
# parse each into State, run chooser, and print move suggestions.

from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time
from typing import Any

from poke_env import AccountConfiguration, ShowdownServerConfiguration
from poke_env.player import Player

from psai.decision.chooser import ModelBonusFn, MoveSuggestion, choose_actions
from psai.domain.state import State, parse_battle_to_state
from psai.mechanics.api import MechanicsAPI
from psai.training.dataset import make_log_record, write_log_record
from psai.training.train import TrainingLoopConfig, run_training_cycle


class AsyncConnectionRunner:

    # Keeps showdown ladder connection running in background while other loop logic runs.

    def __init__(self, player: Any, n_games: int | None = 1) -> None:
        self._player = player
        self._n_games = None if n_games is None else int(n_games)
        if self._n_games is not None and self._n_games <= 0:
            raise ValueError("n_games must be positive or None")
        self._done_event = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            asyncio.run(self._run_coro())
        except BaseException as exc:  # pragma: no cover - defensive bridge from thread
            self._error = exc
        finally:
            self._done_event.set()

    async def _run_coro(self) -> None:
        games = self._n_games or 1
        await self._player.ladder(games)

    def start(self) -> "AsyncConnectionRunner":
        self._thread.start()
        return self

    @property
    def done(self) -> bool:
        return self._done_event.is_set()

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("connection runner failed") from self._error


def get_battle(player):

    # Use poke-env to get the current battle object with our created 'player'.

    battles = getattr(player, "battles", None)

    if not battles:
        return None

    for battle in battles.values():
        if not getattr(battle, "finished", False):
            return battle

    try:
        return next(iter(battles.values()))
    except StopIteration:
        return None


def get_state(battle):

    # Parse current battle information into a State object.

    return parse_battle_to_state(battle)


class pokeEnvPlayerInfo(Player):

    # PLAYER CLASS! This is what lets us connect to showdown, send moves, etc.

    def __init__(
        self,
        username: str = "PokeLearn440",
        password: str = "CPTS440",
        battle_format: str = "gen1randombattle",
        team: str | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.battle_format = battle_format
        self.team = team

        account_configuration = AccountConfiguration(username, password)
        player_kwargs: dict[str, Any] = {
            "account_configuration": account_configuration,
            "server_configuration": ShowdownServerConfiguration,
            "battle_format": battle_format,
        }
        if team:
            player_kwargs["team"] = team

        super().__init__(**player_kwargs)
        self._pending_orders: dict[str, Any] = {}

    # Store pending move actions, then when prompted by showdown return that action.
    def set_pending_order(self, battle_tag: str, order: Any) -> None:
        self._pending_orders[battle_tag] = order

    def choose_move(self, battle):
        battle_tag = getattr(battle, "battle_tag", "")

        while True:
            pending_order = self._pending_orders.pop(battle_tag, None)
            if pending_order is not None:
                return pending_order
            time.sleep(0.1)


def get_turn_suggestions(
    state: State,
    mechanics: MechanicsAPI,
    *,
    top_k: int = 3,
    model: ModelBonusFn | None = None,
) -> list[MoveSuggestion]:

    suggestions = choose_actions(state, mechanics, top_k=top_k, model=model)
    return suggestions


def get_user_choice(turn_suggestions: list[MoveSuggestion], battle: Any) -> Any:

    # UI-heavy area for manual selection.

    del turn_suggestions
    available_moves = list(battle.available_moves)
    available_switches = list(battle.available_switches)

    print("What would you like to do?")
    print("1. Attack")
    print("2. Switch")
    first_choice = int(input("Choose 1 or 2: ").strip())

    if first_choice == 1:
        print("Choose move:")
        for index, move in enumerate(available_moves, start=1):
            move_name = getattr(move, "id", None) or getattr(move, "move", None) or f"Move_{index}"
            print(f"{index}. {move_name}")
        move_choice = int(input("Choose move number: ").strip())
        return {"kind": "attack", "index": move_choice}

    print("Switch to:")
    for index, pokemon in enumerate(available_switches, start=1):
        poke_name = getattr(pokemon, "species", None) or getattr(pokemon, "name", None) or f"Poke_{index}"
        print(f"{index}. {poke_name}")
    switch_choice = int(input("Choose switch number: ").strip())
    return {"kind": "switch", "index": switch_choice}


def send_confirmed_move(player: Any, battle: Any, chosen_action: Any) -> Any:

    action_kind = chosen_action["kind"]
    action_index = int(chosen_action["index"])

    if action_kind == "attack":
        selected_move = list(battle.available_moves)[action_index - 1]
        return player.create_order(selected_move)

    selected_switch = list(battle.available_switches)[action_index - 1]
    return player.create_order(selected_switch)


def _safe_reset_battles(player: Any) -> None:
    reset_fn = getattr(player, "reset_battles", None)
    if not callable(reset_fn):
        return
    try:
        reset_fn()
    except Exception:
        return


def _battle_outcome_value(battle: Any) -> tuple[float, str]:
    won = getattr(battle, "won", None)
    if won is True:
        return 1.0, "win"
    if won is False:
        return -1.0, "loss"
    return 0.0, "tie"


def _launch_single_game(player: Any) -> AsyncConnectionRunner:
    return AsyncConnectionRunner(player, 1).start()


def run_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    top_k: int = 3,
    model: ModelBonusFn | None = None,
    max_turns: int | None = None,
) -> None:

    # Main product/manual suggestion flow.

    turns_ran = 0

    while True:
        battle = get_battle(player)

        if battle is None:
            break

        if getattr(battle, "finished", False):
            break

        state = get_state(battle)
        turn_suggestions = get_turn_suggestions(state, mechanics, top_k=top_k, model=model)

        for suggestion in turn_suggestions:
            print(
                f"#{suggestion.rank} {suggestion.action.move_name} "
                f"(score={suggestion.score:.2f})"
            )
            for reason in suggestion.reasons:
                print(f"  - {reason}")

        chosen_action = get_user_choice(turn_suggestions, battle)
        chosen_order = send_confirmed_move(player, battle, chosen_action)

        if hasattr(player, "set_pending_order"):
            player.set_pending_order(getattr(battle, "battle_tag", ""), chosen_order)

        turns_ran += 1
        if max_turns is not None and turns_ran >= max_turns:
            break

        time.sleep(0.1)


def run_test_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI | None = None,
    top_k: int = 1,
    max_turns: int | None = None,
    n_games: int | None = 1,
) -> None:

    # Ladder connectivity smoke test:
    # starts ladder game(s) and auto-plays with heuristic choices only.
    # No training logs are written.

    _safe_reset_battles(player)
    resolved_mechanics = mechanics or MechanicsAPI()
    runner = AsyncConnectionRunner(player, n_games).start()

    turns_ran = 0
    last_prompted_turn: dict[str, int] = {}
    finished_tags: set[str] = set()

    while True:
        if runner.done:
            runner.raise_if_failed()

        battles = dict(getattr(player, "battles", {}) or {})
        active_battles = []
        for battle_tag, battle in battles.items():
            if getattr(battle, "finished", False):
                if battle_tag not in finished_tags:
                    finished_tags.add(battle_tag)
                last_prompted_turn.pop(str(battle_tag), None)
                continue
            active_battles.append(battle)

        for battle in active_battles:
            battle_tag = str(getattr(battle, "battle_tag", id(battle)))
            can_choose = bool(getattr(battle, "available_moves", None) or getattr(battle, "available_switches", None))
            if not can_choose:
                continue

            current_turn = int(getattr(battle, "turn", 0) or 0)
            if last_prompted_turn.get(battle_tag) == current_turn:
                continue

            state = parse_battle_to_state(battle)
            turn_suggestions = get_turn_suggestions(
                state,
                resolved_mechanics,
                top_k=top_k,
                model=None,
            )

            if turn_suggestions:
                best_action = turn_suggestions[0].action
                if best_action.is_switch:
                    selected_switch = (
                        best_action.raw_move
                        if best_action.raw_move is not None
                        else list(battle.available_switches)[0]
                    )
                    chosen_order = player.create_order(selected_switch)
                else:
                    selected_move = (
                        best_action.raw_move
                        if best_action.raw_move is not None
                        else list(battle.available_moves)[0]
                    )
                    chosen_order = player.create_order(selected_move)
            elif battle.available_moves:
                chosen_order = player.create_order(list(battle.available_moves)[0])
            else:
                chosen_order = player.create_order(list(battle.available_switches)[0])

            if hasattr(player, "set_pending_order"):
                player.set_pending_order(battle_tag, chosen_order)
            last_prompted_turn[battle_tag] = current_turn
            turns_ran += 1

            if max_turns is not None and turns_ran >= max_turns:
                return

        if runner.done and not active_battles:
            break

        time.sleep(0.1)


def run_heuristic_training_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    top_k: int = 3,
    max_turns: int | None = None,
    decision_budget: int | None = None,
    log_path: str | Path = "training/battle_logs.jsonl",
    cycle_id: int = 0,
    n_games: int | None = None,
) -> dict[str, Any]:

    resolved_budget = int(decision_budget if decision_budget is not None else (max_turns or 1000))
    if resolved_budget <= 0:
        return {
            "source": "heuristic",
            "cycle_id": cycle_id,
            "decision_budget": 0,
            "decisions_collected": 0,
            "decisions_played": 0,
            "battles_launched": 0,
            "battles_finished": 0,
        }

    output_path = Path(log_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _safe_reset_battles(player)

    runner: AsyncConnectionRunner | None = None
    battles_launched = 0
    battles_finished = 0
    decisions_played = 0
    decisions_collected = 0

    pending_by_battle: dict[str, list[tuple[State, str]]] = {}
    finished_tags: set[str] = set()
    last_prompted_turn: dict[str, int] = {}

    while True:
        if runner is not None and runner.done:
            runner.raise_if_failed()
            runner = None

        battles = dict(getattr(player, "battles", {}) or {})

        for battle_tag, battle in battles.items():
            if not getattr(battle, "finished", False) or battle_tag in finished_tags:
                continue

            outcome_value, battle_result = _battle_outcome_value(battle)
            buffered = pending_by_battle.pop(battle_tag, [])
            for state, chosen_action_id in buffered:
                record = make_log_record(
                    state,
                    chosen_action_id=chosen_action_id,
                    outcome_value=outcome_value,
                    metadata={
                        "source": "heuristic",
                        "cycle_id": int(cycle_id),
                        "battle_result": battle_result,
                    },
                )
                write_log_record(output_path, record)

            finished_tags.add(battle_tag)
            last_prompted_turn.pop(battle_tag, None)
            battles_finished += 1

        active_battles = [battle for battle in battles.values() if not getattr(battle, "finished", False)]
        can_launch_more = decisions_collected < resolved_budget and (
            n_games is None or battles_launched < n_games
        )

        if runner is None and not active_battles and can_launch_more:
            runner = _launch_single_game(player)
            battles_launched += 1

        for battle in active_battles:
            battle_tag = str(getattr(battle, "battle_tag", id(battle)))
            can_choose = bool(getattr(battle, "available_moves", None) or getattr(battle, "available_switches", None))
            if not can_choose:
                continue

            current_turn = int(getattr(battle, "turn", 0) or 0)
            if last_prompted_turn.get(battle_tag) == current_turn:
                continue

            state = parse_battle_to_state(battle)
            turn_suggestions = get_turn_suggestions(state, mechanics, top_k=top_k, model=None)

            if turn_suggestions:
                best_action = turn_suggestions[0].action
                chosen_action_id = best_action.action_id
                if best_action.is_switch:
                    selected_switch = (
                        best_action.raw_move
                        if best_action.raw_move is not None
                        else list(battle.available_switches)[0]
                    )
                    chosen_order = player.create_order(selected_switch)
                else:
                    selected_move = (
                        best_action.raw_move
                        if best_action.raw_move is not None
                        else list(battle.available_moves)[0]
                    )
                    chosen_order = player.create_order(selected_move)
            elif battle.available_moves:
                fallback_move = list(battle.available_moves)[0]
                chosen_order = player.create_order(fallback_move)
                chosen_action_id = str(getattr(fallback_move, "id", "fallback_move"))
            else:
                fallback_switch = list(battle.available_switches)[0]
                chosen_order = player.create_order(fallback_switch)
                chosen_action_id = f"switch:{getattr(fallback_switch, 'species', 'fallback')}"

            if hasattr(player, "set_pending_order"):
                player.set_pending_order(battle_tag, chosen_order)

            if decisions_collected < resolved_budget:
                pending_by_battle.setdefault(battle_tag, []).append((state, chosen_action_id))
                decisions_collected += 1

            decisions_played += 1
            last_prompted_turn[battle_tag] = current_turn

        if (
            decisions_collected >= resolved_budget
            and not active_battles
            and runner is None
            and not pending_by_battle
        ):
            break

        if not can_launch_more and not active_battles and runner is None and not pending_by_battle:
            break

        time.sleep(0.1)

    for battle_tag, buffered in list(pending_by_battle.items()):
        for state, chosen_action_id in buffered:
            record = make_log_record(
                state,
                chosen_action_id=chosen_action_id,
                outcome_value=0.0,
                metadata={
                    "source": "heuristic",
                    "cycle_id": int(cycle_id),
                    "battle_result": "unknown",
                },
            )
            write_log_record(output_path, record)
        pending_by_battle.pop(battle_tag, None)

    return {
        "source": "heuristic",
        "cycle_id": cycle_id,
        "decision_budget": int(resolved_budget),
        "decisions_collected": int(decisions_collected),
        "decisions_played": int(decisions_played),
        "battles_launched": int(battles_launched),
        "battles_finished": int(battles_finished),
    }


def run_model_training_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    model: ModelBonusFn,
    top_k: int = 3,
    max_turns: int | None = None,
    decision_budget: int | None = None,
    log_path: str | Path = "training/battle_logs.jsonl",
    cycle_id: int = 1,
    model_checkpoint: str | None = None,
    n_games: int | None = None,
) -> dict[str, Any]:

    resolved_budget = int(decision_budget if decision_budget is not None else (max_turns or 1000))
    if resolved_budget <= 0:
        return {
            "source": "model",
            "cycle_id": cycle_id,
            "decision_budget": 0,
            "decisions_collected": 0,
            "decisions_played": 0,
            "battles_launched": 0,
            "battles_finished": 0,
        }

    output_path = Path(log_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _safe_reset_battles(player)

    runner: AsyncConnectionRunner | None = None
    battles_launched = 0
    battles_finished = 0
    decisions_played = 0
    decisions_collected = 0

    pending_by_battle: dict[str, list[tuple[State, str]]] = {}
    finished_tags: set[str] = set()
    last_prompted_turn: dict[str, int] = {}

    while True:
        if runner is not None and runner.done:
            runner.raise_if_failed()
            runner = None

        battles = dict(getattr(player, "battles", {}) or {})

        for battle_tag, battle in battles.items():
            if not getattr(battle, "finished", False) or battle_tag in finished_tags:
                continue

            outcome_value, battle_result = _battle_outcome_value(battle)
            buffered = pending_by_battle.pop(battle_tag, [])
            for state, chosen_action_id in buffered:
                metadata: dict[str, Any] = {
                    "source": "model",
                    "cycle_id": int(cycle_id),
                    "battle_result": battle_result,
                }
                if model_checkpoint is not None:
                    metadata["model_checkpoint"] = model_checkpoint
                record = make_log_record(
                    state,
                    chosen_action_id=chosen_action_id,
                    outcome_value=outcome_value,
                    metadata=metadata,
                )
                write_log_record(output_path, record)

            finished_tags.add(battle_tag)
            last_prompted_turn.pop(battle_tag, None)
            battles_finished += 1

        active_battles = [battle for battle in battles.values() if not getattr(battle, "finished", False)]
        can_launch_more = decisions_collected < resolved_budget and (
            n_games is None or battles_launched < n_games
        )

        if runner is None and not active_battles and can_launch_more:
            runner = _launch_single_game(player)
            battles_launched += 1

        for battle in active_battles:
            battle_tag = str(getattr(battle, "battle_tag", id(battle)))
            can_choose = bool(getattr(battle, "available_moves", None) or getattr(battle, "available_switches", None))
            if not can_choose:
                continue

            current_turn = int(getattr(battle, "turn", 0) or 0)
            if last_prompted_turn.get(battle_tag) == current_turn:
                continue

            state = parse_battle_to_state(battle)
            turn_suggestions = get_turn_suggestions(state, mechanics, top_k=top_k, model=model)

            if turn_suggestions:
                best_action = turn_suggestions[0].action
                chosen_action_id = best_action.action_id
                if best_action.is_switch:
                    selected_switch = (
                        best_action.raw_move
                        if best_action.raw_move is not None
                        else list(battle.available_switches)[0]
                    )
                    chosen_order = player.create_order(selected_switch)
                else:
                    selected_move = (
                        best_action.raw_move
                        if best_action.raw_move is not None
                        else list(battle.available_moves)[0]
                    )
                    chosen_order = player.create_order(selected_move)
            elif battle.available_moves:
                fallback_move = list(battle.available_moves)[0]
                chosen_order = player.create_order(fallback_move)
                chosen_action_id = str(getattr(fallback_move, "id", "fallback_move"))
            else:
                fallback_switch = list(battle.available_switches)[0]
                chosen_order = player.create_order(fallback_switch)
                chosen_action_id = f"switch:{getattr(fallback_switch, 'species', 'fallback')}"

            if hasattr(player, "set_pending_order"):
                player.set_pending_order(battle_tag, chosen_order)

            if decisions_collected < resolved_budget:
                pending_by_battle.setdefault(battle_tag, []).append((state, chosen_action_id))
                decisions_collected += 1

            decisions_played += 1
            last_prompted_turn[battle_tag] = current_turn

        if (
            decisions_collected >= resolved_budget
            and not active_battles
            and runner is None
            and not pending_by_battle
        ):
            break

        if not can_launch_more and not active_battles and runner is None and not pending_by_battle:
            break

        time.sleep(0.1)

    for battle_tag, buffered in list(pending_by_battle.items()):
        for state, chosen_action_id in buffered:
            metadata: dict[str, Any] = {
                "source": "model",
                "cycle_id": int(cycle_id),
                "battle_result": "unknown",
            }
            if model_checkpoint is not None:
                metadata["model_checkpoint"] = model_checkpoint
            record = make_log_record(
                state,
                chosen_action_id=chosen_action_id,
                outcome_value=0.0,
                metadata=metadata,
            )
            write_log_record(output_path, record)
        pending_by_battle.pop(battle_tag, None)

    return {
        "source": "model",
        "cycle_id": cycle_id,
        "decision_budget": int(resolved_budget),
        "decisions_collected": int(decisions_collected),
        "decisions_played": int(decisions_played),
        "battles_launched": int(battles_launched),
        "battles_finished": int(battles_finished),
    }


def connect_to_battle(
    player: Any,
    *,
    max_turns: int | None = None,
    n_games: int | None = 1,
) -> Any:

    runner = AsyncConnectionRunner(player, n_games).start()
    wait_loops = max_turns or 1000

    for _ in range(wait_loops):
        battle = get_battle(player)
        if battle is not None:
            bot_name = getattr(battle, "player_username", None) or getattr(player, "username", None) or "unknown"
            human_name = getattr(battle, "opponent_username", None) or "unknown"
            battle_tag = getattr(battle, "battle_tag", "unknown")
            print(f"Live battle connected: bot='{bot_name}' vs human='{human_name}' (tag={battle_tag})")
            return battle

        if runner.done:
            runner.raise_if_failed()
        time.sleep(0.2)

    print("Timed out waiting for a live battle object.")
    return None


def main() -> int:

    # TO RUN (in bash):
    # cd /home/jeezu/CptS440-PokemonAI/showdownAIproject
    # source .venv/bin/activate
    # python3 -m psai.app.main

    player = pokeEnvPlayerInfo()
    mechanics = MechanicsAPI()

    # DIFFERENT BATTLE TYPES, uncomment whichever one we run.

    # connect_to_battle(player, max_turns=100)
    # run_battle(player, mechanics=mechanics, top_k=3, model=None, max_turns=100)
    run_test_battle(player, max_turns=100000)
    # run_heuristic_training_battle(
    #     player,
    #     mechanics=mechanics,
    #     top_k=3,
    #     decision_budget=20_000,
    #     n_games=None,
    # )

    # Alright, after setting up automatic logging, the training battle loops, and everything else,
    # This is how we run the full training cycle. 
    # 1. If there are no logs, run heuristic training first to generate initial data
    # 2. During the heuristic run, we log all the decisions and outcomes to a file.
    # 3. Then we train the policy/value model on all accumulated logs.
    # 4. After training, we run model-play collection for the configured decision budget.
    # 5. After the cycle completes, we run eval to determine if the winrate is above the accenptable amount.
    # 6. If the eval fails, stop the loop and change training config in train.py.
    

    training_report = run_training_cycle(
        TrainingLoopConfig(
            log_path="training/battle_logs.jsonl",
            artifact_dir="training/artifacts",
            bootstrap_decisions=20_000,
            model_cycle_decisions=10_000,
            eval_games=100,
            eval_min_win_rate=0.50,
            max_cycles=1,
            collection_n_games=None,
        ),
        player,
        mechanics,
    )
    print(f"Training status: {training_report['status']}")
    # run_model_training_battle(player, mechanics=MechanicsAPI(), model=None, top_k=3, max_turns=100000)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
