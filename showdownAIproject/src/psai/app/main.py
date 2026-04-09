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
from poke_env.battle.pokemon import Pokemon
from poke_env.player import Player

from psai.decision.chooser import ModelBonusFn, MoveSuggestion, choose_actions
from psai.domain.state import State, parse_battle_to_state
from psai.mechanics.api import MechanicsAPI
from psai.training.dataset import make_log_record, write_log_record
from psai.training.train import TrainingLoopConfig, run_training_cycle


_ORIGINAL_AVAILABLE_MOVES_FROM_REQUEST = Pokemon.available_moves_from_request


def _request_move_ids_from_request(request: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(request, dict):
        return ()

    active_payload = request.get("active")
    if not isinstance(active_payload, list) or not active_payload:
        return ()

    first_active = active_payload[0]
    if not isinstance(first_active, dict):
        return ()

    moves_payload = first_active.get("moves", [])
    if not isinstance(moves_payload, list):
        return ()

    move_ids: list[str] = []
    for move_payload in moves_payload:
        if not isinstance(move_payload, dict):
            continue
        if move_payload.get("disabled", False):
            continue
        move_id = move_payload.get("id")
        if move_id is None:
            move_id = move_payload.get("move")
        if move_id is None:
            continue

        normalized = str(move_id).strip().lower().replace(" ", "")
        if normalized:
            move_ids.append(normalized)

    return tuple(move_ids)


def _request_first_enabled_move_slot(request: dict[str, Any] | None) -> tuple[int | None, str | None]:
    if not isinstance(request, dict):
        return None, None

    active_payload = request.get("active")
    if not isinstance(active_payload, list) or not active_payload:
        return None, None

    first_active = active_payload[0]
    if not isinstance(first_active, dict):
        return None, None

    moves_payload = first_active.get("moves", [])
    if not isinstance(moves_payload, list):
        return None, None

    for index, move_payload in enumerate(moves_payload, start=1):
        if not isinstance(move_payload, dict):
            continue
        if move_payload.get("disabled", False):
            continue

        move_id = move_payload.get("id")
        if move_id is None:
            move_id = move_payload.get("move")
        if move_id is None:
            move_id = f"slot_{index}"
        normalized = str(move_id).strip().lower().replace(" ", "")
        return index, normalized

    return None, None


def _install_poke_env_move_request_fallback() -> None:
    current_impl = Pokemon.available_moves_from_request
    if getattr(current_impl, "__name__", "") == "_psai_available_moves_from_request":
        return

    def _psai_available_moves_from_request(self: Any, request: dict[str, Any]) -> list[Any]:
        try:
            return _ORIGINAL_AVAILABLE_MOVES_FROM_REQUEST(self, request)
        except AssertionError:
            # Some Gen1 ladder requests include pseudo move ids (e.g. "fight")
            # that trigger poke-env assertions. Fallback to request-confirmed moves only.
            move_lookup = dict(getattr(self, "moves", {}) or {})
            request_move_ids = _request_move_ids_from_request(request)
            fallback_moves = [move_lookup[move_id] for move_id in request_move_ids if move_id in move_lookup]
            return fallback_moves

    Pokemon.available_moves_from_request = _psai_available_moves_from_request


_install_poke_env_move_request_fallback()


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
        if battle_format != "gen1randombattle":
            raise ValueError("Only gen1randombattle is supported in the current ladder runner.")

        self._configured_username = username
        self._configured_password = password
        self._configured_battle_format = battle_format
        self._configured_team = team

        account_configuration = AccountConfiguration(username, password)
        player_kwargs: dict[str, Any] = {
            "account_configuration": account_configuration,
            "server_configuration": ShowdownServerConfiguration,
            "battle_format": battle_format,
            "strict_battle_tracking": False,
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
        return player.create_order(f"/choose move {action_index}")

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


def _choice_object_id(choice: Any) -> str:
    return str(
        getattr(choice, "id", None)
        or getattr(choice, "move", None)
        or getattr(choice, "species", None)
        or getattr(choice, "name", None)
        or repr(choice)
    )


def _battle_request_signature(battle: Any) -> tuple[Any, ...]:
    turn = int(getattr(battle, "turn", 0) or 0)
    available_moves = list(getattr(battle, "available_moves", []) or [])
    available_switches = list(getattr(battle, "available_switches", []) or [])
    move_ids = tuple(_choice_object_id(move) for move in available_moves)
    switch_ids = tuple(_choice_object_id(switch_option) for switch_option in available_switches)

    force_switch_raw = getattr(battle, "force_switch", False)
    if isinstance(force_switch_raw, (list, tuple)):
        force_switch = tuple(bool(value) for value in force_switch_raw)
    else:
        force_switch = bool(force_switch_raw)

    request_payload = getattr(battle, "_last_request", None)
    request_wait = bool(request_payload.get("wait", False)) if isinstance(request_payload, dict) else False
    request_move_ids = _request_move_ids_from_request(request_payload if isinstance(request_payload, dict) else None)

    return (turn, move_ids, switch_ids, force_switch, request_wait, request_move_ids)


def _move_action_id_from_move(move: Any) -> str:
    return str(getattr(move, "id", None) or getattr(move, "move", None) or "fallback_move")


def _switch_action_id_from_target(target: Any) -> str:
    identifier = str(
        getattr(target, "identifier", None)
        or getattr(target, "species", None)
        or getattr(target, "name", None)
        or "fallback"
    )
    return f"switch:{identifier}"


def _default_order(player: Any) -> Any:
    choose_default = getattr(player, "choose_default_move", None)
    if callable(choose_default):
        return choose_default()
    return player.create_order("/choose default")


def _select_move_slot(available_moves: list[Any], best_action: Any | None) -> int | None:
    if not available_moves:
        return None
    if best_action is None or bool(getattr(best_action, "is_switch", False)):
        return 1

    raw_move = getattr(best_action, "raw_move", None)
    if raw_move is not None:
        for index, candidate in enumerate(available_moves, start=1):
            if candidate is raw_move:
                return index

    action_id = str(getattr(best_action, "action_id", "") or "")
    if action_id:
        for index, candidate in enumerate(available_moves, start=1):
            if _move_action_id_from_move(candidate) == action_id:
                return index

    return 1


def _select_switch_target(available_switches: list[Any], best_action: Any | None) -> Any | None:
    if not available_switches:
        return None
    if best_action is None:
        return available_switches[0]
    if not bool(getattr(best_action, "is_switch", False)):
        return available_switches[0]

    raw_switch = getattr(best_action, "raw_move", None)
    if raw_switch is not None:
        for candidate in available_switches:
            if candidate is raw_switch:
                return candidate

    action_id = str(getattr(best_action, "action_id", "") or "")
    if action_id:
        for candidate in available_switches:
            if _switch_action_id_from_target(candidate) == action_id:
                return candidate

    return available_switches[0]


def _has_actionable_request(state: State, battle: Any) -> bool:
    if state.request_mode == "wait":
        return False
    if state.legal_actions:
        return True

    request_payload = getattr(battle, "_last_request", None)
    if not isinstance(request_payload, dict):
        return False
    if bool(request_payload.get("wait", False)):
        return False
    return True


def _choose_order_for_request(
    player: Any,
    battle: Any,
    state: State,
    turn_suggestions: list[MoveSuggestion],
) -> tuple[Any, str]:
    available_moves = list(getattr(battle, "available_moves", []) or [])
    available_switches = list(getattr(battle, "available_switches", []) or [])
    request_payload = getattr(battle, "_last_request", None)
    request_move_ids = _request_move_ids_from_request(request_payload if isinstance(request_payload, dict) else None)
    request_slot_index, request_slot_move_id = _request_first_enabled_move_slot(
        request_payload if isinstance(request_payload, dict) else None
    )
    best_action = turn_suggestions[0].action if turn_suggestions else None
    mode = state.request_mode

    # Dealing with abnormal showdown requests:
    # if request parsing does not produce standard move objects, we keep the game moving by
    # (1) selecting the first enabled move slot from the raw request payload,
    # (2) otherwise selecting the first legal switch, then
    # (3) falling back to /choose default.
    if mode == "move_request_unparsed":
        if request_slot_index is not None:
            slot_id = request_slot_move_id or f"slot_{request_slot_index}"
            return (
                player.create_order(f"/choose move {request_slot_index}"),
                f"request_slot_fallback:{slot_id}",
            )
        selected_switch = _select_switch_target(available_switches, None)
        if selected_switch is not None:
            return player.create_order(selected_switch), _switch_action_id_from_target(selected_switch)
        return _default_order(player), "default"

    if mode in {"forced_switch", "switch_only"}:
        selected_switch = _select_switch_target(available_switches, best_action)
        if selected_switch is not None:
            return player.create_order(selected_switch), _switch_action_id_from_target(selected_switch)
        return _default_order(player), "default"

    if mode in {"team_preview", "wait"}:
        return _default_order(player), "default"

    if mode == "move_or_switch" and best_action is not None and bool(getattr(best_action, "is_switch", False)):
        selected_switch = _select_switch_target(available_switches, best_action)
        if selected_switch is not None:
            return player.create_order(selected_switch), _switch_action_id_from_target(selected_switch)

    selected_slot = _select_move_slot(available_moves, best_action)
    if selected_slot is not None:
        selected_move = available_moves[selected_slot - 1]
        return player.create_order(f"/choose move {selected_slot}"), _move_action_id_from_move(selected_move)

    selected_switch = _select_switch_target(available_switches, best_action)
    if selected_switch is not None:
        return player.create_order(selected_switch), _switch_action_id_from_target(selected_switch)

    if request_move_ids and request_slot_index is not None:
        slot_id = request_slot_move_id or request_move_ids[0]
        return (
            player.create_order(f"/choose move {request_slot_index}"),
            f"request_slot_fallback:{slot_id}",
        )

    return _default_order(player), "default"


def _print_turn_suggestions(
    phase_label: str,
    battle_tag: str,
    state: State,
    turn_suggestions: list[MoveSuggestion],
    chosen_action_id: str,
    *,
    max_suggestions: int,
) -> None:
    turn_value = state.turn_number if state.turn_number is not None else state.turn
    print(
        f"[{phase_label}] battle={battle_tag} turn={turn_value} mode={state.request_mode} "
        f"legal={len(state.legal_actions)} chosen={chosen_action_id}"
    )

    if not turn_suggestions:
        print(f"[{phase_label}] no ranked suggestions for this request")
        return

    for suggestion in turn_suggestions[: max(0, max_suggestions)]:
        print(
            f"[{phase_label}]   #{suggestion.rank} {suggestion.action.action_id} "
            f"score={suggestion.score:.2f}"
        )
        if suggestion.reasons:
            print(f"[{phase_label}]      reasons: {'; '.join(suggestion.reasons)}")


def _print_collection_progress(
    phase_label: str,
    *,
    cycle_id: int,
    decisions_collected: int,
    decision_budget: int,
    decisions_played: int,
    battles_launched: int,
    battles_finished: int,
) -> None:
    print(
        f"[{phase_label}] cycle={cycle_id} decisions={decisions_collected}/{decision_budget} "
        f"played={decisions_played} battles={battles_finished}/{battles_launched}"
    )


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
    verbose: bool = True,
    print_turn_suggestions: bool = True,
    print_top_k: int | None = None,
) -> None:

    # Ladder connectivity smoke test:
    # starts ladder game(s) and auto-plays with heuristic choices only.
    # No training logs are written.

    _safe_reset_battles(player)
    resolved_mechanics = mechanics or MechanicsAPI()
    runner = AsyncConnectionRunner(player, n_games).start()
    resolved_print_top_k = int(print_top_k if print_top_k is not None else top_k)

    turns_ran = 0
    last_prompted_request: dict[str, tuple[Any, ...]] = {}
    finished_tags: set[str] = set()
    seen_tags: set[str] = set()

    if verbose:
        print(f"[test] start n_games={n_games} top_k={top_k} max_turns={max_turns}")

    while True:
        if runner.done:
            runner.raise_if_failed()

        battles = dict(getattr(player, "battles", {}) or {})
        active_battles = []
        for battle_tag, battle in battles.items():
            if getattr(battle, "finished", False):
                if battle_tag not in finished_tags:
                    finished_tags.add(battle_tag)
                    if verbose:
                        outcome_value, battle_result = _battle_outcome_value(battle)
                        del outcome_value
                        print(f"[test] battle_finished tag={battle_tag} result={battle_result}")
                last_prompted_request.pop(str(battle_tag), None)
                continue
            active_battles.append(battle)

        for battle in active_battles:
            battle_tag = str(getattr(battle, "battle_tag", id(battle)))
            if battle_tag not in seen_tags:
                seen_tags.add(battle_tag)
                if verbose:
                    print(f"[test] battle_started tag={battle_tag}")
            request_signature = _battle_request_signature(battle)
            if last_prompted_request.get(battle_tag) == request_signature:
                continue

            state = parse_battle_to_state(battle)
            if not _has_actionable_request(state, battle):
                continue

            turn_suggestions = (
                get_turn_suggestions(
                    state,
                    resolved_mechanics,
                    top_k=top_k,
                    model=None,
                )
                if state.legal_actions
                else []
            )
            chosen_order, _chosen_action_id = _choose_order_for_request(
                player,
                battle,
                state,
                turn_suggestions,
            )

            if verbose and print_turn_suggestions:
                _print_turn_suggestions(
                    "test",
                    battle_tag,
                    state,
                    turn_suggestions,
                    _chosen_action_id,
                    max_suggestions=resolved_print_top_k,
                )

            if hasattr(player, "set_pending_order"):
                player.set_pending_order(battle_tag, chosen_order)
            last_prompted_request[battle_tag] = request_signature
            turns_ran += 1

            if max_turns is not None and turns_ran >= max_turns:
                if verbose:
                    print(f"[test] reached max_turns={max_turns}, stopping")
                return

        if runner.done and not active_battles:
            break

        time.sleep(0.1)

    if verbose:
        print(f"[test] complete turns={turns_ran} battles_finished={len(finished_tags)}")


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
    verbose: bool = True,
    print_every_decisions: int = 100,
    print_top_k: int | None = None,
    print_turn_suggestions: bool = True,
) -> dict[str, Any]:

    resolved_budget = int(decision_budget if decision_budget is not None else (max_turns or 1000))
    resolved_print_top_k = int(print_top_k if print_top_k is not None else top_k)
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

    if verbose:
        print(
            f"[heuristic] start cycle={cycle_id} decision_budget={resolved_budget} "
            f"log_path={output_path}"
        )

    runner: AsyncConnectionRunner | None = None
    battles_launched = 0
    battles_finished = 0
    decisions_played = 0
    decisions_collected = 0

    pending_by_battle: dict[str, list[tuple[State, str]]] = {}
    finished_tags: set[str] = set()
    last_prompted_request: dict[str, tuple[Any, ...]] = {}

    while True:
        if runner is not None and runner.done:
            runner.raise_if_failed()
            runner = None

        battles = dict(getattr(player, "battles", {}) or {})

        for battle_tag, battle in battles.items():
            battle_tag_str = str(battle_tag)
            if not getattr(battle, "finished", False) or battle_tag_str in finished_tags:
                continue

            outcome_value, battle_result = _battle_outcome_value(battle)
            buffered = pending_by_battle.pop(battle_tag_str, [])
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

            finished_tags.add(battle_tag_str)
            last_prompted_request.pop(battle_tag_str, None)
            battles_finished += 1
            if verbose:
                print(
                    f"[heuristic] battle_finished tag={battle_tag_str} result={battle_result} "
                    f"buffered_decisions={len(buffered)}"
                )

        active_battles = [battle for battle in battles.values() if not getattr(battle, "finished", False)]
        can_launch_more = decisions_collected < resolved_budget and (
            n_games is None or battles_launched < n_games
        )

        if runner is None and not active_battles and can_launch_more:
            runner = _launch_single_game(player)
            battles_launched += 1
            if verbose:
                print(f"[heuristic] launched ladder game #{battles_launched}")

        for battle in active_battles:
            battle_tag = str(getattr(battle, "battle_tag", id(battle)))
            request_signature = _battle_request_signature(battle)
            if last_prompted_request.get(battle_tag) == request_signature:
                continue

            state = parse_battle_to_state(battle)
            if not _has_actionable_request(state, battle):
                continue

            turn_suggestions = (
                get_turn_suggestions(state, mechanics, top_k=top_k, model=None)
                if state.legal_actions
                else []
            )
            chosen_order, chosen_action_id = _choose_order_for_request(
                player,
                battle,
                state,
                turn_suggestions,
            )

            if verbose and print_turn_suggestions:
                _print_turn_suggestions(
                    "heuristic",
                    battle_tag,
                    state,
                    turn_suggestions,
                    chosen_action_id,
                    max_suggestions=resolved_print_top_k,
                )

            if hasattr(player, "set_pending_order"):
                player.set_pending_order(battle_tag, chosen_order)

            should_log_decision = bool(state.legal_actions) and chosen_action_id != "default"
            if should_log_decision and decisions_collected < resolved_budget:
                pending_by_battle.setdefault(battle_tag, []).append((state, chosen_action_id))
                decisions_collected += 1

            if should_log_decision:
                decisions_played += 1
            last_prompted_request[battle_tag] = request_signature
            if (
                verbose
                and should_log_decision
                and print_every_decisions > 0
                and decisions_collected % int(print_every_decisions) == 0
            ):
                _print_collection_progress(
                    "heuristic",
                    cycle_id=int(cycle_id),
                    decisions_collected=int(decisions_collected),
                    decision_budget=int(resolved_budget),
                    decisions_played=int(decisions_played),
                    battles_launched=int(battles_launched),
                    battles_finished=int(battles_finished),
                )

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

    if verbose:
        _print_collection_progress(
            "heuristic",
            cycle_id=int(cycle_id),
            decisions_collected=int(decisions_collected),
            decision_budget=int(resolved_budget),
            decisions_played=int(decisions_played),
            battles_launched=int(battles_launched),
            battles_finished=int(battles_finished),
        )

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
    verbose: bool = True,
    print_every_decisions: int = 100,
    print_top_k: int | None = None,
    print_turn_suggestions: bool = True,
) -> dict[str, Any]:

    resolved_budget = int(decision_budget if decision_budget is not None else (max_turns or 1000))
    resolved_print_top_k = int(print_top_k if print_top_k is not None else top_k)
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

    if verbose:
        print(
            f"[model] start cycle={cycle_id} decision_budget={resolved_budget} "
            f"log_path={output_path} checkpoint={model_checkpoint}"
        )

    runner: AsyncConnectionRunner | None = None
    battles_launched = 0
    battles_finished = 0
    decisions_played = 0
    decisions_collected = 0

    pending_by_battle: dict[str, list[tuple[State, str]]] = {}
    finished_tags: set[str] = set()
    last_prompted_request: dict[str, tuple[Any, ...]] = {}

    while True:
        if runner is not None and runner.done:
            runner.raise_if_failed()
            runner = None

        battles = dict(getattr(player, "battles", {}) or {})

        for battle_tag, battle in battles.items():
            battle_tag_str = str(battle_tag)
            if not getattr(battle, "finished", False) or battle_tag_str in finished_tags:
                continue

            outcome_value, battle_result = _battle_outcome_value(battle)
            buffered = pending_by_battle.pop(battle_tag_str, [])
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

            finished_tags.add(battle_tag_str)
            last_prompted_request.pop(battle_tag_str, None)
            battles_finished += 1
            if verbose:
                print(
                    f"[model] battle_finished tag={battle_tag_str} result={battle_result} "
                    f"buffered_decisions={len(buffered)}"
                )

        active_battles = [battle for battle in battles.values() if not getattr(battle, "finished", False)]
        can_launch_more = decisions_collected < resolved_budget and (
            n_games is None or battles_launched < n_games
        )

        if runner is None and not active_battles and can_launch_more:
            runner = _launch_single_game(player)
            battles_launched += 1
            if verbose:
                print(f"[model] launched ladder game #{battles_launched}")

        for battle in active_battles:
            battle_tag = str(getattr(battle, "battle_tag", id(battle)))
            request_signature = _battle_request_signature(battle)
            if last_prompted_request.get(battle_tag) == request_signature:
                continue

            state = parse_battle_to_state(battle)
            if not _has_actionable_request(state, battle):
                continue

            turn_suggestions = (
                get_turn_suggestions(state, mechanics, top_k=top_k, model=model)
                if state.legal_actions
                else []
            )
            chosen_order, chosen_action_id = _choose_order_for_request(
                player,
                battle,
                state,
                turn_suggestions,
            )

            if verbose and print_turn_suggestions:
                _print_turn_suggestions(
                    "model",
                    battle_tag,
                    state,
                    turn_suggestions,
                    chosen_action_id,
                    max_suggestions=resolved_print_top_k,
                )

            if hasattr(player, "set_pending_order"):
                player.set_pending_order(battle_tag, chosen_order)

            should_log_decision = bool(state.legal_actions) and chosen_action_id != "default"
            if should_log_decision and decisions_collected < resolved_budget:
                pending_by_battle.setdefault(battle_tag, []).append((state, chosen_action_id))
                decisions_collected += 1

            if should_log_decision:
                decisions_played += 1
            last_prompted_request[battle_tag] = request_signature
            if (
                verbose
                and should_log_decision
                and print_every_decisions > 0
                and decisions_collected % int(print_every_decisions) == 0
            ):
                _print_collection_progress(
                    "model",
                    cycle_id=int(cycle_id),
                    decisions_collected=int(decisions_collected),
                    decision_budget=int(resolved_budget),
                    decisions_played=int(decisions_played),
                    battles_launched=int(battles_launched),
                    battles_finished=int(battles_finished),
                )

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

    if verbose:
        _print_collection_progress(
            "model",
            cycle_id=int(cycle_id),
            decisions_collected=int(decisions_collected),
            decision_budget=int(resolved_budget),
            decisions_played=int(decisions_played),
            battles_launched=int(battles_launched),
            battles_finished=int(battles_finished),
        )

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
    
    """
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
    print(f"Training status: {training_report['status']}")"""
    # run_model_training_battle(player, mechanics=MechanicsAPI(), model=None, top_k=3, max_turns=100000)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
