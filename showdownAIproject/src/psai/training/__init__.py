"""Learning-system modules: dataset utilities, model definitions, and training."""

# Plain-English summary:
# This package contains everything needed to turn logs into a trained model.

from psai.training.dataset import (
    BattleLogRecord,
    encode_state,
    make_log_record,
    read_log_records,
    records_to_numpy,
    write_log_record,
)
from psai.training.model import PolicyValueMLP
from psai.training.train import (
    TrainConfig,
    TrainingLoopConfig,
    build_model_bonus_fn,
    load_checkpoint,
    run_training_cycle,
    save_checkpoint,
    train_policy_value,
)

__all__ = [
    "BattleLogRecord",
    "PolicyValueMLP",
    "TrainConfig",
    "TrainingLoopConfig",
    "build_model_bonus_fn",
    "encode_state",
    "load_checkpoint",
    "make_log_record",
    "read_log_records",
    "records_to_numpy",
    "run_training_cycle",
    "save_checkpoint",
    "train_policy_value",
    "write_log_record",
]
