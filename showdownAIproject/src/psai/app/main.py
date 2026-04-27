"""Runtime scaffold for poke-env battle intake and decision handoff."""

# Plain-English summary:
# This module provides a battle-loop scaffold: get battle objects,
# parse each into State, run chooser, and print move suggestions.

# ========================================
# Imports
# ========================================

from __future__ import annotations
from dataclasses import asdict, is_dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.request

from poke_env import AccountConfiguration, ShowdownServerConfiguration
from poke_env.battle.abstract_battle import AbstractBattle
from poke_env.battle.pokemon import Pokemon
from poke_env.player import Player

from psai.app.connections import (
    AsyncConnectionRunner,
    _launch_single_game,
    _maybe_requeue_on_idle,
    _resolve_runner_state,
    _safe_cleanup_finished_battle,
    _safe_ensure_battle_timer_on,
    _safe_reset_battles,
    _safe_send_challenge,
    _wait_for_active_battle,
)
from psai.app.interaction import ConsoleInteractionPort, HttpBridgeInteractionPort, ManualInteractionPort
from psai.app.ui_bridge import set_interaction_port
from psai.decision.chooser import ModelBonusFn, MoveSuggestion, choose_actions
from psai.domain.state import State, parse_battle_to_state
from psai.mechanics.api import MechanicsAPI
from psai.training.dataset import make_log_record, read_log_records, write_log_record
from psai.training.model import PolicyValueMLP
from psai.training.train import (
    TrainConfig,
    TrainingLoopConfig,
    build_model_bonus_fn,
    load_checkpoint,
    run_training_cycle,
)

# UI helpers
from psai.app.ui_bridge import update_state

# ========================================
# Guard Code for Abnormal Shoddown Requests (Like hyperbeam recharge)
# ========================================

_ORIGINAL_AVAILABLE_MOVES_FROM_REQUEST = Pokemon.available_moves_from_request
_ORIGINAL_UPDATE_TEAM_FROM_REQUEST = AbstractBattle._update_team_from_request


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


def _install_poke_env_request_team_guard() -> None:
    current_impl = AbstractBattle._update_team_from_request
    if getattr(current_impl, "__name__", "") == "_psai_update_team_from_request":
        return

    def _psai_update_team_from_request(self: Any, side: dict[str, Any], strict_battle_tracking: bool = False) -> None:
        try:
            _ORIGINAL_UPDATE_TEAM_FROM_REQUEST(self, side, strict_battle_tracking)
            return
        except KeyError:
            # Server restarts and reconnect races can occasionally produce request payloads
            # whose side identifiers are not yet present in poke-env's local team map.
            # Best-effort repair the mapping and retry once instead of wedging the battle loop.
            pass

        pokemon_payload = side.get("pokemon", []) if isinstance(side, dict) else []
        repaired = False
        for pokemon in pokemon_payload:
            if not isinstance(pokemon, dict):
                continue
            ident = str(pokemon.get("ident", "") or "")
            if not ident:
                continue
            details = str(pokemon.get("details", "") or "")
            try:
                self.get_pokemon(
                    ident,
                    force_self_team=True,
                    details=details,
                    request=pokemon,
                )
                repaired = True
            except Exception:
                continue

        if repaired:
            try:
                _ORIGINAL_UPDATE_TEAM_FROM_REQUEST(self, side, strict_battle_tracking)
                return
            except KeyError:
                pass

        # Last-resort graceful path: avoid raising and update what we can.
        for pokemon in pokemon_payload:
            if not isinstance(pokemon, dict):
                continue
            ident = str(pokemon.get("ident", "") or "")
            if not ident:
                continue
            details = str(pokemon.get("details", "") or "")
            mon = self.team.get(ident)
            if mon is None:
                try:
                    mon = self.get_pokemon(
                        ident,
                        force_self_team=True,
                        details=details,
                        request=pokemon,
                    )
                except Exception:
                    continue
            try:
                mon.update_from_request(pokemon)
            except Exception:
                continue

    AbstractBattle._update_team_from_request = _psai_update_team_from_request


_install_poke_env_request_team_guard()

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


def _format_status_text(status: str | None) -> str:
    return str(status or "none")


def _format_types_text(types: tuple[str, ...]) -> str:
    if not types:
        return "unknown"
    return "/".join(types)


def _format_boosts_text(boosts: dict[str, int]) -> str:
    non_zero = [f"{name}:{value:+d}" for name, value in boosts.items() if int(value) != 0]
    return ", ".join(non_zero) if non_zero else "-"


def _format_hp_percent(hp_fraction: float) -> str:
    return f"{max(0.0, min(1.0, float(hp_fraction))) * 100.0:.1f}%"


def _extract_pokemon_hp_fraction(raw_pokemon: Any) -> float | None:
    for attr_name in ("current_hp_fraction", "hp_fraction"):
        value = getattr(raw_pokemon, attr_name, None)
        if value is None:
            continue
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue

    current_hp = getattr(raw_pokemon, "current_hp", None)
    max_hp = getattr(raw_pokemon, "max_hp", None)
    try:
        current_hp_value = float(current_hp)
        max_hp_value = float(max_hp)
    except (TypeError, ValueError):
        return None
    if max_hp_value <= 0:
        return None
    return max(0.0, min(1.0, current_hp_value / max_hp_value))


def _extract_pokemon_status_text(raw_pokemon: Any) -> str:
    status = getattr(raw_pokemon, "status", None)
    if status is None:
        return "none"
    return str(getattr(status, "name", status))


def _extract_pokemon_types_text(raw_pokemon: Any) -> str:
    types = getattr(raw_pokemon, "types", None)
    if not types:
        return "unknown"
    names: list[str] = []
    for pokemon_type in tuple(types):
        if pokemon_type is None:
            continue
        names.append(str(getattr(pokemon_type, "name", pokemon_type)))
    return "/".join(names) if names else "unknown"


def _render_ascii_battle_view(
    state: State,
    battle: Any,
    *,
    board_width: int = 96,
) -> str:
    inner_width = max(40, board_width - 4)

    def _line(text: str = "") -> str:
        clipped = text[:inner_width]
        return f"| {clipped:<{inner_width}} |"

    border = "+" + "-" * (inner_width + 2) + "+"
    battle_tag = str(getattr(battle, "battle_tag", "") or "unknown")
    turn_value = state.turn_number if state.turn_number is not None else state.turn

    opponent = state.opponent_active
    friendly = state.friendly_active
    active_section = [
        border,
        _line(f"Battle {battle_tag} | Turn {turn_value} | Mode: {state.request_mode}"),
        border,
        _line("TOP HALF: Active Pokemon"),
        _line(
            f"Opponent: {opponent.species} | HP { _format_hp_percent(opponent.hp_fraction) } | "
            f"Status { _format_status_text(opponent.status) } | Types { _format_types_text(opponent.types) }"
        ),
        _line(f"Opponent boosts: {_format_boosts_text(opponent.boosts)}"),
        _line(
            f"You:      {friendly.species} | HP { _format_hp_percent(friendly.hp_fraction) } | "
            f"Status { _format_status_text(friendly.status) } | Types { _format_types_text(friendly.types) }"
        ),
        _line(f"Your boosts: {_format_boosts_text(friendly.boosts)}"),
        border,
        _line("BOTTOM HALF: Available Actions"),
    ]

    available_moves = list(getattr(battle, "available_moves", []) or [])
    available_switches = list(getattr(battle, "available_switches", []) or [])

    if available_moves:
        active_section.append(_line("Moves:"))
        for index, move in enumerate(available_moves, start=1):
            move_name = str(getattr(move, "id", None) or getattr(move, "move", None) or f"move_{index}")
            pp_current = getattr(move, "current_pp", None)
            if pp_current is None:
                pp_current = getattr(move, "pp", None)
            pp_max = getattr(move, "max_pp", None)
            if pp_max is None:
                pp_max = getattr(move, "maxpp", None)
            pp_text = f" PP {pp_current}/{pp_max}" if pp_current is not None and pp_max is not None else ""
            move_type = getattr(move, "type", None)
            move_type_text = str(getattr(move_type, "name", move_type)) if move_type is not None else "unknown"
            active_section.append(_line(f"  [{index}] {move_name} | Type {move_type_text}{pp_text}"))
    else:
        active_section.append(_line("Moves: none"))

    if available_switches:
        active_section.append(_line("Switches:"))
        for index, switch_target in enumerate(available_switches, start=1):
            switch_name = str(
                getattr(switch_target, "species", None)
                or getattr(switch_target, "name", None)
                or f"switch_{index}"
            )
            hp_fraction = _extract_pokemon_hp_fraction(switch_target)
            hp_text = _format_hp_percent(hp_fraction) if hp_fraction is not None else "unknown"
            status_text = _extract_pokemon_status_text(switch_target)
            types_text = _extract_pokemon_types_text(switch_target)
            active_section.append(
                _line(f"  [{index}] {switch_name} | HP {hp_text} | Status {status_text} | Types {types_text}")
            )
    else:
        active_section.append(_line("Switches: none"))

    active_section.append(border)
    return "\n".join(active_section)


def _print_ranked_suggestions_short(turn_suggestions: list[MoveSuggestion], *, top_k: int = 3) -> None:
    print("Top model suggestions:")
    if not turn_suggestions:
        print("  (none available for this request)")
        return

    for suggestion in turn_suggestions[: max(1, int(top_k))]:
        reason_text = "; ".join(suggestion.reasons[:2]) if suggestion.reasons else "no breakdown available"
        print(
            f"  #{suggestion.rank} {suggestion.action.action_id} "
            f"(score={suggestion.score:.2f}) -> {reason_text}"
        )


def _timed_input(prompt: str, timeout_seconds: float) -> str | None:
    timeout = max(0.0, float(timeout_seconds))
    print(prompt, end="", flush=True)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except Exception:
        try:
            value = input()
        except EOFError:
            return None
        return value.strip()

    if not ready:
        print("")
        return None

    line = sys.stdin.readline()
    if line == "":
        return None
    return line.strip()


def _interaction_emit(interaction_port: ManualInteractionPort | None, text: str) -> None:
    if interaction_port is None:
        print(text)
        return
    interaction_port.emit(text)


def _interaction_prompt(
    interaction_port: ManualInteractionPort | None,
    *,
    kind: str,
    message: str,
    options: list[dict[str, str]] | None = None,
    timeout_seconds: float | None = None,
    allow_text: bool = False,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    if interaction_port is None:
        if timeout_seconds is None:
            try:
                return input(message).strip()
            except EOFError:
                return None
        return _timed_input(message, timeout_seconds)

    return interaction_port.prompt(
        kind=kind,
        message=message,
        options=options,
        timeout_seconds=timeout_seconds,
        allow_text=allow_text,
        metadata=metadata,
    )


def _build_recommendation_ranks(turn_suggestions: list[MoveSuggestion]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for suggestion in turn_suggestions:
        action_id = str(getattr(suggestion.action, "action_id", "") or "")
        if action_id and action_id not in ranks:
            ranks[action_id] = int(suggestion.rank)
    return ranks


def _recommendation_suffix(action_id: str, rank_lookup: dict[str, int]) -> str:
    rank = rank_lookup.get(action_id)
    if rank is None:
        return ""
    return f" (recommended #{rank})"


def _extract_request_remaining_seconds(battle: Any) -> int | None:
    request = getattr(battle, "_last_request", None)
    candidate_values: list[Any] = []

    if isinstance(request, dict):
        for key in (
            "secondsLeft",
            "secondsleft",
            "timeLeft",
            "timeleft",
            "remaining",
            "remainingSeconds",
            "maxMoveTime",
            "maxmovetime",
            "timer",
        ):
            candidate_values.append(request.get(key))
        side_payload = request.get("side")
        if isinstance(side_payload, dict):
            for key in ("secondsLeft", "timeLeft", "remaining", "timer"):
                candidate_values.append(side_payload.get(key))

    for attr_name in ("seconds_left", "time_left", "remaining_seconds", "remaining_time"):
        candidate_values.append(getattr(battle, attr_name, None))

    for raw_value in candidate_values:
        if raw_value is None:
            continue
        try:
            seconds = int(float(raw_value))
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds
    return None


def _prompt_user_choice_for_request(
    player: Any,
    battle: Any,
    state: State,
    turn_suggestions: list[MoveSuggestion],
    *,
    user_timeout_seconds: float = 60.0,
    interaction_port: ManualInteractionPort | None = None,
) -> tuple[Any, str]:
    auto_order, auto_action_id = _choose_order_for_request(
        player,
        battle,
        state,
        turn_suggestions,
    )

    request_seconds_remaining = _extract_request_remaining_seconds(battle)
    if request_seconds_remaining is not None and request_seconds_remaining <= 30:
        _interaction_emit(
            interaction_port,
            f"[manual] timer is at {request_seconds_remaining}s. "
            "Auto-selecting #1 ranked move.",
        )
        return auto_order, auto_action_id

    available_moves = list(getattr(battle, "available_moves", []) or [])
    available_switches = list(getattr(battle, "available_switches", []) or [])
    if not available_moves and not available_switches:
        _interaction_emit(interaction_port, "[manual] no selectable actions found. Using automatic fallback.")
        return auto_order, auto_action_id

    recommendation_ranks = _build_recommendation_ranks(turn_suggestions)
    deadline = time.time() + max(1.0, float(user_timeout_seconds))

    while True:
        request_seconds_remaining = _extract_request_remaining_seconds(battle)
        if request_seconds_remaining is not None and request_seconds_remaining <= 30:
            _interaction_emit(
                interaction_port,
                f"[manual] timer is at {request_seconds_remaining}s. "
                "Auto-selecting #1 ranked move.",
            )
            return auto_order, auto_action_id

        remaining = deadline - time.time()
        if remaining <= 0:
            _interaction_emit(interaction_port, "[manual] no user selection within 60 seconds. Auto-selecting #1 ranked move.")
            return auto_order, auto_action_id

        mode_choice = _interaction_prompt(
            interaction_port,
            kind="battle_mode",
            message=f"Choose 1, 2, or 3 ({int(max(1, remaining))}s left): ",
            options=[
                {"id": "1", "label": "Attack"},
                {"id": "2", "label": "Switch"},
                {"id": "3", "label": "Forfeit"},
            ],
            timeout_seconds=min(remaining, 20.0),
            metadata={
                "battle_tag": str(getattr(battle, "battle_tag", "") or ""),
                "turn": int(getattr(state, "turn_number", 0) or 0),
            },
        )
        if mode_choice is None:
            continue
        if mode_choice not in {"1", "2", "3"}:
            _interaction_emit(interaction_port, "[manual] invalid choice; please enter 1, 2, or 3.")
            continue

        if mode_choice == "3":
            remaining = deadline - time.time()
            if remaining <= 0:
                _interaction_emit(interaction_port, "[manual] input timed out. Auto-selecting #1 ranked move.")
                return auto_order, auto_action_id
            confirmation = _interaction_prompt(
                interaction_port,
                kind="forfeit_confirm",
                message=f"Confirm forfeit ({int(max(1, remaining))}s left): ",
                options=[
                    {"id": "confirm", "label": "Confirm Forfeit"},
                    {"id": "cancel", "label": "Cancel"},
                ],
                timeout_seconds=min(remaining, 20.0),
                allow_text=True,
            )
            if confirmation is None:
                continue
            normalized_confirmation = confirmation.strip().lower()
            if normalized_confirmation not in {"confirm", "forfeit", "yes"}:
                _interaction_emit(interaction_port, "[manual] forfeit cancelled.")
                continue
            chosen_order = player.create_order("/forfeit")
            return chosen_order, "forfeit"

        if mode_choice == "1":
            if not available_moves:
                _interaction_emit(interaction_port, "[manual] no attack options available this turn.")
                continue
            attack_options: list[dict[str, str]] = []
            for index, move in enumerate(available_moves, start=1):
                action_id = _move_action_id_from_move(move)
                move_name = str(getattr(move, "id", None) or getattr(move, "move", None) or f"move_{index}")
                suffix = _recommendation_suffix(action_id, recommendation_ranks)
                attack_options.append(
                    {
                        "id": str(index),
                        "label": f"{move_name}{suffix}",
                    }
                )

            remaining = deadline - time.time()
            if remaining <= 0:
                _interaction_emit(interaction_port, "[manual] input timed out. Auto-selecting #1 ranked move.")
                return auto_order, auto_action_id
            slot_text = _interaction_prompt(
                interaction_port,
                kind="attack_slot",
                message=f"Select attack number ({int(max(1, remaining))}s left): ",
                options=attack_options,
                timeout_seconds=min(remaining, 20.0),
                metadata={
                    "battle_tag": str(getattr(battle, "battle_tag", "") or ""),
                },
            )
            if slot_text is None:
                continue
            try:
                slot_index = int(slot_text)
            except ValueError:
                _interaction_emit(interaction_port, "[manual] invalid attack selection.")
                continue
            if slot_index < 1 or slot_index > len(available_moves):
                _interaction_emit(interaction_port, "[manual] attack selection is out of range.")
                continue

            selected_move = available_moves[slot_index - 1]
            chosen_order = player.create_order(f"/choose move {slot_index}")
            chosen_action_id = _move_action_id_from_move(selected_move)
            return chosen_order, chosen_action_id

        if not available_switches:
            _interaction_emit(interaction_port, "[manual] no switch options available this turn.")
            continue
        switch_options: list[dict[str, str]] = []
        for index, switch_target in enumerate(available_switches, start=1):
            action_id = _switch_action_id_from_target(switch_target)
            switch_name = str(
                getattr(switch_target, "species", None)
                or getattr(switch_target, "name", None)
                or f"switch_{index}"
            )
            suffix = _recommendation_suffix(action_id, recommendation_ranks)
            switch_options.append(
                {
                    "id": str(index),
                    "label": f"{switch_name}{suffix}",
                }
            )

        remaining = deadline - time.time()
        if remaining <= 0:
            _interaction_emit(interaction_port, "[manual] input timed out. Auto-selecting #1 ranked move.")
            return auto_order, auto_action_id
        slot_text = _interaction_prompt(
            interaction_port,
            kind="switch_slot",
            message=f"Select switch number ({int(max(1, remaining))}s left): ",
            options=switch_options,
            timeout_seconds=min(remaining, 20.0),
            metadata={
                "battle_tag": str(getattr(battle, "battle_tag", "") or ""),
            },
        )
        if slot_text is None:
            continue
        try:
            slot_index = int(slot_text)
        except ValueError:
            _interaction_emit(interaction_port, "[manual] invalid switch selection.")
            continue
        if slot_index < 1 or slot_index > len(available_switches):
            _interaction_emit(interaction_port, "[manual] switch selection is out of range.")
            continue

        selected_switch = available_switches[slot_index - 1]
        chosen_order = player.create_order(selected_switch)
        chosen_action_id = _switch_action_id_from_target(selected_switch)
        return chosen_order, chosen_action_id


def _write_player_wr_log(
    path: Path,
    *,
    battle: Any,
    result: str,
    mode: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "battle_tag": str(getattr(battle, "battle_tag", "") or ""),
        "mode": str(mode),
        "opponent": str(getattr(battle, "opponent_username", None) or "unknown"),
        "result": str(result),
        "turns": int(getattr(battle, "turn", 0) or 0),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
        handle.write("\n")


def _print_player_wr_summary(
    path: Path,
    *,
    interaction_port: ManualInteractionPort | None = None,
) -> None:
    if not path.exists():
        _interaction_emit(interaction_port, "No all-time winrate records yet.")
        return

    wins = 0
    losses = 0
    ties = 0
    total = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            total += 1
            result = str(payload.get("result", "")).strip().lower()
            if result == "win":
                wins += 1
            elif result == "loss":
                losses += 1
            else:
                ties += 1

    wr = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0
    _interaction_emit(interaction_port, "All-time Battle Record")
    _interaction_emit(interaction_port, f"  Total battles: {total}")
    _interaction_emit(interaction_port, f"  Wins: {wins}")
    _interaction_emit(interaction_port, f"  Losses: {losses}")
    _interaction_emit(interaction_port, f"  Ties/Other: {ties}")
    _interaction_emit(interaction_port, f"  Win rate (excl ties): {wr * 100.0:.2f}%")


def _print_outcome_banner(
    result: str,
    *,
    interaction_port: ManualInteractionPort | None = None,
) -> None:
    if result == "win":
        banner = [
            "+-----------------------------+",
            "|           VICTORY           |",
            "+-----------------------------+",
        ]
    elif result == "loss":
        banner = [
            "+-----------------------------+",
            "|            DEFEAT           |",
            "+-----------------------------+",
        ]
    else:
        banner = [
            "+-----------------------------+",
            "|             TIE             |",
            "+-----------------------------+",
        ]
    for line in banner:
        _interaction_emit(interaction_port, line)


def _run_manual_connected_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    top_k: int,
    model: ModelBonusFn | None,
    launch_mode: str,
    runner: AsyncConnectionRunner | None = None,
    waiting_message: str,
    wr_log_path: Path,
    max_turns: int | None = None,
    user_timeout_seconds: float = 60.0,
    state_callback: Any = None,
    interaction_port: ManualInteractionPort | None = None,
) -> None:
    battle, runner = _wait_for_active_battle(
        player,
        phase_label="manual",
        waiting_message=waiting_message,
        runner=runner,
        allow_requeue=(launch_mode == "ladder"),
        timeout_seconds=240.0 if launch_mode == "ladder" else 180.0,
    )
    if battle is None:
        _interaction_emit(interaction_port, "[manual] no battle was found before timeout. Returning to menu.")
        return

    battle_tag = str(getattr(battle, "battle_tag", id(battle)) or id(battle))
    _interaction_emit(interaction_port, f"[manual] connected to battle {battle_tag}")

    previous_pending_timeout = float(getattr(player, "_pending_order_timeout_seconds", 15.0))
    if hasattr(player, "_pending_order_timeout_seconds"):
        setattr(player, "_pending_order_timeout_seconds", max(previous_pending_timeout, user_timeout_seconds + 10.0))

    turns_ran = 0
    last_prompted_request: tuple[Any, ...] | None = None
    last_prompted_at = 0.0
    last_submitted_order: Any | None = None
    retry_same_request_after_seconds = 15.0
    manual_forfeit_requested = False

    try:
        while True:
            battles = dict(getattr(player, "battles", {}) or {})
            battle = battles.get(battle_tag, battle)
            if getattr(battle, "finished", False):
                break

            _safe_ensure_battle_timer_on(player, battle_tag, phase_label="manual", verbose=True)
            request_signature = _battle_request_signature(battle)
            now = time.time()
            if last_prompted_request == request_signature:
                elapsed = now - last_prompted_at
                if elapsed >= retry_same_request_after_seconds and last_submitted_order is not None:
                    _interaction_emit(
                        interaction_port,
                        f"[manual] request_stalled battle={battle_tag} "
                        f"waited={elapsed:.1f}s; resubmitting latest order",
                    )
                    if hasattr(player, "set_pending_order"):
                        player.set_pending_order(battle_tag, last_submitted_order)
                    last_prompted_at = now
                time.sleep(0.1)
                continue

            try:
                state = parse_battle_to_state(battle)
            except Exception as exc:
                _interaction_emit(
                    interaction_port,
                    f"[manual] parse_state_failed battle={battle_tag} "
                    f"error={type(exc).__name__}: {exc}. Sending default order.",
                )
                chosen_order = _default_order(player)
                if hasattr(player, "set_pending_order"):
                    player.set_pending_order(battle_tag, chosen_order)
                last_prompted_request = request_signature
                last_prompted_at = now
                last_submitted_order = chosen_order
                continue

            if not _has_actionable_request(state, battle):
                time.sleep(0.1)
                continue

            turn_suggestions = (
                get_turn_suggestions(state, mechanics, top_k=top_k, model=model)
                if state.legal_actions
                else []
            )
            
            # using this state for ui payloads
            if state_callback is not None:
                # add turn suggestions for model reponse
                state_callback(state, turn_suggestions)

            _interaction_emit(interaction_port, _render_ascii_battle_view(state, battle))
            _interaction_emit(interaction_port, "Top model suggestions:")
            if not turn_suggestions:
                _interaction_emit(interaction_port, "  (none available for this request)")
            else:
                for suggestion in turn_suggestions[: max(1, int(top_k))]:
                    reason_text = "; ".join(suggestion.reasons[:2]) if suggestion.reasons else "no breakdown available"
                    _interaction_emit(
                        interaction_port,
                        (
                            f"  #{suggestion.rank} {suggestion.action.action_id} "
                            f"(score={suggestion.score:.2f}) -> {reason_text}"
                        ),
                    )
            chosen_order, chosen_action_id = _prompt_user_choice_for_request(
                player,
                battle,
                state,
                turn_suggestions,
                user_timeout_seconds=user_timeout_seconds,
                interaction_port=interaction_port,
            )
            _interaction_emit(interaction_port, f"[manual] selected action: {chosen_action_id}")

            if hasattr(player, "set_pending_order"):
                player.set_pending_order(battle_tag, chosen_order)

            last_prompted_request = request_signature
            last_prompted_at = now
            last_submitted_order = chosen_order

            if chosen_action_id == "forfeit":
                manual_forfeit_requested = True
                _interaction_emit(interaction_port, "[manual] forfeit submitted.")
                break

            turns_ran += 1
            if max_turns is not None and turns_ran >= max_turns:
                _interaction_emit(interaction_port, f"[manual] max_turns={max_turns} reached; stopping manual battle loop.")
                break

            time.sleep(0.1)
    finally:
        if hasattr(player, "_pending_order_timeout_seconds"):
            setattr(player, "_pending_order_timeout_seconds", previous_pending_timeout)

    outcome_value, result_label = _battle_outcome_value(battle)
    if manual_forfeit_requested:
        outcome_value = -1.0
        result_label = "loss"

    if getattr(battle, "finished", False) or manual_forfeit_requested:
        _print_outcome_banner(result_label, interaction_port=interaction_port)
        _write_player_wr_log(
            wr_log_path,
            battle=battle,
            result=result_label,
            mode=launch_mode,
        )
        _safe_cleanup_finished_battle(player, battle_tag, phase_label="manual", verbose=True)
    else:
        _interaction_emit(interaction_port, "[manual] battle loop ended before battle finished.")

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
    wr_log_path: str | Path = "training/player_WR_log.jsonl",
    user_timeout_seconds: float = 60.0,
    runtime_log_path: str | Path = "training/battle_logs.jsonl",
    runtime_artifact_dir: str | Path = "training/artifacts",
    runtime_model_bonus_weight: float = 90.0,
    runtime_default_hidden_sizes: tuple[int, ...] = (256, 128),
    state_callback: Any = None, # to expose state info to the UI , through run_manual_connected_battle (more details of battle)
    model_checkpoint_path: str | Path | None = None,
    interaction_port: ManualInteractionPort | None = None,
) -> None:
    # Manual product mode menu:
    # 1) Ladder game
    # 2) Challenge a player
    # 3) View all-time win rate
    # 4) Quit
    wr_log = Path(wr_log_path)
    resolved_interaction_port: ManualInteractionPort = interaction_port or ConsoleInteractionPort()
    resolved_model = model
    if resolved_model is None:
        resolved_model, checkpoint_path = _load_runtime_model_bonus(
            log_path=runtime_log_path,
            artifact_dir=runtime_artifact_dir,
            model_bonus_weight=runtime_model_bonus_weight,
            default_hidden_sizes=runtime_default_hidden_sizes,
            checkpoint_path_override=model_checkpoint_path,
        )
        if checkpoint_path is None:
            _interaction_emit(resolved_interaction_port, "[manual] launching with heuristic-only suggestions.")

    while True:
        _interaction_emit(resolved_interaction_port, "")
        _interaction_emit(resolved_interaction_port, "==============================================")
        _interaction_emit(resolved_interaction_port, "Pokemon AI Console Menu")
        _interaction_emit(resolved_interaction_port, "1. Connect to a ladder game")
        _interaction_emit(resolved_interaction_port, "2. Challenge a player")
        _interaction_emit(resolved_interaction_port, "3. View my all-time winrate")
        _interaction_emit(resolved_interaction_port, "4. Quit")
        _interaction_emit(resolved_interaction_port, "==============================================")
        menu_choice = _interaction_prompt(
            resolved_interaction_port,
            kind="main_menu",
            message="Select option (1-4): ",
            options=[
                {"id": "1", "label": "Connect to ladder"},
                {"id": "2", "label": "Challenge a player"},
                {"id": "3", "label": "View all-time winrate"},
                {"id": "4", "label": "Quit"},
            ],
            timeout_seconds=None,
        )
        if menu_choice is None:
            continue
        menu_choice = str(menu_choice).strip()

        if menu_choice == "1":
            _interaction_emit(resolved_interaction_port, "[manual] launching ladder game...")
            _safe_reset_battles(player)
            runner = _launch_single_game(player)
            _run_manual_connected_battle(
                player,
                mechanics=mechanics,
                top_k=top_k,
                model=resolved_model,
                launch_mode="ladder",
                runner=runner,
                waiting_message="waiting for ladder opponent",
                wr_log_path=wr_log,
                max_turns=max_turns,
                user_timeout_seconds=user_timeout_seconds,
                state_callback=state_callback,
                interaction_port=resolved_interaction_port,
            )
            continue

        if menu_choice == "2":
            opponent_name = _interaction_prompt(
                resolved_interaction_port,
                kind="challenge_username",
                message="Enter player name to challenge: ",
                timeout_seconds=None,
                allow_text=True,
            )
            if opponent_name is not None:
                opponent_name = str(opponent_name).strip()
            if not opponent_name:
                _interaction_emit(resolved_interaction_port, "[manual] challenge cancelled: no opponent entered.")
                continue
            _safe_reset_battles(player)
            _interaction_emit(resolved_interaction_port, f"[manual] sending challenge to {opponent_name}...")
            if not _safe_send_challenge(player, opponent_name, phase_label="manual"):
                _interaction_emit(resolved_interaction_port, "[manual] challenge was not sent. Returning to menu.")
                continue
            _run_manual_connected_battle(
                player,
                mechanics=mechanics,
                top_k=top_k,
                model=resolved_model,
                launch_mode="challenge",
                runner=None,
                waiting_message=f"waiting for {opponent_name} to accept challenge",
                wr_log_path=wr_log,
                max_turns=max_turns,
                user_timeout_seconds=user_timeout_seconds,
                state_callback=state_callback,
                interaction_port=resolved_interaction_port,
            )
            continue

        if menu_choice == "3":
            _print_player_wr_summary(
                wr_log,
                interaction_port=resolved_interaction_port,
            )
            continue

        if menu_choice == "4":
            _interaction_emit(resolved_interaction_port, "[manual] exiting menu.")
            break

        _interaction_emit(resolved_interaction_port, "[manual] invalid menu option. Please select 1-4.")


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
            runner_started_at, idle_requeue_attempts = _maybe_requeue_on_idle(
                player,
                runner=runner,
                runner_started_at=runner_started_at,
                idle_requeue_attempts=idle_requeue_attempts,
                phase_label=phase_label,
                verbose=verbose,
            )

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


def _normalize_training_config(training_config: Any | None) -> dict[str, Any] | None:
    if training_config is None:
        return None
    if is_dataclass(training_config):
        return dict(asdict(training_config))
    if isinstance(training_config, dict):
        return dict(training_config)
    return {"value": str(training_config)}


def _extract_cycle_id_from_checkpoint(checkpoint_path: Path) -> int | None:
    match = re.fullmatch(r"policy_value_cycle_(\d+)(?:_run_\d+)?\.pt", checkpoint_path.name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _load_training_config_for_checkpoint(
    artifact_root: Path,
    checkpoint_path: Path,
) -> dict[str, Any] | None:
    cycle_id = _extract_cycle_id_from_checkpoint(checkpoint_path)
    if cycle_id is None:
        return None

    cycle_metrics_path = artifact_root / "metrics" / f"cycle_{cycle_id:04d}.json"
    if not cycle_metrics_path.exists():
        return None

    try:
        cycle_payload = json.loads(cycle_metrics_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    training_config = cycle_payload.get("train_config")
    if isinstance(training_config, dict):
        return dict(training_config)
    return None


def _build_model_progress_payload(
    *,
    model_name: str,
    evaluation: dict[str, Any],
    eval_threshold: float,
    checkpoint_path: str | None = None,
    training_config: dict[str, Any] | None = None,
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
    if training_config is not None:
        payload["training_config"] = dict(training_config)
    return payload


def _write_model_progress(
    path: str | Path,
    payload: dict[str, Any],
    *,
    verbose: bool = True,
) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    existing_entries: list[dict[str, Any]] = []
    if output_path.exists():
        try:
            existing_payload = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception:
            existing_payload = None

        if isinstance(existing_payload, list):
            existing_entries = [dict(entry) for entry in existing_payload if isinstance(entry, dict)]
        elif isinstance(existing_payload, dict):
            models_list = existing_payload.get("models")
            if isinstance(models_list, list):
                existing_entries = [dict(entry) for entry in models_list if isinstance(entry, dict)]
            elif "models_evaluation_stats" in existing_payload:
                existing_entries = [dict(existing_payload)]

    def _entry_key(entry: dict[str, Any]) -> tuple[str, str]:
        checkpoint = str(entry.get("checkpoint_path", "") or "")
        model_name = str(entry.get("modelname", "") or "")
        return checkpoint, model_name

    new_entry = dict(payload)
    new_key = _entry_key(new_entry)
    replaced = False
    for index, entry in enumerate(existing_entries):
        if _entry_key(entry) == new_key:
            existing_entries[index] = new_entry
            replaced = True
            break
    if not replaced:
        existing_entries.append(new_entry)

    output_document = {
        "models": existing_entries,
        "latest": existing_entries[-1] if existing_entries else new_entry,
    }
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(output_document, handle, indent=2, sort_keys=True)
    if verbose:
        print(f"[eval] wrote model progress -> {output_path}")


def run_evaluation_games(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    checkpoint_path: str | Path,
    log_path: str | Path = "training/battle_logs.jsonl",
    artifact_dir: str | Path = "training/artifacts",
    eval_games: int = 100,
    eval_threshold: float = 0.50,
    model_bonus_weight: float = 120.0,
    model_hidden_sizes: tuple[int, ...] = (128, 64),
    verbose: bool = True,
    eval_print_every_games: int = 10,
    top_k: int = 3,
    print_top_k: int | None = None,
    print_turn_suggestions: bool = True,
    model_progress_path: str | Path | None = None,
    training_config: Any | None = None,
    champion_min_delta: float = 0.03,
    champion_min_games: int = 300,
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
    
    # we now also use a champion system for determining our current best model, where whenever a new model is trained,
    # it is compared to the current "champion" or best model, and depnding on the results a new king is crowned.

    artifact_root = Path(artifact_dir)
    resolved_checkpoint_path = Path(checkpoint_path)
    if not resolved_checkpoint_path.is_absolute():
        resolved_checkpoint_path = Path.cwd() / resolved_checkpoint_path
    resolved_checkpoint_path = resolved_checkpoint_path.resolve()
    if not resolved_checkpoint_path.exists():
        raise FileNotFoundError(f"Checkpoint does not exist: {resolved_checkpoint_path}")

    records = read_log_records(log_path)
    if not records:
        raise ValueError("No training records found; cannot infer model input dimensions for evaluation.")

    model = PolicyValueMLP(
        input_dim=len(records[0].state_features),
        hidden_sizes=model_hidden_sizes,
        action_dim=4,
    )
    checkpoint_payload = load_checkpoint(resolved_checkpoint_path, model)
    model_bonus = build_model_bonus_fn(model, weight=model_bonus_weight)
    resolved_model_name = f"PokeLearn_{_checkpoint_creation_tag(resolved_checkpoint_path)}"

    resolved_training_config = _normalize_training_config(training_config)
    if resolved_training_config is None:
        checkpoint_training_config = checkpoint_payload.get("train_config")
        if isinstance(checkpoint_training_config, dict):
            resolved_training_config = dict(checkpoint_training_config)
    if resolved_training_config is None:
        resolved_training_config = _load_training_config_for_checkpoint(
            artifact_root,
            resolved_checkpoint_path,
        )

    resolved_model_progress_path = (
        Path(model_progress_path) if model_progress_path is not None else artifact_root / "model_progress.json"
    )

    if verbose:
        print(f"[eval] loaded checkpoint={resolved_checkpoint_path}")
        print(f"[eval] modelname={resolved_model_name}")
        print(f"[loop] evaluation start games={eval_games}")

    # makes our default evaluate results
    if eval_games <= 0:
        return {"games": 0, "wins": 0, "losses": 0, "ties": 0, "win_rate": 0.0}

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
    resolved_top_k = max(1, int(top_k))
    resolved_print_top_k = int(print_top_k if print_top_k is not None else resolved_top_k)

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
            battle_result = "tie"
            if won is True:
                battle_result = "win"
            elif won is False:
                battle_result = "loss"
            last_prompted_request.pop(battle_tag_str, None)
            last_prompted_at.pop(battle_tag_str, None)
            _safe_cleanup_finished_battle(
                player,
                battle_tag_str,
                phase_label="eval",
                verbose=False,
            )
            if verbose:
                print(
                    f"[eval] battle_finished tag={battle_tag_str} result={battle_result} "
                    f"games={games_finished}/{eval_games}"
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
            runner = _launch_single_game(player)
            runner_started_at = time.time()
            games_launched += 1
            if verbose:
                print(f"[eval] launched ladder game #{games_launched}/{eval_games}")

        if runner is not None and not runner.done and not active_battles:
            runner_started_at, idle_requeue_attempts = _maybe_requeue_on_idle(
                player,
                runner=runner,
                runner_started_at=runner_started_at,
                idle_requeue_attempts=idle_requeue_attempts,
                phase_label="eval",
                verbose=verbose,
            )

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
                    top_k=resolved_top_k,
                    model=model_bonus,
                )
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
                    "eval",
                    battle_tag,
                    state,
                    turn_suggestions,
                    chosen_action_id,
                    max_suggestions=resolved_print_top_k,
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

    gate_passed = bool(evaluation["win_rate"] >= float(eval_threshold))

    # Champion/challenger promotion:
    # Promote challenger if it beats current champion by at least champion_min_delta
    # on at least champion_min_games evaluated games.
    best_pointer_path = artifact_root / "best_model.json"
    existing_champion_payload: dict[str, Any] | None = None
    if best_pointer_path.exists():
        try:
            existing_champion_payload = dict(json.loads(best_pointer_path.read_text(encoding="utf-8")))
        except Exception:
            existing_champion_payload = None

    champion_checkpoint_before = None
    champion_win_rate_before = None
    if isinstance(existing_champion_payload, dict):
        checkpoint_value = str(existing_champion_payload.get("checkpoint_path", "")).strip()
        if checkpoint_value:
            champion_checkpoint_before = checkpoint_value
        try:
            champion_win_rate_before = float(existing_champion_payload.get("win_rate"))
        except (TypeError, ValueError):
            champion_win_rate_before = None

    challenger_checkpoint = str(resolved_checkpoint_path)
    challenger_win_rate = float(evaluation["win_rate"])
    resolved_min_delta = max(0.0, float(champion_min_delta))
    resolved_min_games = max(1, int(champion_min_games))
    meets_min_games = int(evaluation["games"]) >= resolved_min_games
    is_same_checkpoint = bool(champion_checkpoint_before) and champion_checkpoint_before == challenger_checkpoint

    champion_promoted = False
    promotion_reason = ""

    if is_same_checkpoint:
        promotion_reason = "challenger_is_current_champion"
    elif champion_checkpoint_before is None or champion_win_rate_before is None:
        # No champion exists yet: accept this challenger as initial champion.
        champion_promoted = True
        promotion_reason = "no_existing_champion"
    elif not meets_min_games:
        promotion_reason = (
            f"insufficient_eval_games:{int(evaluation['games'])}<{resolved_min_games}"
        )
    elif challenger_win_rate >= (champion_win_rate_before + resolved_min_delta):
        champion_promoted = True
        promotion_reason = "challenger_beats_champion"
    else:
        win_rate_delta = challenger_win_rate - champion_win_rate_before
        promotion_reason = (
            f"delta_below_requirement:{win_rate_delta:.4f}<{resolved_min_delta:.4f}"
        )

    champion_checkpoint_after = champion_checkpoint_before
    champion_win_rate_after = champion_win_rate_before

    if champion_promoted:
        champion_checkpoint_after = challenger_checkpoint
        champion_win_rate_after = challenger_win_rate
        best_pointer_payload = {
            "checkpoint_path": challenger_checkpoint,
            "win_rate": challenger_win_rate,
            "games": int(evaluation["games"]),
            "promoted_at_utc": datetime.utcnow().isoformat() + "Z",
            "promotion_rule": {
                "min_delta": resolved_min_delta,
                "min_games": resolved_min_games,
            },
        }
        best_pointer_path.parent.mkdir(parents=True, exist_ok=True)
        with best_pointer_path.open("w", encoding="utf-8") as handle:
            json.dump(best_pointer_payload, handle, indent=2, sort_keys=True)
        if verbose:
            print(
                f"[eval] champion_promoted checkpoint={challenger_checkpoint} "
                f"win_rate={challenger_win_rate:.3f}"
            )
    elif verbose:
        print(
            f"[eval] champion_retained checkpoint={champion_checkpoint_before} "
            f"reason={promotion_reason}"
        )

    progress_payload = _build_model_progress_payload(
        model_name=resolved_model_name,
        evaluation=evaluation,
        eval_threshold=float(eval_threshold),
        checkpoint_path=str(resolved_checkpoint_path),
        training_config=resolved_training_config,
    )
    _write_model_progress(resolved_model_progress_path, progress_payload, verbose=verbose)
    evaluation["modelname"] = resolved_model_name
    evaluation["eval_threshold"] = float(eval_threshold)
    evaluation["gate_passed"] = gate_passed
    evaluation["champion_promoted"] = bool(champion_promoted)
    evaluation["promotion_reason"] = str(promotion_reason)
    evaluation["champion_checkpoint_before"] = champion_checkpoint_before
    evaluation["champion_win_rate_before"] = champion_win_rate_before
    evaluation["champion_checkpoint_after"] = champion_checkpoint_after
    evaluation["champion_win_rate_after"] = champion_win_rate_after
    evaluation["champion_min_delta"] = resolved_min_delta
    evaluation["champion_min_games"] = resolved_min_games
    return evaluation


def _resolve_best_checkpoint_path(artifact_dir: str | Path = "training/artifacts") -> Path:
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
    return checkpoint_path.resolve()


def _load_runtime_model_bonus(
    *,
    log_path: str | Path = "training/battle_logs.jsonl",
    artifact_dir: str | Path = "training/artifacts",
    model_bonus_weight: float = 90.0,
    default_hidden_sizes: tuple[int, ...] = (256, 128),
    checkpoint_path_override: str | Path | None = None,
) -> tuple[ModelBonusFn | None, Path | None]:
    checkpoint_path: Path | None = None
    if checkpoint_path_override is not None:
        try:
            checkpoint_candidate = Path(checkpoint_path_override)
            if not checkpoint_candidate.is_absolute():
                checkpoint_candidate = Path.cwd() / checkpoint_candidate
            checkpoint_path = checkpoint_candidate.resolve()
            if not checkpoint_path.exists():
                raise FileNotFoundError(f"checkpoint not found: {checkpoint_path}")
        except Exception as exc:
            print(
                "[manual] explicit checkpoint load requested, but path was invalid; "
                f"using heuristic-only suggestions ({type(exc).__name__}: {exc})"
            )
            return None, None
    else:
        try:
            checkpoint_path = _resolve_best_checkpoint_path(artifact_dir)
        except Exception as exc:
            print(
                "[manual] no checkpoint found; using heuristic-only suggestions "
                f"({type(exc).__name__}: {exc})"
            )
            return None, None
    assert checkpoint_path is not None

    records = read_log_records(log_path)
    if not records:
        print("[manual] no battle logs found; using heuristic-only suggestions.")
        return None, checkpoint_path

    input_dim = len(records[0].state_features)
    model = PolicyValueMLP(
        input_dim=input_dim,
        hidden_sizes=tuple(default_hidden_sizes),
        action_dim=4,
    )
    try:
        load_checkpoint(checkpoint_path, model)
    except Exception as exc:
        print(
            f"[manual] checkpoint load failed for {checkpoint_path} "
            f"error={type(exc).__name__}: {exc}. Using heuristic-only suggestions."
        )
        return None, checkpoint_path

    model_bonus = build_model_bonus_fn(model, weight=model_bonus_weight)
    print(
        f"[manual] loaded checkpoint for suggestions: {checkpoint_path} "
        f"hidden_sizes={tuple(default_hidden_sizes)}"
    )
    return model_bonus, checkpoint_path


def _wait_for_http_ready(url: str, *, timeout_seconds: float = 15.0) -> bool:
    deadline = time.time() + max(1.0, float(timeout_seconds))
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                if int(getattr(response, "status", 200)) < 500:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _launch_electron_runtime(
    *,
    host: str,
    port: int,
) -> subprocess.Popen:
    project_root = Path(__file__).resolve().parents[3]
    frontend_root = project_root.parent / "showdownAIproject-frontend"
    if not frontend_root.exists():
        raise FileNotFoundError(f"Frontend directory not found: {frontend_root}")
    node_modules = frontend_root / "node_modules"
    if not node_modules.exists():
        raise RuntimeError(
            "Frontend dependencies are missing. Run `npm install` in "
            f"{frontend_root} before launching electron UI mode."
        )

    dist_dir = frontend_root / "dist"
    dist_js = dist_dir / "app.js"
    dist_css = dist_dir / "app.css"
    desktop_entry = frontend_root / "index.html"

    env = dict(os.environ)
    env.update(
        {
            "PSAI_BACKEND_ORIGIN": f"http://{host}:{int(port)}",
            "PSAI_ELECTRON_ENTRY_FILE": str(desktop_entry),
        }
    )

    build_process = subprocess.run(
        ["npm", "run", "build"],
        cwd=frontend_root,
        env=env,
        text=True,
        check=False,
    )
    if int(build_process.returncode) != 0:
        raise RuntimeError("Frontend build failed; cannot launch Electron UI mode.")

    missing_artifacts = [
        str(path) for path in (desktop_entry, dist_js, dist_css) if not path.exists()
    ]
    if missing_artifacts:
        raise RuntimeError(
            "Frontend build completed but required artifacts were missing: "
            + ", ".join(missing_artifacts)
        )

    electron_process = subprocess.Popen(
        ["npm", "run", "electron"],
        cwd=frontend_root,
        env=env,
        text=True,
    )

    return electron_process


def _stop_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except Exception:
        process.kill()
        try:
            process.wait(timeout=2.0)
        except Exception:
            pass


def _run_electron_ui_mode(
    *,
    player: Any,
    mechanics: MechanicsAPI,
    model_checkpoint_path: str | None,
    host: str,
    port: int,
) -> int:
    try:
        import uvicorn
    except Exception as exc:
        raise RuntimeError(
            "electron-ui mode requires uvicorn and fastapi. "
            "Install them before running GUI mode."
        ) from exc

    interaction_port = HttpBridgeInteractionPort()
    set_interaction_port(interaction_port)

    server = uvicorn.Server(
        uvicorn.Config(
            "psai.app.ui_server:app",
            host=host,
            port=int(port),
            reload=False,
            log_level="info",
        )
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_for_http_ready(f"http://{host}:{int(port)}/state", timeout_seconds=20.0):
        server.should_exit = True
        server_thread.join(timeout=5.0)
        set_interaction_port(None)
        raise RuntimeError("UI bridge server failed to start for electron-ui mode.")

    electron_process: subprocess.Popen | None = None
    try:
        electron_process = _launch_electron_runtime(
            host=host,
            port=port,
        )
        run_battle(
            player,
            mechanics=mechanics,
            model=None,
            state_callback=update_state,
            model_checkpoint_path=model_checkpoint_path,
            interaction_port=interaction_port,
        )
        return 0
    finally:
        _stop_process(electron_process)
        server.should_exit = True
        server_thread.join(timeout=5.0)
        set_interaction_port(None)


def run_UI_battle(
    player: Any,
    *,
    mechanics: MechanicsAPI,
    model_checkpoint_path: str | None = None,
    ui_host: str = "127.0.0.1",
    ui_port: int = 8000,
) -> int:
    return _run_electron_ui_mode(
        player=player,
        mechanics=mechanics,
        model_checkpoint_path=model_checkpoint_path,
        host=ui_host,
        port=int(ui_port),
    )

# ========================================
# Application Entrypoint
# ========================================


def main() -> int:
    player = pokeEnvPlayerInfo()
    mechanics = MechanicsAPI()

    # TRAINING CYCLE (active by default):
    # 1. If there are no logs, run heuristic training first to generate initial data.
    # 2. During the heuristic run, log all decisions and outcomes.
    # 3. Train the policy/value model on accumulated logs.
    # 4. Run model-play collection for the configured decision budget.
    # 5. Run evaluation and compare against target winrate.
    '''training_report = run_training_cycle(
        TrainingLoopConfig(
            log_path="training/battle_logs_V2.jsonl",
            artifact_dir="training/artifacts",
            heuristic_decisions=20000,
            model_cycle_decisions=0,
            eval_games=300,
            eval_min_win_rate=0.30,
            model_bonus_weight=90.0,
            model_hidden_sizes=(256, 128),
            max_cycles=3,
            collection_n_games=None,
            train_config=TrainConfig(
                epochs=15,
                batch_size=64,
                learning_rate=5e-4,
                weight_decay=1e-4,
                value_loss_weight=0.5,
                device="cpu",
                verbose=True,
            ),
        ),
        player,
        mechanics,
    )
    print(f"Training status: {training_report['status']}")
    '''
    '''
    run_battle(
        player,
        mechanics=mechanics,
        top_k=3,
        model=None,
        max_turns=None,
        wr_log_path="training/player_WR_log.jsonl",
        user_timeout_seconds=60.0,
        model_checkpoint_path="/absolute/path/to/model.pt",
        interaction_port=ConsoleInteractionPort(),
    )'''

    run_UI_battle(
        player,
        mechanics=mechanics,
        model_checkpoint_path="/absolute/path/to/model.pt",
        ui_host="127.0.0.1",
        ui_port=8000,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
