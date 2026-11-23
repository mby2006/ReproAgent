"""Static, audited LeNet-5/MNIST code templates + plan-driven config.

The .py files are parameter-agnostic: every tunable lives in config.yaml, which
is rendered from the approved ExperimentPlan. Code reads only config.yaml and the
baked dataset at /data/mnist, and writes only to /output -- no environment
variables, no network, no host paths. This keeps the executable surface constant
and auditable while the plan drives behavior.
"""

import yaml
from reproagent_schemas import DEPENDENCY_WHITELIST, ExperimentPlan
from reproagent_schemas.plan import execution_metric

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

_EVALUATION = '''def evaluate(model, loader, criterion, output, target):
    model.eval()
    correct = 0
    total = 0
    loss_sum = 0.0
    samples = []
    subset = loader.dataset
    with torch.no_grad():
        for features, labels in loader:
            outputs = model(features)
            predictions = outputs.argmax(1)
            loss_sum += criterion(outputs, labels).item() * labels.size(0)
            correct += (predictions == labels).sum().item()
            for offset in range(min(len(labels), 12 - len(samples))):
                index = subset.indices[total + offset]
                pixels = subset.dataset.data[index].flatten().tolist()
                samples.append({"index": index, "pixels": bytes(pixels).hex(),
                                "label": int(labels[offset]), "prediction": int(predictions[offset])})
            total += labels.size(0)
    values = {"accuracy": correct / total, "error_rate": (total - correct) / total,
              "test_loss": loss_sum / total}
    (output / "prediction_examples.json").write_text(
        json.dumps({"version": 1, "samples": samples}), encoding="utf-8"
    )
    return {**values, "target_metric": target, "metric_value": values[target],
            "correct_count": correct, "evaluated_count": total}


'''

TRAIN_PY = '''"""Train LeNet-5 on a MNIST subset and emit deterministic metrics.json.

Fixed seed -> fixed initial weights -> fixed sample order. The run writes a
reproducibility fingerprint of all three so two paired runs can prove they were
actually comparable instead of asserting it.
"""

import hashlib
import json
import time
from pathlib import Path

import numpy as np
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


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def indices_fingerprint(indices) -> str:
    return text_fingerprint(",".join(str(int(index)) for index in indices))


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in ("model.py", "train.py", "evaluate.py"):
        path = Path(name)
        if not path.exists():
            continue
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def weights_fingerprint(model) -> str:
    digest = hashlib.sha256()
    state = model.state_dict()
    for name in sorted(state):
        digest.update(name.encode("utf-8"))
        array = np.ascontiguousarray(state[name].detach().cpu().numpy())
        digest.update(array.tobytes())
    return digest.hexdigest()


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
    train_indices = list(range(data["train_subset_size"]))
    test_indices = list(range(data["test_subset_size"]))
    train_set = Subset(train_full, train_indices)
    test_set = Subset(test_full, test_indices)
    batch_size = cfg["training"]["batch_size"]
    test_loader = DataLoader(test_set, batch_size=batch_size, shuffle=False)
    return train_set, test_loader, train_indices, test_indices


''' + _EVALUATION + '''def main() -> None:
    cfg = load_config()
    seed = cfg["seed"]
    # One seed fixes both the initial weights and the sampling stream, so a
    # paired run with the same seed starts from byte-identical parameters.
    torch.manual_seed(seed)
    output = Path(cfg["output"]["dir"])
    output.mkdir(parents=True, exist_ok=True)

    train_set, test_loader, train_indices, test_indices = build_loaders(cfg)
    model = build_model(cfg["data"]["num_classes"])
    init_fingerprint = weights_fingerprint(model)

    training = cfg["training"]
    optimizer = OPTIMIZERS[training["optimizer"].lower()](
        model.parameters(),
        lr=training["learning_rate"],
        weight_decay=training.get("weight_decay", 0.0),
    )
    criterion = torch.nn.CrossEntropyLoss()
    batch_size = training["batch_size"]
    generator = torch.Generator()
    generator.manual_seed(seed)
    first_order = None

    started = time.time()
    log = []
    train_loss = 0.0
    for epoch in range(training["epochs"]):
        order = torch.randperm(len(train_set), generator=generator).tolist()
        if first_order is None:
            first_order = order
        loader = DataLoader(train_set, batch_size=batch_size, sampler=order)
        model.train()
        running = 0.0
        seen = 0
        for features, labels in loader:
            optimizer.zero_grad()
            loss = criterion(model(features), labels)
            loss.backward()
            optimizer.step()
            running += loss.item() * labels.size(0)
            seen += labels.size(0)
        train_loss = running / max(seen, 1)
        log.append({"epoch": epoch + 1, "train_loss": train_loss})

    results = evaluate(model, test_loader, criterion, output, cfg["evaluation"]["target_metric"])
    runtime = time.time() - started
    reproducibility = {
        "dataset_split_sha256": text_fingerprint(
            "train:" + indices_fingerprint(train_indices)
            + "|test:" + indices_fingerprint(test_indices)
        ),
        "init_weights_sha256": init_fingerprint,
        "sample_order_sha256": indices_fingerprint(first_order or train_indices),
        "template_sha256": source_fingerprint(),
        "batch_size": batch_size,
        "epochs": training["epochs"],
        "train_subset_size": len(train_set),
        "test_subset_size": len(test_set),
        "loss_function": training["loss_function"],
        "optimizer": training["optimizer"],
        "learning_rate": training["learning_rate"],
    }
    metrics = {
        "schema_version": "1.2",
        **results,
        "train_loss": train_loss,
        "epochs_completed": training["epochs"],
        "train_samples": len(train_set),
        "test_samples": len(test_set),
        "seed": seed,
        "runtime_seconds": runtime,
        "reproducibility": reproducibility,
    }
    (output / cfg["output"]["metrics_filename"]).write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    (output / "training_log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    (output / "reproducibility.json").write_text(
        json.dumps(reproducibility, indent=2), encoding="utf-8"
    )
    torch.save(model.state_dict(), output / "model.pt")
    print(json.dumps({"status": "ok", "accuracy": results["accuracy"], "epochs": training["epochs"]}))


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


''' + _EVALUATION + '''def main() -> None:
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
    results = evaluate(model, loader, torch.nn.CrossEntropyLoss(), output, cfg["evaluation"]["target_metric"])
    report = {
        "schema_version": "1.1",
        **results,
        "epochs_completed": 0,
        "train_samples": 0,
        "test_samples": len(test_set),
        "seed": cfg["seed"],
        "runtime_seconds": 0.0,
    }
    (output / "eval_metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "accuracy": results["accuracy"]}))


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
    target = execution_metric(plan.evaluation_config.target_metric)
    if target is None:
        raise ValueError("执行目标请选择准确率、错误率或测试损失")
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
            "target_metric": target,
            "tolerance": plan.evaluation_config.tolerance,
        },
        "output": {"dir": "/output", "metrics_filename": "metrics.json"},
    }


def render_config_yaml(plan: ExperimentPlan) -> str:
    return yaml.safe_dump(build_config(plan), sort_keys=False, allow_unicode=True)