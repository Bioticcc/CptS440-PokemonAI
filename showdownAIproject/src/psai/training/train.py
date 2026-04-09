"""Training loop utilities for the policy+value model."""

# Plain-English summary:
# This file handles optimization, metrics, and checkpoint save/load for
# training from baseline battle logs.

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import time
from typing import Any, Sequence

try:
    import torch
    import torch.nn.functional as F
    from torch.utils.data import DataLoader, TensorDataset
except ImportError as import_error:  # This is just here so that when training the log gets stored with clear error message
    torch = None
    F = None
    DataLoader = Any
    TensorDataset = Any
    _TORCH_IMPORT_ERROR = import_error
else:
    _TORCH_IMPORT_ERROR = None

from psai.training.dataset import BattleLogRecord, records_to_numpy
from psai.training.model import PolicyValueMLP
from psai.decision.chooser import ModelBonusFn
from psai.domain.state import LegalAction, State, parse_battle_to_state
from psai.mechanics.api import MechanicsAPI
from psai.training.dataset import encode_state, read_log_records


@dataclass(slots=True)
class TrainConfig:

    # Training params, change as needed.
 
    epochs: int = 5 # how many times we loop throug hteh full dataset. more epochs better trainingm but takes longer
    batch_size: int = 32 # how many examples we train on at once
    learning_rate: float = 1e-3 # step size for weight updates (if loss barely changes, increase.)
    weight_decay: float = 0.0 # penalizes large weights.
    value_loss_weight: float = 1.0 # how much important we give to value loss vs policy loss. 
    device: str = "cpu" # uses cpu or gpu
    checkpoint_path: str | None = None # where to save models during training, if needed.
    verbose: bool = True


def _require_torch() -> None: # guard
    if torch is None:
        raise ImportError("torch is required to run training") from _TORCH_IMPORT_ERROR


def save_checkpoint(
    path: str | Path,
    model: PolicyValueMLP,
    optimizer: Any,
    *,
    epoch: int,
    metrics: dict[str, list[float]],
) -> None:

    # Save a model we like

    _require_torch()

    checkpoint_path = Path(path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "epoch": int(epoch),
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "metrics": metrics,
        },
        checkpoint_path,
    )


def load_checkpoint(
    path: str | Path,
    model: PolicyValueMLP,
    optimizer: Any | None = None,
    *,
    map_location: str = "cpu",
) -> dict[str, Any]:

    # Load a model we like to train it further or for eval.

    _require_torch()

    payload = torch.load(Path(path), map_location=map_location)
    model.load_state_dict(payload["model_state_dict"])
    if optimizer is not None and "optimizer_state_dict" in payload:
        optimizer.load_state_dict(payload["optimizer_state_dict"])
    return payload


def train_policy_value(
    model: PolicyValueMLP,
    records: Sequence[BattleLogRecord],
    *,
    config: TrainConfig | None = None,
    optimizer: Any | None = None,
) -> dict[str, Any]:

    # The main training loop! Here we take in the model and train a policy value model using the battle log records

    _require_torch()

    cfg = config or TrainConfig() # set config
    arrays = records_to_numpy(records, action_dim=model.action_dim) # converts records to arrays
    if arrays["state"].shape[0] == 0: # if no records cant train
        raise ValueError("No records provided for training.")

    device = torch.device(cfg.device) # 
    state_tensor = torch.as_tensor(arrays["state"], dtype=torch.float32, device=device)
    policy_tensor = torch.as_tensor(arrays["policy"], dtype=torch.float32, device=device)
    value_tensor = torch.as_tensor(arrays["value"], dtype=torch.float32, device=device).squeeze(-1)
    mask_tensor = torch.as_tensor(arrays["mask"], dtype=torch.float32, device=device)

    dataset = TensorDataset(state_tensor, policy_tensor, value_tensor, mask_tensor)
    loader = DataLoader(dataset, batch_size=max(1, cfg.batch_size), shuffle=True)

    model.to(device)
    model.train()

    if optimizer is None:
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=cfg.learning_rate,
            weight_decay=cfg.weight_decay,
        )

    metrics: dict[str, list[float]] = {
        "policy_loss": [],
        "value_loss": [],
        "total_loss": [],
    }

    for epoch in range(cfg.epochs):
        epoch_policy_loss = 0.0
        epoch_value_loss = 0.0
        epoch_total_loss = 0.0
        batch_count = 0

        for state_batch, policy_batch, value_batch, mask_batch in loader:
            optimizer.zero_grad()

            logits, value_pred = model(state_batch, action_mask=mask_batch)
            log_probs = torch.log_softmax(logits, dim=-1)
            policy_loss = -(policy_batch * log_probs).sum(dim=-1).mean()
            value_loss = F.mse_loss(value_pred, value_batch)
            total_loss = policy_loss + (cfg.value_loss_weight * value_loss)

            total_loss.backward()
            optimizer.step()

            epoch_policy_loss += float(policy_loss.item())
            epoch_value_loss += float(value_loss.item())
            epoch_total_loss += float(total_loss.item())
            batch_count += 1

        denom = max(1, batch_count)
        metrics["policy_loss"].append(epoch_policy_loss / denom)
        metrics["value_loss"].append(epoch_value_loss / denom)
        metrics["total_loss"].append(epoch_total_loss / denom)
        if cfg.verbose:
            print(
                f"[train] epoch {epoch + 1}/{cfg.epochs} "
                f"policy_loss={metrics['policy_loss'][-1]:.4f} "
                f"value_loss={metrics['value_loss'][-1]:.4f} "
                f"total_loss={metrics['total_loss'][-1]:.4f}"
            )

    if cfg.checkpoint_path:
        save_checkpoint(
            cfg.checkpoint_path,
            model,
            optimizer,
            epoch=cfg.epochs,
            metrics=metrics,
        )

    return {
        "epochs": cfg.epochs,
        "num_records": len(records),
        "metrics": metrics,
    }


@dataclass(slots=True)
class TrainingLoopConfig:
    log_path: str = "training/battle_logs.jsonl"
    artifact_dir: str = "training/artifacts"
    bootstrap_decisions: int = 20_000
    heuristic_refresh_decisions: int = 0
    model_cycle_decisions: int = 10_000
    eval_games: int = 100
    eval_min_win_rate: float = 0.50
    model_bonus_weight: float = 120.0
    max_cycles: int = 1
    model_hidden_sizes: tuple[int, ...] = (128, 64)
    train_config: TrainConfig = field(default_factory=TrainConfig)
    collection_n_games: int | None = None
    verbose: bool = True
    print_turn_suggestions: bool = True
    print_top_k: int = 3
    print_every_decisions: int = 100
    eval_print_every_games: int = 10


def _normalize_features_for_model(features: list[float], input_dim: int) -> list[float]:
    if len(features) == input_dim:
        return features
    if len(features) < input_dim:
        return features + [0.0] * (input_dim - len(features))
    return features[:input_dim]


def build_model_bonus_fn(model: PolicyValueMLP, weight: float = 120.0) -> ModelBonusFn:
    _require_torch()
    model.eval()

    def _bonus(state: State, action: LegalAction) -> float:
        if action.is_switch:
            return 0.0

        non_switch_actions = [candidate for candidate in state.legal_actions if not candidate.is_switch]
        slot_candidates = non_switch_actions[: model.action_dim]
        slot_index = None
        for index, candidate in enumerate(slot_candidates):
            if candidate.action_id == action.action_id:
                slot_index = index
                break
        if slot_index is None:
            return 0.0

        features = encode_state(state)
        normalized = _normalize_features_for_model(features, model.input_dim)
        model_device = next(model.parameters()).device
        state_tensor = torch.as_tensor([normalized], dtype=torch.float32, device=model_device)
        action_mask = torch.zeros((1, model.action_dim), dtype=torch.float32, device=model_device)
        action_mask[0, : len(slot_candidates)] = 1.0

        probabilities, _value = model.predict(state_tensor, action_mask=action_mask)
        probability = float(probabilities[0, slot_index].item())
        uniform = 1.0 / float(model.action_dim)
        return (probability - uniform) * float(weight)

    return _bonus


def _load_best_pointer(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return dict(json.load(handle))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def run_training_cycle(
    config: TrainingLoopConfig,
    player: Any,
    mechanics: MechanicsAPI,
) -> dict[str, Any]:
    from psai.app import main as app_main

    log_path = Path(config.log_path)
    artifact_dir = Path(config.artifact_dir)
    checkpoints_dir = artifact_dir / "checkpoints"
    metrics_dir = artifact_dir / "metrics"
    best_pointer_path = artifact_dir / "best_model.json"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    records = read_log_records(log_path)
    bootstrap_result: dict[str, Any] | None = None
    if config.verbose:
        print(
            f"[loop] start log_path={log_path} artifact_dir={artifact_dir} "
            f"existing_records={len(records)} max_cycles={config.max_cycles}"
        )
    if not records and config.bootstrap_decisions > 0:
        if config.verbose:
            print(f"[loop] bootstrap heuristic collection target={config.bootstrap_decisions}")
        bootstrap_result = app_main.run_heuristic_training_battle(
            player,
            mechanics=mechanics,
            decision_budget=config.bootstrap_decisions,
            log_path=log_path,
            cycle_id=0,
            n_games=config.collection_n_games,
            verbose=config.verbose,
            print_every_decisions=config.print_every_decisions,
            print_top_k=config.print_top_k,
            print_turn_suggestions=config.print_turn_suggestions,
        )
        records = read_log_records(log_path)
        if config.verbose:
            print(f"[loop] bootstrap complete records={len(records)}")

    if not records:
        raise ValueError("No training records available after bootstrap stage.")

    best_pointer = _load_best_pointer(best_pointer_path) or {}
    best_checkpoint = best_pointer.get("checkpoint_path")
    best_win_rate = float(best_pointer.get("win_rate", -1.0))

    input_dim = len(records[0].state_features)
    model = PolicyValueMLP(
        input_dim=input_dim,
        hidden_sizes=config.model_hidden_sizes,
        action_dim=4,
    )
    if best_checkpoint:
        best_checkpoint_path = Path(str(best_checkpoint))
        if best_checkpoint_path.exists():
            load_checkpoint(best_checkpoint_path, model)

    cycle_reports: list[dict[str, Any]] = []
    status = "completed"

    for cycle_id in range(1, int(config.max_cycles) + 1):
        if config.verbose:
            print(f"[loop] cycle {cycle_id}/{config.max_cycles} begin")
        if config.heuristic_refresh_decisions > 0:
            if config.verbose:
                print(f"[loop] heuristic refresh target={config.heuristic_refresh_decisions}")
            app_main.run_heuristic_training_battle(
                player,
                mechanics=mechanics,
                decision_budget=config.heuristic_refresh_decisions,
                log_path=log_path,
                cycle_id=cycle_id,
                n_games=config.collection_n_games,
                verbose=config.verbose,
                print_every_decisions=config.print_every_decisions,
                print_top_k=config.print_top_k,
                print_turn_suggestions=config.print_turn_suggestions,
            )

        records = read_log_records(log_path)
        if not records:
            raise ValueError("Training data unexpectedly missing before model training.")
        if config.verbose:
            print(f"[loop] training model on records={len(records)}")

        checkpoint_path = checkpoints_dir / f"policy_value_cycle_{cycle_id:04d}.pt"
        cycle_train_config = replace(config.train_config, checkpoint_path=str(checkpoint_path))
        train_result = train_policy_value(model, records, config=cycle_train_config)
        if config.verbose:
            metrics = train_result["metrics"]
            print(
                f"[loop] cycle={cycle_id} train_complete "
                f"last_total_loss={metrics['total_loss'][-1]:.4f} checkpoint={checkpoint_path}"
            )

        model_bonus = build_model_bonus_fn(model, weight=config.model_bonus_weight)
        if config.verbose:
            print(f"[loop] model self-play collection target={config.model_cycle_decisions}")
        model_collection = app_main.run_model_training_battle(
            player,
            mechanics=mechanics,
            decision_budget=config.model_cycle_decisions,
            log_path=log_path,
            cycle_id=cycle_id,
            model=model_bonus,
            model_checkpoint=str(checkpoint_path),
            n_games=config.collection_n_games,
            verbose=config.verbose,
            print_every_decisions=config.print_every_decisions,
            print_top_k=config.print_top_k,
            print_turn_suggestions=config.print_turn_suggestions,
        )

        if config.eval_games <= 0:
            evaluation = {"games": 0, "wins": 0, "losses": 0, "ties": 0, "win_rate": 0.0}
        else:
            if config.verbose:
                print(f"[loop] evaluation start games={config.eval_games}")
            reset_fn = getattr(player, "reset_battles", None)
            if callable(reset_fn):
                try:
                    reset_fn()
                except Exception:
                    pass

            runner = None
            games_launched = 0
            games_finished = 0
            wins = 0
            losses = 0
            ties = 0
            counted_tags: set[str] = set()
            last_prompted_request: dict[str, tuple[Any, ...]] = {}

            while True:
                if runner is not None and runner.done:
                    runner.raise_if_failed()
                    runner = None

                battles = dict(getattr(player, "battles", {}) or {})
                for battle_tag, battle in battles.items():
                    if not getattr(battle, "finished", False) or battle_tag in counted_tags:
                        continue
                    won = getattr(battle, "won", None)
                    if won is True:
                        wins += 1
                    elif won is False:
                        losses += 1
                    else:
                        ties += 1
                    counted_tags.add(battle_tag)
                    games_finished += 1
                    last_prompted_request.pop(battle_tag, None)
                    if (
                        config.verbose
                        and config.eval_print_every_games > 0
                        and games_finished % int(config.eval_print_every_games) == 0
                    ):
                        print(
                            f"[eval] games={games_finished}/{config.eval_games} "
                            f"wins={wins} losses={losses} ties={ties}"
                        )

                active_battles = [battle for battle in battles.values() if not getattr(battle, "finished", False)]
                if runner is None and not active_battles and games_launched < config.eval_games:
                    runner = app_main.AsyncConnectionRunner(player, 1).start()
                    games_launched += 1

                for battle in active_battles:
                    battle_tag = str(getattr(battle, "battle_tag", id(battle)))
                    request_signature = app_main._battle_request_signature(battle)
                    if last_prompted_request.get(battle_tag) == request_signature:
                        continue

                    try:
                        state = parse_battle_to_state(battle)
                    except Exception as exc:
                        if config.verbose:
                            print(
                                f"[eval] parse_state_failed battle={battle_tag} "
                                f"error={type(exc).__name__}: {exc}. Sending default order."
                            )
                        if hasattr(player, "set_pending_order"):
                            player.set_pending_order(battle_tag, app_main._default_order(player))
                        last_prompted_request[battle_tag] = request_signature
                        continue
                    if not app_main._has_actionable_request(state, battle):
                        continue

                    turn_suggestions = (
                        app_main.get_turn_suggestions(
                            state,
                            mechanics,
                            top_k=1,
                            model=model_bonus,
                        )
                        if state.legal_actions
                        else []
                    )
                    chosen_order, _chosen_action_id = app_main._choose_order_for_request(
                        player,
                        battle,
                        state,
                        turn_suggestions,
                    )

                    if hasattr(player, "set_pending_order"):
                        player.set_pending_order(battle_tag, chosen_order)
                    last_prompted_request[battle_tag] = request_signature

                if games_finished >= config.eval_games and not active_battles and runner is None:
                    break

                time.sleep(0.1)

            evaluation = {
                "games": int(games_finished),
                "wins": int(wins),
                "losses": int(losses),
                "ties": int(ties),
                "win_rate": float(wins / games_finished) if games_finished > 0 else 0.0,
            }
            if config.verbose:
                print(
                    f"[eval] complete games={evaluation['games']} wins={evaluation['wins']} "
                    f"losses={evaluation['losses']} ties={evaluation['ties']} "
                    f"win_rate={evaluation['win_rate']:.3f}"
                )

        gate_passed = evaluation["win_rate"] >= float(config.eval_min_win_rate)
        cycle_report = {
            "cycle_id": cycle_id,
            "records_used": len(records),
            "checkpoint_path": str(checkpoint_path),
            "train_result": train_result,
            "model_collection": model_collection,
            "evaluation": evaluation,
            "gate_passed": gate_passed,
        }
        cycle_reports.append(cycle_report)
        _write_json(metrics_dir / f"cycle_{cycle_id:04d}.json", cycle_report)
        if config.verbose:
            print(
                f"[loop] cycle={cycle_id} gate={'pass' if gate_passed else 'fail'} "
                f"threshold={config.eval_min_win_rate:.3f} win_rate={evaluation['win_rate']:.3f}"
            )

        if gate_passed and evaluation["win_rate"] >= best_win_rate:
            best_win_rate = float(evaluation["win_rate"])
            _write_json(
                best_pointer_path,
                {
                    "cycle_id": cycle_id,
                    "checkpoint_path": str(checkpoint_path),
                    "win_rate": best_win_rate,
                },
            )

        if not gate_passed:
            status = "below_threshold"
            break

    return {
        "status": status,
        "bootstrap": bootstrap_result,
        "cycles": cycle_reports,
        "log_path": str(log_path),
        "artifact_dir": str(artifact_dir),
    }
