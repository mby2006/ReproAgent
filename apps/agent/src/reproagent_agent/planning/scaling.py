"""MVP scaling profile for LeNet-5 + MNIST (v1).

These constants are intentional scale-down defaults, NOT the paper's settings.
Every value sourced from here is tagged ParamSource.MVP_SCALING and surfaced in
the plan's deviations_from_paper so it can never masquerade as paper config.
"""

from reproagent_schemas import OPTIMIZER_NAMES, SchemaModel

SCALING_PROFILE = "mvp_lenet5_mnist_v1"

MVP_TRAIN_SUBSET = 10_000
MVP_TEST_SUBSET = 10_000
MVP_EPOCHS = 3
MVP_BATCH_SIZE = 64
MVP_SEED = 42
MVP_MAX_SECONDS = 600
MVP_DEVICE = "cpu"

MNIST_FULL_TRAIN = 60_000
MNIST_FULL_TEST = 10_000
MNIST_NUM_CLASSES = 10
MNIST_INPUT_SHAPE = [1, 28, 28]
MNIST_NORMALIZE_MEAN = 0.1307
MNIST_NORMALIZE_STD = 0.3081

SUPPORTED_OPTIMIZERS = set(OPTIMIZER_NAMES)

# Absolute tolerance for a scaled-down trend comparison (not a paper-equivalence claim).
MVP_TOLERANCE = 0.03


class ScalingConfig(SchemaModel):
    """Tunable MVP knobs; defaults are the v1 scaling profile."""

    train_subset_size: int = MVP_TRAIN_SUBSET
    test_subset_size: int = MVP_TEST_SUBSET
    epochs: int = MVP_EPOCHS
    batch_size: int = MVP_BATCH_SIZE
    max_seconds: int = MVP_MAX_SECONDS
