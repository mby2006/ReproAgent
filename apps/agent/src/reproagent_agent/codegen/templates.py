"""Static, audited LeNet-5/MNIST code templates + plan-driven config.

The .py files are parameter-agnostic: every tunable lives in config.yaml, which
is rendered from the approved ExperimentPlan. Code reads only config.yaml and the
baked dataset at /data/mnist, and writes only to /output -- no environment
variables, no network, no host paths. This keeps the executable surface constant
and auditable while the plan drives behavior.
"""

import yaml
from reproagent_schemas import DEPENDENCY_WHITELIST, ExperimentPlan

from ..planning.assumptions import normalize_loss, normalize_optimizer
from ..planning.scaling import MNIST_NORMALIZE_MEAN, MNIST_NORMALIZE_STD

MODEL_PY = '''"""LeNet-5 for MNIST (1x28x28 -> 10 classes)."""

import torch.nn as nn


class LeNet5(nn.Module):
    def __init__(self, num_classes: int = 10):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 6, kernel_size=5, padding=2),
            nn.ReLU(),
            nn.MaxPool2d(2, 2),
            nn.Conv2d(6, 16, kernel_size=5),
            nn.ReLU(),
            nn.MaxPool2d(2, 2),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(16 * 5 * 5, 120),
            nn.ReLU(),
            nn.Linear(120, 84),
            nn.ReLU(),
            nn.Linear(84, num_classes),
        )

    def forward(self, x):
        return self.classifier(self.features(x))


def build_model(num_classes: int = 10) -> nn.Module:
    return LeNet5(num_classes)
'''

TRAIN_PY = '''"""Train LeNet-5 on a MNIST subset and emit deterministic metrics.json."""

import json
import time
from pathlib import Path

import torch
import yaml
from torch.utils.data import DataLoader, Subset
from torchvision import datasets, transforms

from model import build_model

CONFIG_PATH = "config.yaml"
OPTIMIZERS = {
    "sgd": torch.optim.SGD,
    "adam": torch.optim.Adam,
    "adamw": torch.optim.AdamW,
    "adagrad": torch.optim.Adagrad,
    "adadelta": torch.optim.Adadelta,
    "rmsprop": torch.optim.RMSprop,
}


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def build_loaders(cfg: dict):
    data = cfg["data"]
    transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize((data["normalize_mean"],), (data["normalize_std"],)),
        ]
    )
    train_full = datasets.MNIST(data["dir"], train=True, download=False, transform=transform)
    test_full = datasets.MNIST(data["dir"], train=False, download=False, transform=transform)
    train_set = Subset(train_full, range(data["train_subset_size"]))
    test_set = Subset(test_full, range(data["test_subset_size"]))
    batch_size = cfg["training"]["batch_size"]
    train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True)
    test_loader = DataLoader(test_set, batch_size=batch_size, shuffle=False)
    return train_loader, test_loader, train_set, test_set


def evaluate(model, loader, criterion):
    model.eval()
    correct = 0
    total = 0
    loss_sum = 0.0
    with torch.no_grad():
        for features, labels in loader:
            outputs = model(features)
            loss_sum += criterion(outputs, labels).item() * labels.size(0)
            correct += (outputs.argmax(1) == labels).sum().item()
            total += labels.size(0)
    return correct / max(total, 1), loss_sum / max(total, 1)


def main() -> None:
    cfg = load_config()
    seed = cfg["seed"]
    torch.manual_seed(seed)
    output = Path(cfg["output"]["dir"])
    output.mkdir(parents=True, exist_ok=True)

    train_loader, test_loader, train_set, test_set = build_loaders(cfg)
    model = build_model(cfg["data"]["num_classes"])
    training = cfg["training"]
    optimizer = OPTIMIZERS[training["optimizer"].lower()](
        model.parameters(),
        lr=training["learning_rate"],
        weight_decay=training.get("weight_decay", 0.0),
    )
    criterion = torch.nn.CrossEntropyLoss()

    started = time.time()
    log = []
    train_loss = 0.0
    for epoch in range(training["epochs"]):
        model.train()
        running = 0.0
        seen = 0
        for features, labels in train_loader:
            optimizer.zero_grad()
            loss = criterion(model(features), labels)
            loss.backward()
            optimizer.step()
            running += loss.item() * labels.size(0)
            seen += labels.size(0)
        train_loss = running / max(seen, 1)
        log.append({"epoch": epoch + 1, "train_loss": train_loss})

    accuracy, test_loss = evaluate(model, test_loader, criterion)
    runtime = time.time() - started
    target = cfg["evaluation"]["target_metric"]
    metrics = {
        "schema_version": "1.0",
        "target_metric": target,
        "metric_value": accuracy,
        "accuracy": accuracy,
        "train_loss": train_loss,
        "test_loss": test_loss,
        "epochs_completed": training["epochs"],
        "train_samples": len(train_set),
        "test_samples": len(test_set),
        "seed": seed,
        "runtime_seconds": runtime,
    }
    (output / cfg["output"]["metrics_filename"]).write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    (output / "training_log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    torch.save(model.state_dict(), output / "model.pt")
    print(json.dumps({"status": "ok", "accuracy": accuracy, "epochs": training["epochs"]}))


if __name__ == "__main__":
    main()
'''

EVALUATE_PY = '''"""Standalone evaluation of a saved LeNet-5 checkpoint on the MNIST test subset."""

import json
from pathlib import Path

import torch
import yaml
from torch.utils.data import DataLoader, Subset
from torchvision import datasets, transforms

from model import build_model

CONFIG_PATH = "config.yaml"


def main() -> None:
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)
    torch.manual_seed(cfg["seed"])
    data = cfg["data"]
    transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize((data["normalize_mean"],), (data["normalize_std"],)),
        ]
    )
    test_full = datasets.MNIST(data["dir"], train=False, download=False, transform=transform)
    test_set = Subset(test_full, range(data["test_subset_size"]))
    loader = DataLoader(test_set, batch_size=cfg["training"]["batch_size"], shuffle=False)

    output = Path(cfg["output"]["dir"])
    model = build_model(data["num_classes"])
    model.load_state_dict(torch.load(output / "model.pt", weights_only=True))
    model.eval()
    criterion = torch.nn.CrossEntropyLoss()
    correct = 0
    total = 0
    loss_sum = 0.0
    with torch.no_grad():
        for features, labels in loader:
            outputs = model(features)
            loss_sum += criterion(outputs, labels).item() * labels.size(0)
            correct += (outputs.argmax(1) == labels).sum().item()
            total += labels.size(0)
    accuracy = correct / max(total, 1)
    report = {
        "schema_version": "1.0",
        "target_metric": cfg["evaluation"]["target_metric"],
        "metric_value": accuracy,
        "accuracy": accuracy,
        "test_loss": loss_sum / max(total, 1),
        "epochs_completed": 0,
        "train_samples": 0,
        "test_samples": len(test_set),
        "seed": cfg["seed"],
        "runtime_seconds": 0.0,
    }
    (output / "eval_metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "accuracy": accuracy}))


if __name__ == "__main__":
    main()
'''


def requirements_txt() -> str:
    return "".join(f"{name}=={version}\n" for name, version in DEPENDENCY_WHITELIST.items())


def build_config(plan: ExperimentPlan) -> dict:
    training = plan.training_config
    weight_decay = training.weight_decay.value if training.weight_decay is not None else 0.0
    optimizer = normalize_optimizer(training.optimizer.value)
    if optimizer is None:
        # never silently substitute: an unexecutable optimizer must fail at
        # generation time, not as a KeyError inside the sandbox
        raise ValueError(f"unsupported optimizer: {training.optimizer.value!r}")
    loss = normalize_loss(training.loss_function.value)
    if loss is None:
        # the train template implements cross-entropy only; refuse to generate
        # code that would silently run a different loss than the approved plan
        raise ValueError(f"unsupported loss_function: {training.loss_function.value!r}")
    return {
        "seed": training.seed,
        "data": {
            "dir": "/data/mnist",
            "train_subset_size": plan.dataset_config.train_subset_size,
            "test_subset_size": plan.dataset_config.test_subset_size,
            "num_classes": plan.dataset_config.num_classes,
            "input_shape": list(plan.dataset_config.input_shape),
            "normalize_mean": MNIST_NORMALIZE_MEAN,
            "normalize_std": MNIST_NORMALIZE_STD,
        },
        "model": {"architecture": plan.model_cfg.architecture},
        "training": {
            "optimizer": optimizer,
            "learning_rate": training.learning_rate.value,
            "loss_function": loss,
            "weight_decay": weight_decay,
            "batch_size": training.batch_size.value,
            "epochs": training.epochs.value,
        },
        "evaluation": {
            "target_metric": plan.evaluation_config.target_metric,
            "tolerance": plan.evaluation_config.tolerance,
        },
        "output": {"dir": "/output", "metrics_filename": "metrics.json"},
    }


def render_config_yaml(plan: ExperimentPlan) -> str:
    return yaml.safe_dump(build_config(plan), sort_keys=False, allow_unicode=True)