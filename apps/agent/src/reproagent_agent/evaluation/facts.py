"""Extract implementation facts from the PINNED code files via AST/config parsing.

The evaluator never trusts a hardcoded description of the templates: it reads
the exact code version bound to the run (execution_run_files), so a repaired
bundle that deviates from the canonical implementation is detected.
"""

import ast

import yaml
from reproagent_schemas import CodeFile, SchemaModel

_LOSS_NAMES = {
    "CrossEntropyLoss": "cross_entropy",
    "MSELoss": "mse",
    "NLLLoss": "nll",
    "BCELoss": "bce",
    "BCEWithLogitsLoss": "bce_with_logits",
    "L1Loss": "l1",
}


class ConvLayer(SchemaModel):
    in_channels: int
    out_channels: int
    kernel_size: int
    padding: int = 0


class LinearLayer(SchemaModel):
    in_features: int
    out_features: int


class PoolLayer(SchemaModel):
    kind: str
    kernel_size: int
    stride: int


class ImplementationFacts(SchemaModel):
    architecture: str | None = None
    conv_layers: list[ConvLayer] = []
    linear_layers: list[LinearLayer] = []
    activations: list[str] = []
    pooling: list[PoolLayer] = []
    output_features: int | None = None
    loss_function: str | None = None
    preprocessing: list[str] = []
    normalize_mean: float | None = None
    normalize_std: float | None = None
    dataset: str | None = None
    config: dict = {}
    model_source: str | None = None
    train_source: str | None = None
    config_source: str | None = None


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _const_int(node: ast.expr, names: dict[str, int]) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return int(node.value)
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult | ast.Add | ast.Sub):
        left = _const_int(node.left, names)
        right = _const_int(node.right, names)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Add):
            return left + right
        return left - right
    return None


def _int_arg(
    node: ast.Call, index: int, keyword: str, names: dict[str, int], default: int = 0
) -> int | None:
    if len(node.args) > index:
        value = _const_int(node.args[index], names)
        if value is not None:
            return value
    for kw in node.keywords:
        if kw.arg == keyword:
            value = _const_int(kw.value, names)
            if value is not None:
                return value
    return default


def _init_defaults(tree: ast.Module) -> dict[str, int]:
    names: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            args = node.args
            defaults = args.defaults
            offset = len(args.args) - len(defaults)
            for i, default in enumerate(defaults):
                value = _const_int(default, names)
                if value is not None:
                    names[args.args[offset + i].arg] = value
    return names


def _extract_model(tree: ast.Module, facts: ImplementationFacts) -> None:
    names = _init_defaults(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "Conv2d":
            layer = ConvLayer(
                in_channels=_int_arg(node, 0, "in_channels", names) or 0,
                out_channels=_int_arg(node, 1, "out_channels", names) or 0,
                kernel_size=_int_arg(node, 2, "kernel_size", names) or 0,
                padding=_int_arg(node, 3, "padding", names, 0) or 0,
            )
            facts.conv_layers.append(layer)
        elif name == "Linear":
            facts.linear_layers.append(
                LinearLayer(
                    in_features=_int_arg(node, 0, "in_features", names) or 0,
                    out_features=_int_arg(node, 1, "out_features", names) or 0,
                )
            )
        elif name in ("ReLU", "Tanh", "Sigmoid", "GELU", "LeakyReLU"):
            facts.activations.append(name.lower())
        elif name in ("MaxPool2d", "AvgPool2d"):
            kernel = _int_arg(node, 0, "kernel_size", names) or 0
            facts.pooling.append(
                PoolLayer(
                    kind="max" if name == "MaxPool2d" else "average",
                    kernel_size=kernel,
                    stride=_int_arg(node, 1, "stride", names, kernel) or kernel,
                )
            )
    if facts.linear_layers:
        facts.output_features = facts.linear_layers[-1].out_features


def _extract_train(tree: ast.Module, facts: ImplementationFacts) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _LOSS_NAMES and facts.loss_function is None:
            facts.loss_function = _LOSS_NAMES[name]
        elif name == "MNIST":
            facts.dataset = "mnist"
        elif name == "Compose":
            for arg in node.args:
                if not isinstance(arg, ast.List):
                    continue
                for item in arg.elts:
                    if not isinstance(item, ast.Call):
                        continue
                    transform = _call_name(item)
                    if transform == "ToTensor":
                        facts.preprocessing.append("to_tensor")
                    elif transform == "Normalize":
                        facts.preprocessing.append("normalize")
                        if item.args and isinstance(item.args[0], ast.Tuple):
                            values = [
                                e.value for e in item.args[0].elts if isinstance(e, ast.Constant)
                            ]
                            if values:
                                facts.normalize_mean = float(values[0])
                        if len(item.args) > 1 and isinstance(item.args[1], ast.Tuple):
                            values = [
                                e.value for e in item.args[1].elts if isinstance(e, ast.Constant)
                            ]
                            if values:
                                facts.normalize_std = float(values[0])
                    elif transform:
                        facts.preprocessing.append(transform.lower())


def extract_facts(code_files: list[CodeFile]) -> ImplementationFacts:
    facts = ImplementationFacts()
    for file in code_files:
        if file.path == "model.py":
            facts.model_source = file.path
            try:
                _extract_model(ast.parse(file.content), facts)
            except SyntaxError:
                facts.model_source = None
        elif file.path == "train.py":
            facts.train_source = file.path
            try:
                _extract_train(ast.parse(file.content), facts)
            except SyntaxError:
                facts.train_source = None
        elif file.path == "config.yaml":
            facts.config_source = file.path
            try:
                loaded = yaml.safe_load(file.content)
                facts.config = loaded if isinstance(loaded, dict) else {}
            except yaml.YAMLError:
                facts.config_source = None
    architecture = facts.config.get("model", {}).get("architecture")
    facts.architecture = str(architecture) if architecture else None
    data_cfg = facts.config.get("data", {})
    if facts.dataset is None and data_cfg.get("dir"):
        data_dir = str(data_cfg["dir"]).lower()
        if "mnist" in data_dir:
            facts.dataset = "mnist"
    if not facts.output_features and data_cfg.get("num_classes"):
        facts.output_features = int(data_cfg["num_classes"])
    if facts.normalize_mean is None and data_cfg.get("normalize_mean") is not None:
        facts.normalize_mean = float(data_cfg["normalize_mean"])
    if facts.normalize_std is None and data_cfg.get("normalize_std") is not None:
        facts.normalize_std = float(data_cfg["normalize_std"])
    return facts