"""Runtime scaffold for poke-env battle intake and decision handoff."""

# Plain-English summary:
# This module provides a battle-loop scaffold: get battle objects,
# parse each into State, run chooser, and print move suggestions.

# ========================================
# Imports
# ========================================

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

from poke_env import AccountConfiguration, ShowdownServerConfiguration
from poke_env.battle.pokemon import Pokemon
from poke_env.player import Player

from psai.app.connections import (
    AsyncConnectionRunner,
    _resolve_runner_state,
    _safe_cleanup_finished_battle,
    _safe_ensure_battle_timer_on,
    _safe_requeue_ladder_search,
    _safe_reset_battles,
)
from psai.decision.chooser import ModelBonusFn, MoveSuggestion, choose_actions
from psai.domain.state import State, parse_battle_to_state
from psai.mechanics.api import MechanicsAPI
from psai.training.dataset import make_log_record, read_log_records, write_log_record
from psai.training.model import PolicyValueMLP
from psai.training.train import (
    TrainingLoopConfig,
    build_model_bonus_fn,
    load_checkpoint,
    run_training_cycle,
)

# ========================================
# Guard Code for Abnormal Shoddown Requests (Like hyperbeam recharge)
# ========================================

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

# ========================================
# Getting Battles, States, Player, and Player Actions
# ========================================

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
            "start_timer_on_battle_start": True,
        }
        if team:
            player_kwargs["team"] = team

        super().__init__(**player_kwargs)
        self._pending_orders: dict[str, Any] = {}
        self._pending_order_timeout_seconds = 15.0

    # Store pending move actions, then when prompted by showdown return that action.
    def set_pending_order(self, battle_tag: str, order: Any) -> None:
        normalized_tag = str(battle_tag or "")
        self._pending_orders[normalized_tag] = order

    def choose_move(self, battle):
        battle_tag = str(getattr(battle, "battle_tag", id(battle)) or id(battle))
        started_at = time.time()

        while True:
            pending_order = self._pending_orders.pop(battle_tag, None)
            if pending_order is not None:
                return pending_order

            if getattr(battle, "finished", False):
                return _default_order(self)

            elapsed = time.time() - started_at
            if elapsed >= self._pending_order_timeout_seconds:
                print(
                    f"[runtime] pending_order_timeout battle={battle_tag} "
                    f"waited={elapsed:.1f}s; sending default order"
                )
                return _default_order(self)
            time.sleep(0.1)


def get_turn_suggestions(
    state: State,
    mechanics: MechanicsAPI,
    *,
    top_k: int = 3,
    model: ModelBonusFn | None = None,
) -> list[MoveSuggestion]:
    try:
        suggestions = choose_actions(state, mechanics, top_k=top_k, model=model)
        return suggestions
    except Exception as exc:
        battle_tag = str(getattr(state, "battle_tag", "") or "")
        turn_value = getattr(state, "turn_number", None)
        if turn_value is None:
            turn_value = getattr(state, "turn", None)
        print(
            f"[chooser] suggestion_failed battle={battle_tag} turn={turn_value} "
            f"error={type(exc).__name__}: {exc}"
        )
        return []


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

# ========================================
# Internal Request and Order Helpers
# ========================================


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


def _resolve_switch_identifier(target: Any) -> str:
    identifier = getattr(target, "identifier", None)
    if callable(identifier):
        try:
            identifier = identifier()
        except Exception:
            identifier = None

    if not identifier:
        identifier = getattr(target, "species", None) or getattr(target, "name", None)
    if callable(identifier):
        try:
            identifier = identifier()
        except Exception:
            identifier = None

    if not identifier:
        identifier = "fallback"
    return str(identifier)


def _switch_action_id_from_target(target: Any) -> str:
    return f"switch:{_resolve_switch_identifier(target)}"


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

# ========================================
# Logging and Progress Helpers
# ========================================


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


def _safe_write_decision_record(
    output_path: Path,
    *,
    state: State,
    chosen_action_id: str,
    outcome_value: float,
    metadata: dict[str, Any],
    phase_label: str,
) -> None:
    try:
        record = make_log_record(
            state,
            chosen_action_id=chosen_action_id,
            outcome_value=outcome_value,
            metadata=metadata,
        )
        write_log_record(output_path, record)
    except Exception as exc:
        battle_tag = str(getattr(state, "battle_tag", "") or "")
        turn_value = getattr(state, "turn_number", None)
        if turn_value is None:
            turn_value = getattr(state, "turn", None)
        print(
            f"[{phase_label}] log_record_failed battle={battle_tag} turn={turn_value} "
            f"chosen={chosen_action_id} error={type(exc).__name__}: {exc}"
        )

# ========================================
# Public Runtime Functions
# ========================================


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

        battle_tag = str(getattr(battle, "battle_tag", id(battle)) or id(battle))
        _safe_ensure_battle_timer_on(player, battle_tag, phase_label="manual", verbose=False)

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
            battle_tag = str(getattr(battle, "battle_tag", id(battle)) or id(battle))
            player.set_pending_order(battle_tag, chosen_order)

        turns_ran += 1
        if max_turns is not None and turns_ran >= max_turns:
            break

        time.sleep(0.1)


def run_training_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    source: str,
    model: ModelBonusFn | None = None,
    top_k: int = 3,
    max_turns: int | None = None,
    decision_budget: int | None = None,
    log_path: str | Path = "training/battle_logs.jsonl",
    cycle_id: int | None = None,
    model_checkpoint: str | None = None,
    n_games: int | None = None,
    verbose: bool = True,
    print_every_decisions: int = 100,
    print_top_k: int | None = None,
    print_turn_suggestions: bool = True,
) -> dict[str, Any]:
    if source not in {"heuristic", "model"}:
        raise ValueError("source must be 'heuristic' or 'model'")
    if source == "model" and model is None:
        raise ValueError("model callable is required when source='model'")

    phase_label = source
    model_bonus = model if source == "model" else None
    resolved_cycle_id = int(cycle_id if cycle_id is not None else (1 if source == "model" else 0))

    resolved_budget = int(decision_budget if decision_budget is not None else (max_turns or 1000))
    resolved_print_top_k = int(print_top_k if print_top_k is not None else top_k)
    if resolved_budget <= 0:
        return {
            "source": source,
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
        if source == "heuristic":
            print(
                f"[heuristic] start cycle={resolved_cycle_id} decision_budget={resolved_budget} "
                f"log_path={output_path}"
            )
        else:
            print(
                f"[model] start cycle={resolved_cycle_id} decision_budget={resolved_budget} "
                f"log_path={output_path} checkpoint={model_checkpoint}"
            )

    runner: AsyncConnectionRunner | None = None
    runner_started_at: float | None = None
    battles_launched = 0
    battles_finished = 0
    decisions_played = 0
    decisions_collected = 0

    pending_by_battle: dict[str, list[tuple[State, str]]] = {}
    finished_tags: set[str] = set()
    last_prompted_request: dict[str, tuple[Any, ...]] = {}
    last_prompted_at: dict[str, float] = {}
    retry_same_request_after_seconds = 15.0
    idle_requeue_attempts = 0

    while True:
        runner = _resolve_runner_state(
            runner,
            player=player,
            phase_label=phase_label,
            verbose=verbose,
        )
        if runner is None:
            runner_started_at = None
            idle_requeue_attempts = 0

        battles = dict(getattr(player, "battles", {}) or {})

        for battle_tag, battle in battles.items():
            battle_tag_str = str(battle_tag)
            if not getattr(battle, "finished", False) or battle_tag_str in finished_tags:
                continue

            outcome_value, battle_result = _battle_outcome_value(battle)
            buffered = pending_by_battle.pop(battle_tag_str, [])
            for state, chosen_action_id in buffered:
                metadata: dict[str, Any] = {
                    "source": source,
                    "cycle_id": resolved_cycle_id,
                    "battle_result": battle_result,
                }
                if source == "model" and model_checkpoint is not None:
                    metadata["model_checkpoint"] = model_checkpoint
                _safe_write_decision_record(
                    output_path,
                    state=state,
                    chosen_action_id=chosen_action_id,
                    outcome_value=outcome_value,
                    metadata=metadata,
                    phase_label=phase_label,
                )

            finished_tags.add(battle_tag_str)
            last_prompted_request.pop(battle_tag_str, None)
            last_prompted_at.pop(battle_tag_str, None)
            battles_finished += 1
            if verbose:
                print(
                    f"[{phase_label}] battle_finished tag={battle_tag_str} result={battle_result} "
                    f"buffered_decisions={len(buffered)}"
                )
            _safe_cleanup_finished_battle(player, battle_tag_str, phase_label=phase_label, verbose=verbose)

        active_battles = [battle for battle in battles.values() if not getattr(battle, "finished", False)]
        if active_battles:
            idle_requeue_attempts = 0
        can_launch_more = decisions_collected < resolved_budget and (
            n_games is None or battles_launched < n_games
        )

        if runner is None and not active_battles and can_launch_more:
            runner = _launch_single_game(player)
            runner_started_at = time.time()
            battles_launched += 1
            if verbose:
                print(f"[{phase_label}] launched ladder game #{battles_launched}")

        if runner is not None and not runner.done and not active_battles:
            started_at = runner_started_at or time.time()
            idle_seconds = time.time() - started_at
            if idle_seconds >= 45.0:
                idle_requeue_attempts += 1
                force_recovery = idle_requeue_attempts >= 4
                if verbose:
                    print(
                        f"[{phase_label}] idle_without_battle for {idle_seconds:.1f}s; "
                        f"attempting ladder requeue"
                    )
                    if force_recovery:
                        print(
                            f"[{phase_label}] prolonged idle detected "
                            f"(attempt={idle_requeue_attempts}); forcing recovery"
                        )
                _safe_requeue_ladder_search(
                    player,
                    phase_label=phase_label,
                    verbose=verbose,
                    force=force_recovery,
                )
                runner_started_at = time.time()

        for battle in active_battles:
            battle_tag = str(getattr(battle, "battle_tag", id(battle)) or id(battle))
            _safe_ensure_battle_timer_on(player, battle_tag, phase_label=phase_label, verbose=verbose)
            request_signature = _battle_request_signature(battle)
            now = time.time()
            if last_prompted_request.get(battle_tag) == request_signature:
                last_prompt_time = float(last_prompted_at.get(battle_tag, now))
                elapsed = now - last_prompt_time
                if elapsed < retry_same_request_after_seconds:
                    continue
                if verbose:
                    print(
                        f"[{phase_label}] request_stalled battle={battle_tag} "
                        f"waited={elapsed:.1f}s; retrying order"
                    )

            try:
                state = parse_battle_to_state(battle)
            except Exception as exc:
                if verbose:
                    print(
                        f"[{phase_label}] parse_state_failed battle={battle_tag} "
                        f"error={type(exc).__name__}: {exc}. Sending default order."
                    )
                if hasattr(player, "set_pending_order"):
                    player.set_pending_order(battle_tag, _default_order(player))
                last_prompted_request[battle_tag] = request_signature
                last_prompted_at[battle_tag] = now
                continue
            if not _has_actionable_request(state, battle):
                continue

            turn_suggestions = (
                get_turn_suggestions(state, mechanics, top_k=top_k, model=model_bonus)
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
                    phase_label,
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
            last_prompted_at[battle_tag] = now
            if (
                verbose
                and should_log_decision
                and print_every_decisions > 0
                and decisions_collected % int(print_every_decisions) == 0
            ):
                _print_collection_progress(
                    phase_label,
                    cycle_id=resolved_cycle_id,
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
            metadata = {
                "source": source,
                "cycle_id": resolved_cycle_id,
                "battle_result": "unknown",
            }
            if source == "model" and model_checkpoint is not None:
                metadata["model_checkpoint"] = model_checkpoint
            _safe_write_decision_record(
                output_path,
                state=state,
                chosen_action_id=chosen_action_id,
                outcome_value=0.0,
                metadata=metadata,
                phase_label=phase_label,
            )
        pending_by_battle.pop(battle_tag, None)

    if verbose:
        _print_collection_progress(
            phase_label,
            cycle_id=resolved_cycle_id,
            decisions_collected=int(decisions_collected),
            decision_budget=int(resolved_budget),
            decisions_played=int(decisions_played),
            battles_launched=int(battles_launched),
            battles_finished=int(battles_finished),
        )

    return {
        "source": source,
        "cycle_id": resolved_cycle_id,
        "decision_budget": int(resolved_budget),
        "decisions_collected": int(decisions_collected),
        "decisions_played": int(decisions_played),
        "battles_launched": int(battles_launched),
        "battles_finished": int(battles_finished),
    }


def _checkpoint_creation_tag(checkpoint_path: Path) -> str:
    try:
        created_at = datetime.fromtimestamp(checkpoint_path.stat().st_mtime)
    except Exception:
        created_at = datetime.now()
    return created_at.strftime("%Y%m%d")


def _build_model_progress_payload(
    *,
    model_name: str,
    evaluation: dict[str, Any],
    eval_threshold: float,
    checkpoint_path: str | None = None,
) -> dict[str, Any]:
    wins = int(evaluation.get("wins", 0))
    losses = int(evaluation.get("losses", 0))
    ties = int(evaluation.get("ties", 0))
    win_rate = float(evaluation.get("win_rate", 0.0))
    threshold = float(eval_threshold)
    gate_result = "Passed" if win_rate >= threshold else "Failed"

    payload: dict[str, Any] = {
        "modelname": model_name,
        "models_evaluation_stats": {
            "Wins": wins,
            "Loss": losses,
            "Ties": ties,
            "WR%": round(win_rate * 100.0, 2),
            "Eval Threshold": round(threshold * 100.0, 2),
            "Passed/Failed": gate_result,
        },
    }
    if checkpoint_path:
        payload["checkpoint_path"] = str(checkpoint_path)
    return payload


def _write_model_progress(
    path: str | Path,
    payload: dict[str, Any],
    *,
    verbose: bool = True,
) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    if verbose:
        print(f"[eval] wrote model progress -> {output_path}")


def run_evaluation_games(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    model: ModelBonusFn | None = None,
    eval_games: int = 100,
    eval_threshold: float = 0.50,
    verbose: bool = True,
    eval_print_every_games: int = 10,
    model_name: str | None = None,
    model_progress_path: str | Path = "training/artifacts/model_progress.json",
    checkpoint_path: str | None = None,
) -> dict[str, Any]:
    # Alright, this is where we run evaluation for the model. Frankly, it should be its own function,
    # I just wanted to have everything in one place and didnt realize until it was too late just how long this section would be.
    # 
    # Nows its the new function!

    # SO, what is happening here is we are running x games of the ladder, and counting how many wins and losses we get.
    # We then get a win rate, if it that winrate is below what we expected, it failes the gate and stops the training cycle.
    # currently max cycles is 1 anyway, but we will increase that later and the gate threshold will probably be lowered.
    # Unless by some miracle the model is hitting 50%+ winrate off of the FIRST run, in which case shoot for the stars.

    # Majority of this code is just the various guard code related stuff, now moved to main for organization.

    # makes our default evaluate results
    if eval_games <= 0:
        return {"games": 0, "wins": 0, "losses": 0, "ties": 0, "win_rate": 0.0}

    if verbose:
        print(f"[loop] evaluation start games={eval_games}")
    reset_fn = getattr(player, "reset_battles", None)
    if callable(reset_fn):
        try:
            reset_fn()
        except Exception:
            pass

    runner = None
    runner_started_at: float | None = None
    games_launched = 0
    games_finished = 0
    wins = 0
    losses = 0
    ties = 0
    counted_tags: set[str] = set()
    last_prompted_request: dict[str, tuple[Any, ...]] = {}
    last_prompted_at: dict[str, float] = {}
    retry_same_request_after_seconds = 15.0
    idle_requeue_attempts = 0

    while True:
        runner = _resolve_runner_state(
            runner,
            player=player,
            phase_label="eval",
            verbose=verbose,
        )
        if runner is None:
            runner_started_at = None
            idle_requeue_attempts = 0

        battles = dict(getattr(player, "battles", {}) or {})
        for battle_tag, battle in battles.items():
            battle_tag_str = str(battle_tag)
            if not getattr(battle, "finished", False) or battle_tag_str in counted_tags:
                continue
            won = getattr(battle, "won", None)
            if won is True:
                wins += 1
            elif won is False:
                losses += 1
            else:
                ties += 1
            counted_tags.add(battle_tag_str)
            games_finished += 1
            last_prompted_request.pop(battle_tag_str, None)
            last_prompted_at.pop(battle_tag_str, None)
            _safe_cleanup_finished_battle(
                player,
                battle_tag_str,
                phase_label="eval",
                verbose=False,
            )
            if (
                verbose
                and eval_print_every_games > 0
                and games_finished % int(eval_print_every_games) == 0
            ):
                print(
                    f"[eval] games={games_finished}/{eval_games} "
                    f"wins={wins} losses={losses} ties={ties}"
                )

        active_battles = [battle for battle in battles.values() if not getattr(battle, "finished", False)]
        if active_battles:
            idle_requeue_attempts = 0
        if runner is None and not active_battles and games_launched < eval_games:
            runner = AsyncConnectionRunner(player, 1).start()
            runner_started_at = time.time()
            games_launched += 1

        if runner is not None and not runner.done and not active_battles:
            started_at = runner_started_at or time.time()
            idle_seconds = time.time() - started_at
            if idle_seconds >= 45.0:
                idle_requeue_attempts += 1
                force_recovery = idle_requeue_attempts >= 4
                if verbose:
                    print(
                        f"[eval] idle_without_battle for {idle_seconds:.1f}s; "
                        f"attempting ladder requeue"
                    )
                    if force_recovery:
                        print(
                            f"[eval] prolonged idle detected "
                            f"(attempt={idle_requeue_attempts}); forcing recovery"
                        )
                _safe_requeue_ladder_search(
                    player,
                    phase_label="eval",
                    verbose=verbose,
                    force=force_recovery,
                )
                runner_started_at = time.time()

        for battle in active_battles:
            battle_tag = str(getattr(battle, "battle_tag", id(battle)) or id(battle))
            _safe_ensure_battle_timer_on(
                player,
                battle_tag,
                phase_label="eval",
                verbose=verbose,
            )
            request_signature = _battle_request_signature(battle)
            now = time.time()
            if last_prompted_request.get(battle_tag) == request_signature:
                last_prompt_time = float(last_prompted_at.get(battle_tag, now))
                elapsed = now - last_prompt_time
                if elapsed < retry_same_request_after_seconds:
                    continue
                if verbose:
                    print(
                        f"[eval] request_stalled battle={battle_tag} "
                        f"waited={elapsed:.1f}s; retrying order"
                    )

            try:
                state = parse_battle_to_state(battle)
            except Exception as exc:
                if verbose:
                    print(
                        f"[eval] parse_state_failed battle={battle_tag} "
                        f"error={type(exc).__name__}: {exc}. Sending default order."
                    )
                if hasattr(player, "set_pending_order"):
                    player.set_pending_order(battle_tag, _default_order(player))
                last_prompted_request[battle_tag] = request_signature
                last_prompted_at[battle_tag] = now
                continue
            if not _has_actionable_request(state, battle):
                continue

            turn_suggestions = (
                get_turn_suggestions(
                    state,
                    mechanics,
                    top_k=1,
                    model=model,
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

            if hasattr(player, "set_pending_order"):
                player.set_pending_order(battle_tag, chosen_order)
            last_prompted_request[battle_tag] = request_signature
            last_prompted_at[battle_tag] = now

        if games_finished >= eval_games and not active_battles and runner is None:
            break

        time.sleep(0.1)

    evaluation = {
        "games": int(games_finished),
        "wins": int(wins),
        "losses": int(losses),
        "ties": int(ties),
        "win_rate": float(wins / games_finished) if games_finished > 0 else 0.0,
    }
    if verbose:
        print(
            f"[eval] complete games={evaluation['games']} wins={evaluation['wins']} "
            f"losses={evaluation['losses']} ties={evaluation['ties']} "
            f"win_rate={evaluation['win_rate']:.3f}"
        )
    if model_name is not None:
        resolved_model_name = str(model_name)
    elif checkpoint_path:
        checkpoint_for_name = Path(str(checkpoint_path))
        if not checkpoint_for_name.is_absolute():
            checkpoint_for_name = Path.cwd() / checkpoint_for_name
        resolved_model_name = f"PokeLearn_{_checkpoint_creation_tag(checkpoint_for_name)}"
    else:
        resolved_model_name = "PokeLearn_unknown"
    gate_passed = bool(evaluation["win_rate"] >= float(eval_threshold))
    progress_payload = _build_model_progress_payload(
        model_name=resolved_model_name,
        evaluation=evaluation,
        eval_threshold=float(eval_threshold),
        checkpoint_path=checkpoint_path,
    )
    _write_model_progress(model_progress_path, progress_payload, verbose=verbose)
    evaluation["modelname"] = resolved_model_name
    evaluation["eval_threshold"] = float(eval_threshold)
    evaluation["gate_passed"] = gate_passed
    return evaluation


def run_eval_from_best_checkpoint(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    log_path: str | Path = "training/battle_logs.jsonl",
    artifact_dir: str | Path = "training/artifacts",
    eval_games: int = 100,
    eval_threshold: float = 0.50,
    model_bonus_weight: float = 120.0,
    model_hidden_sizes: tuple[int, ...] = (128, 64),
    verbose: bool = True,
    eval_print_every_games: int = 10,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    best_pointer_path = artifact_root / "best_model.json"

    checkpoint_path: Path | None = None
    if best_pointer_path.exists():
        try:
            best_payload = dict(json.loads(best_pointer_path.read_text(encoding="utf-8")))
            checkpoint_value = str(best_payload.get("checkpoint_path", "")).strip()
            if checkpoint_value:
                checkpoint_path = Path(checkpoint_value)
        except Exception:
            checkpoint_path = None

    if checkpoint_path is None:
        checkpoint_candidates = sorted((artifact_root / "checkpoints").glob("policy_value_cycle_*.pt"))
        if checkpoint_candidates:
            checkpoint_path = checkpoint_candidates[-1]

    if checkpoint_path is None:
        raise FileNotFoundError(
            "No checkpoint found. Expected best_model.json or a file under training/artifacts/checkpoints."
        )

    if not checkpoint_path.is_absolute():
        checkpoint_path = Path.cwd() / checkpoint_path
    checkpoint_path = checkpoint_path.resolve()
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"Checkpoint does not exist: {checkpoint_path}")

    records = read_log_records(log_path)
    if not records:
        raise ValueError("No training records found; cannot infer model input dimensions for evaluation.")

    model = PolicyValueMLP(
        input_dim=len(records[0].state_features),
        hidden_sizes=model_hidden_sizes,
        action_dim=4,
    )
    load_checkpoint(checkpoint_path, model)
    model_bonus = build_model_bonus_fn(model, weight=model_bonus_weight)
    resolved_model_name = f"PokeLearn_{_checkpoint_creation_tag(checkpoint_path)}"

    if verbose:
        print(f"[eval] loaded checkpoint={checkpoint_path}")
        print(f"[eval] modelname={resolved_model_name}")

    return run_evaluation_games(
        player,
        mechanics=mechanics,
        model=model_bonus,
        eval_games=eval_games,
        eval_threshold=eval_threshold,
        verbose=verbose,
        eval_print_every_games=eval_print_every_games,
        model_name=resolved_model_name,
        model_progress_path=artifact_root / "model_progress.json",
        checkpoint_path=str(checkpoint_path),
    )

# ========================================
# Application Entrypoint
# ========================================


def main() -> int:

    # TO RUN (in bash):
    # cd /home/jeezu/CptS440-PokemonAI/showdownAIproject
    # source .venv/bin/activate
    # python3 -m psai.app.main

    player = pokeEnvPlayerInfo()
    mechanics = MechanicsAPI()
    # run_battle(player, mechanics=mechanics, top_k=3, model=None, max_turns=100)

    # TRAINING CYCLE:
    # Alright, after setting up automatic logging, the training battle loops, and everything else,
    # This is how we run the full training cycle. 
    # 1. If there are no logs, run heuristic training first to generate initial data
    # 2. During the heuristic run, we log all the decisions and outcomes to a file.
    # 3. Then we train the policy/value model on all accumulated logs.
    # 4. After training, we run model-play collection for the configured decision budget.
    # 5. After the cycle completes, we run eval to determine if the winrate is above the accenptable amount.
    # 6. If the eval fails, stop the loop and change training config in train.py.
    
    # training_report = run_training_cycle(
    #     TrainingLoopConfig(
    #         log_path="training/battle_logs.jsonl",
    #         artifact_dir="training/artifacts",
    #         heuristic_decisions=20_000, # heuristic
    #         model_cycle_decisions=10_000,
    #         eval_games=100,
    #         eval_min_win_rate=0.50,
    #         max_cycles=1,
    #         collection_n_games=None,
    #     ),
    #     player,
    #     mechanics,
    # )
    # print(f"Training status: {training_report['status']}")


    # RUN THIS IF TRAINING CYCLE GOT CUT OFF LAST TIME.
    evaluation = run_eval_from_best_checkpoint(
        player,
        mechanics=mechanics,
        log_path="training/battle_logs.jsonl",
        artifact_dir="training/artifacts",
        eval_games=100,
        eval_threshold=0.50,
        verbose=True,
        eval_print_every_games=10,
    )
    print(
        f"Eval result: wins={evaluation['wins']} losses={evaluation['losses']} "
        f"ties={evaluation['ties']} win_rate={evaluation['win_rate']:.3f} "
        f"gate={'pass' if evaluation['gate_passed'] else 'fail'}"
    )


    return 0


if __name__ == "__main__":
    raise SystemExit(main())
