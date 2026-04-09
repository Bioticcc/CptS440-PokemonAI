"""Dataset utilities for logging runtime decisions and preparing training arrays."""

# Plain-English summary:
# This file stores and loads training examples (state, action, outcome),
# and converts them into numeric arrays for the model.

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from psai.domain.state import State

# max size of policy head (4 because 4 available moves, doesnt  account for switches at the moment.)
MAX_ACTION_SLOTS = 4


@dataclass(slots=True)
class BattleLogRecord:
    
    # This represents a single decision instance in a battle, with its outcome and related metadata.
    # Essentially for logging purposes, this shows:
    # At turn x in battle y, given state s, we chose action a, and got outcome v.

    battle_tag: str # which battle this is from
    turn: int # self explanatory
    state_features: list[float] # numeric version of the state.
    legal_action_ids: list[str] # legal actions during the above state
    chosen_action_id: str # what action we chose

    # These two are best described as "answer keys" for training:
    outcome_value: float # correct answer for the numerical value of how this decision turn out for us.
    policy_target: list[float] | None = None # correct answer for what move to choose
    metadata: dict[str, Any] = field(default_factory=dict) # optional extra info about the decision


def encode_state(state: State) -> list[float]:
   
   # This takes in the state with various different types like int, string, etc...
   # and turns all of it into floats. Then returns, for use in numeric related gobbledegook.

    turn_value = getattr(state, "turn_number", None)
    if turn_value is None:
        turn_value = getattr(state, "turn", 0)
    turn = int(turn_value or 0)

    revealed_team = getattr(state, "opponent_team", None)
    if revealed_team is None:
        revealed_team = getattr(state, "revealed_opponent_team", ())
    revealed_count = (
        len(revealed_team)
        if revealed_team is not None
        else int(getattr(state, "opponent_revealed_count", 0) or 0)
    )

    forced_switch = bool(getattr(state, "forced_switch", False))

    return [
        float(turn),
        float(state.friendly_active.hp_fraction),
        float(state.opponent_active.hp_fraction),
        1.0 if state.friendly_active.status else 0.0,
        1.0 if state.opponent_active.status else 0.0,
        float(sum(state.friendly_active.boosts.values())),
        float(sum(state.opponent_active.boosts.values())),
        float(len(state.legal_actions)), # len because we dont really have a fixed value for this, its just len
        float(revealed_count), # same as above
        1.0 if forced_switch else 0.0,
    ]


def default_policy_target(
    legal_action_ids: Sequence[str],
    chosen_action_id: str,
    *,
    action_dim: int = MAX_ACTION_SLOTS,
) -> list[float]:

    # Here we get a list of possible actions, the one we chose, and max number of actions possible.
    # We make a vector of 0's the same length as possible actions, to say by defauly "No action was chosen".
    # Then, we find the index of the chosen action, and if its less then the max number of actions, 
    # We set the coresponding index to 1, to say "This action was chosen". Then we return the vector.
    
    # All this function is saying is what action was chosen, albeit in a longwinded way. 

    target = [0.0 for _ in range(action_dim)]
    if not legal_action_ids: # If there are no legal actions just return a 0 vector
        return target

    idx = list(legal_action_ids).index(chosen_action_id) # index of that legal action

    if idx < action_dim: 
        target[idx] = 1.0
    return target


def make_log_record(
    state: State,
    chosen_action_id: str,
    outcome_value: float,
    *,
    policy_target: Sequence[float] | None = None,
    metadata: dict[str, Any] | None = None,
) -> BattleLogRecord:

    # Takes in a bunch of info and makes a nice and pretty BattleLogRecord instance.

    legal_ids = [action.action_id for action in state.legal_actions]
    battle_tag = str(getattr(state, "battle_tag", "") or "")
    if not battle_tag and getattr(state, "raw_battle", None) is not None:
        battle_tag = str(getattr(state.raw_battle, "battle_tag", "") or "")

    turn_value = getattr(state, "turn_number", None)
    if turn_value is None:
        turn_value = getattr(state, "turn", 0)
    turn = int(turn_value or 0)

    # if we are given the policy target use it. Otherwhise, use the default policy target function
    # from right above to generate it. Essentially just (of x actions, which was chosen) in a fancy vector
    resolved_policy = (
        [float(x) for x in policy_target]
        if policy_target is not None
        else default_policy_target(legal_ids, chosen_action_id)
    )

    return BattleLogRecord(
        battle_tag=battle_tag,
        turn=turn,
        state_features=encode_state(state),
        legal_action_ids=legal_ids,
        chosen_action_id=chosen_action_id,
        outcome_value=float(outcome_value),
        policy_target=resolved_policy,
        metadata=dict(metadata or {}),
    )


def write_log_record(path: str | Path, record: BattleLogRecord) -> None:
    
    # Writes a BattleLogRecord to json file.

    output_path = Path(path) # duh
    output_path.parent.mkdir(parents=True, exist_ok=True) # makes sure parent directory exists, makes it if not

    to_write = asdict(record) # turns into a dictionary! nice n easy for json writing
    to_write["state_features"] = [float(x) for x in record.state_features] # normalizes to float (again, but its for safety reasons. Lets us test later withou encode state)
    to_write["outcome_value"] = float(record.outcome_value) # normalizes to float

    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(to_write))
        f.write("\n")


def read_log_records(path: str | Path) -> list[BattleLogRecord]:

    # Reads the json file, turning into a list of BattleLogRecords!

    input_path = Path(path)
    if not input_path.exists():
        return []

    records: list[BattleLogRecord] = []
    with input_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            records.append(
                BattleLogRecord(
                    battle_tag=str(payload["battle_tag"]),
                    turn=int(payload["turn"]),
                    state_features=[float(x) for x in payload["state_features"]],
                    legal_action_ids=[str(x) for x in payload["legal_action_ids"]],
                    chosen_action_id=str(payload["chosen_action_id"]),
                    outcome_value=float(payload["outcome_value"]),
                    policy_target=(
                        [float(x) for x in payload["policy_target"]]
                        if payload.get("policy_target") is not None
                        else None
                    ),
                    metadata=dict(payload.get("metadata", {})),
                )
            )
    return records


def records_to_numpy(
    records: Sequence[BattleLogRecord],
    *,
    action_dim: int = MAX_ACTION_SLOTS,
) -> dict[str, np.ndarray]:

    # Once we have the records, we turn them into numpy arrays that will be actually fed into the model

    # So WHY do we turn them into numpy arrays?
    # so the array is just a table, essentially. a spreadsheet where each row is one battle decision.
    # this function will give us 4 different tables, each with their own info.
    # state: numeric data from the state object. rows are sample nums, cols are features like current_hp, etc.
    # policy: which move as chosen essentially which move was the correct one to pick for this decision.
    # value: how GOOD that decision was, in a single value. Thing fancier heuristic scoring
    # mask: this is just the legal actions for that turn, so we can ignore illegal/impossible moves.
    # The neural network can only really work in fixed table shaped objects, like numpy arrays!
    # We cant just give it our custom BattleLogRecord or State objects, so we need to turn them into something readable

    # If the given records list is empty, just return empty arrays in the correct shape.
    if not records:
        empty = np.zeros((0, action_dim), dtype=np.float32)
        return {
            "state": np.zeros((0, 0), dtype=np.float32),
            "policy": empty,
            "value": np.zeros((0, 1), dtype=np.float32),
            "mask": empty,
        }

    # Defaults for the arrays, we make them thte correct shape ahead of time but empty.
    feature_dim = len(records[0].state_features)
    state_arr = np.zeros((len(records), feature_dim), dtype=np.float32)
    policy_arr = np.zeros((len(records), action_dim), dtype=np.float32)
    value_arr = np.zeros((len(records), 1), dtype=np.float32)
    mask_arr = np.zeros((len(records), action_dim), dtype=np.float32)

    # So for each recorrd in our battle record list, we fill out the state array with 
    # information from the record at the given index i.
    for i, record in enumerate(records):
        if len(record.state_features) != feature_dim:
            raise ValueError("All records must have the same feature dimension.")

        state_arr[i, :] = np.asarray(record.state_features, dtype=np.float32)

        policy_source = (
            record.policy_target
            if record.policy_target is not None
            else default_policy_target(record.legal_action_ids, record.chosen_action_id, action_dim=action_dim)
        )
        policy_source = list(policy_source)
        if len(policy_source) < action_dim:
            policy_source = policy_source + [0.0] * (action_dim - len(policy_source))
        policy_arr[i] = np.asarray(policy_source[:action_dim], dtype=np.float32)

        legal_slots = min(len(record.legal_action_ids), action_dim)
        if legal_slots > 0:
            mask_arr[i, :legal_slots] = 1.0

        value_arr[i, 0] = float(record.outcome_value)

    return {
        "state": state_arr,
        "policy": policy_arr,
        "value": value_arr,
        "mask": mask_arr,
    }
