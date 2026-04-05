"""Normalized internal battle-state representation.

This module defines the State contract used across decision, mechanics, app,
and training code. The representation stays independent from raw `poke-env`
objects so parser/runtime details do not leak into the core pipeline.
"""

# Plain-English summary:
# This file defines our own clean snapshot of battle state.
# The rest of the project should rely on this normalized shape, not on
# raw poke-env battle internals.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PokemonSnapshot:

    # Snapshot of one Pokemon, used for both active and revealed team members.
    
    species: str 
    hp_fraction: float
    status: str | None = None
    boosts: dict[str, int] = field(default_factory=dict)
    types: tuple[str, ...] = field(default_factory=tuple)
    # Tracks the revealed moves since they're not all known at the state of the battle
    known_moves: tuple[str, ...] = field(default_factory=tuple)
    # Marks whether this Pokemon was seen directly or only inferred, which can be useful for the heuristics maybe? IDK.
    knowledge_state: str = "seen"
    fainted: bool = False
    identifier: str | None = None  # needed if in mechanics engine to get poke-env built in values
    raw_pokemon: Any = None  # This is the poke-env pokemon object, used for built ins.

    # Additional fields can be added later if the parser needs more detail.

    def __post_init__(self) -> None:

        # HP is clamped to a range that is valid and if HP is 0, mark as fainted.
        self.hp_fraction = max(0.0, min(1.0, float(self.hp_fraction)))

        if self.hp_fraction <= 0.0:
            self.fainted = True


@dataclass(slots=True)
class LegalAction:

    # One legal action the agent can take on the current turn.

    action_id: str
    move_name: str
    move_type: str | None = None
    base_power: int | None = None
    damage_class: str | None = None
    target: str | None = None
    accuracy: float = 1.0 # should be somewhere in the state for that turn. Surely the battle object has this somewhere
    priority: int = 0 # move priority (Quick Attack-style ordering before speed ties)
    current_pp: int | None = None
    max_pp: int | None = None
    is_switch: bool = False
    raw_move: Any = None  # poke-env move object, used for built ins like raw_pokemon above.

    def __post_init__(self) -> None:

        # Make sure accuracy and priority are always valid types/ranges
        self.accuracy = max(0.0, min(1.0, float(self.accuracy)))
        self.priority = int(self.priority)
        if self.current_pp is not None:
            self.current_pp = max(0, int(self.current_pp))
        if self.max_pp is not None:
            self.max_pp = max(0, int(self.max_pp))

@dataclass(slots=True)
class State:

    # The big one! This is state. Use the previous dataclasses to fill out everything. 

    friendly_active: PokemonSnapshot # our pokemon, currently active! use a snapshot.
    opponent_active: PokemonSnapshot # same, but opponent. Fill out info as we go. If we see it use a move, we should fill that info out for that pokemon. 
    turn_number: int | None = None
    # Full known team info for both sides
    friendly_team: tuple[PokemonSnapshot, ...] = field(default_factory=tuple)
    opponent_team: tuple[PokemonSnapshot, ...] = field(default_factory=tuple)
    friendly_revealed_count: int = 0
    opponent_revealed_count: int = 0
    legal_actions: tuple[LegalAction, ...] = field(default_factory=tuple)
    raw_battle: Any = None  # Optional pointer to the raw poke-env battle object.

def parse_battle_to_state(battle: Any) -> State:

    # Convert the live battle object into the normalized State dataclass.

    turn_number = _extract_turn_number(battle)
    friendly_active = parse_active_pokemon(battle, side="friendly") 
    opponent_active = parse_active_pokemon(battle, side="opponent")
    friendly_team = _parse_team_snapshots(battle, side="friendly")
    opponent_team = _parse_team_snapshots(battle, side="opponent")
    legal_actions = parse_legal_actions(battle)

    return State(
        turn_number=turn_number,
        friendly_active=friendly_active,
        opponent_active=opponent_active,
        friendly_team=friendly_team,
        opponent_team=opponent_team,
        friendly_revealed_count=len(friendly_team),
        opponent_revealed_count=len(opponent_team),
        legal_actions=legal_actions,
        raw_battle=battle,
    )


def parse_active_pokemon(battle: Any, *, side: str) -> PokemonSnapshot:

    # Build a PokemonSnapshot for the active Pokemon on the requested side.

    if side not in {"friendly", "opponent"}:
        raise ValueError("side must be 'friendly' or 'opponent'")

    if side == "friendly":
        raw_pokemon = getattr(battle, "active_pokemon", None)
    else:
        raw_pokemon = getattr(battle, "opponent_active_pokemon", None)

    if raw_pokemon is None:
        # Return a placeholder instead of failing if the active Pokemon isn't visible yet, which can happen at the start of the battle or if poke-env doesn't expose it for some reason.
        return PokemonSnapshot(
            species="unknown",
            hp_fraction=0.0,
            status=None,
            boosts={},
            types=(),
            known_moves=(),
            knowledge_state="inferred",
            fainted=True,
            identifier=None,
            raw_pokemon=None,
        )

    return _build_pokemon_snapshot(raw_pokemon)


def parse_legal_actions(battle: Any) -> tuple[LegalAction, ...]:

    # Convert poke-env move and switch options into our LegalAction tuple.
    legal_actions: list[LegalAction] = []

    available_moves = list(getattr(battle, "available_moves", []) or [])
    for move in available_moves:
        move_type = getattr(move, "type", None)
        move_type_name = getattr(move_type, "name", None)
        base_power, damage_class, target = _extract_move_details(move)
        accuracy = _normalize_accuracy(getattr(move, "accuracy", 1.0))
        action_id = str(getattr(move, "id", None) or getattr(move, "move", None) or "move")
        current_pp, max_pp = _extract_move_pp(move)

        legal_actions.append(
            LegalAction(
                action_id=action_id,
                move_name=action_id,
                move_type=str(move_type_name) if move_type_name else None,
                base_power=base_power,
                damage_class=damage_class,
                target=target,
                accuracy=accuracy,
                priority=int(getattr(move, "priority", 0) or 0),
                current_pp=current_pp,
                max_pp=max_pp,
                is_switch=False,
                raw_move=move,
            )
        )

    # Encode switches as actions so decision code can rank them alongside moves.
    available_switches = list(getattr(battle, "available_switches", []) or [])
    for index, target in enumerate(available_switches, start=1):
        identifier = str(getattr(target, "identifier", None) or getattr(target, "species", None) or index)
        species = str(getattr(target, "species", None) or getattr(target, "name", None) or identifier)
        legal_actions.append(
            LegalAction(
                action_id=f"switch:{identifier}",
                move_name=f"switch_{species}",
                move_type=None,
                accuracy=1.0,
                priority=6,
                is_switch=True,
                raw_move=target,
            )
        )

    return tuple(legal_actions)


def _normalize_accuracy(accuracy: Any) -> float:

    try:
        value = float(accuracy)
    except (TypeError, ValueError):
        return 1.0

    # Unfortunately, poke-env can represent accuracy as either 1.0 or 100, so it's normalized here to always be between 0.0 and 1.0.
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _extract_turn_number(battle: Any) -> int | None:

    turn_value = getattr(battle, "turn", None)
    if turn_value is None:
        return None

    try:
        return int(turn_value)
    except (TypeError, ValueError):
        return None


def _extract_move_pp(move: Any) -> tuple[int | None, int | None]:

    # Move objects can expose PP with slightly different attribute names, so this checks for both.
    current_pp = getattr(move, "current_pp", None)
    if current_pp is None:
        current_pp = getattr(move, "pp", None)

    max_pp = getattr(move, "max_pp", None)
    if max_pp is None:
        max_pp = getattr(move, "maxpp", None)

    try:
        normalized_current = None if current_pp is None else max(0, int(current_pp))
    except (TypeError, ValueError):
        normalized_current = None

    try:
        normalized_max = None if max_pp is None else max(0, int(max_pp))
    except (TypeError, ValueError):
        normalized_max = None

    return normalized_current, normalized_max


def _extract_move_details(move: Any) -> tuple[int | None, str | None, str | None]:

    base_power = getattr(move, "base_power", None)
    if base_power is None:
        base_power = getattr(move, "power", None)

    damage_class = getattr(move, "damage_class", None)
    if damage_class is not None:
        damage_class = getattr(damage_class, "name", damage_class)

    target = getattr(move, "target", None)
    if target is not None:
        target = getattr(target, "name", target)

    try:
        normalized_power = None if base_power is None else max(0, int(base_power))
    except (TypeError, ValueError):
        normalized_power = None

    normalized_damage_class = None if damage_class is None else str(damage_class)
    normalized_target = None if target is None else str(target)

    return normalized_power, normalized_damage_class, normalized_target


def _build_pokemon_snapshot(raw_pokemon: Any) -> PokemonSnapshot:

    # Convert a poke-env Pokemon object into our internal snapshot.

    species = str(
        getattr(raw_pokemon, "species", None)
        or getattr(raw_pokemon, "base_species", None)
        or getattr(raw_pokemon, "name", None)
        or "unknown"
    )

    hp_fraction = _extract_hp_fraction(raw_pokemon)

    status = getattr(raw_pokemon, "status", None)
    status_name = getattr(status, "name", None)
    normalized_status = str(status_name or status) if status else None

    boosts_raw = getattr(raw_pokemon, "boosts", None) or {}
    boosts = {str(k): int(v) for k, v in dict(boosts_raw).items()}

    types = _extract_types(raw_pokemon)
    known_moves = _extract_known_move_names(raw_pokemon)
    fainted = bool(getattr(raw_pokemon, "fainted", False)) or hp_fraction <= 0.0

    identifier = getattr(raw_pokemon, "identifier", None)
    if identifier is not None:
        identifier = str(identifier)

    return PokemonSnapshot(
        species=species,
        hp_fraction=hp_fraction,
        status=normalized_status,
        boosts=boosts,
        types=types,
        known_moves=known_moves,
        knowledge_state="seen",
        fainted=fainted,
        identifier=identifier,
        raw_pokemon=raw_pokemon,
    )


def _extract_hp_fraction(raw_pokemon: Any) -> float:

    # Read HP as a fraction, falling back to current/max HP when needed.

    hp_fraction = getattr(raw_pokemon, "current_hp_fraction", None)
    if hp_fraction is not None:
        return max(0.0, min(1.0, float(hp_fraction)))

    current_hp = getattr(raw_pokemon, "current_hp", None)
    max_hp = getattr(raw_pokemon, "max_hp", None)
    if current_hp is not None and max_hp:
        try:
            return max(0.0, min(1.0, float(current_hp) / float(max_hp)))
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    return 0.0


def _extract_types(raw_pokemon: Any) -> tuple[str, ...]:

    # Convert type enums or objects into plain string names.

    raw_types = getattr(raw_pokemon, "types", None) or ()
    type_names: list[str] = []
    for item in raw_types:
        if item is None:
            continue
        type_name = getattr(item, "name", None)
        type_names.append(str(type_name or item))
    return tuple(type_names)


def _extract_known_move_names(raw_pokemon: Any) -> tuple[str, ...]:

    # Store normalized move identifiers for revealed/known move tracking.

    moves_dict = getattr(raw_pokemon, "moves", None) or {}
    names: list[str] = []
    for move_id, move_obj in dict(moves_dict).items():
        move_name = getattr(move_obj, "id", None) or getattr(move_obj, "move", None) or move_id
        names.append(str(move_name))
    return tuple(names)


def _parse_team_snapshots(battle: Any, *, side: str) -> tuple[PokemonSnapshot, ...]:

    # Build snapshots for each known team member on the requested side.

    if side == "friendly":
        team_raw = getattr(battle, "team", None) or {}
    elif side == "opponent":
        team_raw = getattr(battle, "opponent_team", None) or {}
    else:
        raise ValueError("side must be 'friendly' or 'opponent'")

    snapshots: list[PokemonSnapshot] = []
    for pokemon in dict(team_raw).values():
        if pokemon is not None:
            snapshots.append(_build_pokemon_snapshot(pokemon))

    return tuple(snapshots)
