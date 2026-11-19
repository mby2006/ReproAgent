"""Static validation of a generated CodeBundle before it may reach the Runner.

Checks Python syntax, path safety, the dependency whitelist, the entrypoint,
required config keys and the metrics output interface, and rejects any code that
could touch the host, environment variables, secrets or arbitrary network
resources. This is a defense-in-depth gate on top of the locked-down sandbox.
"""

import ast

import yaml
from reproagent_schemas import (
    DEPENDENCY_WHITELIST,
    CodeBundle,
    ValidationIssue,
    ValidationReport,
    is_whitelisted,
)

BANNED_MODULES = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "urllib3",
    "http",
    "ftplib",
    "smtplib",
    "telnetlib",
    "ctypes",
    "pickle",
    "shutil",
    "multiprocessing",
    "importlib",
    "signal",
    "resource",
    "pty",
    "pwd",
    "spwd",
}

BANNED_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "input",
    "breakpoint",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
}

BANNED_ATTRS = {
    "environ",
    "getenv",
    "putenv",
    "system",
    "popen",
    "execv",
    "execve",
    "spawnl",
    "spawnv",
    "fork",
    "kill",
    "home",
    "expanduser",
    "expandvars",
}

BANNED_DOTTED = {"torch.hub", "torch.utils.model_zoo", "socket.socket"}

SENSITIVE_SUBSTRINGS = (
    "/etc",
    "/proc",
    "/sys/",
    "/root",
    "/home",
    "/var/run",
    ".ssh",
    ".env",
    "id_rsa",
    "docker.sock",
    "c:\\",
    "service_role",
    "api_key",
    "secret",
)

REQUIRED_CONFIG_KEYS = (
    ("seed",),
    ("data", "dir"),
    ("data", "train_subset_size"),
    ("data", "test_subset_size"),
    ("data", "num_classes"),
    ("training", "optimizer"),
    ("training", "learning_rate"),
    ("training", "batch_size"),
    ("training", "epochs"),
    ("evaluation", "target_metric"),
    ("output", "dir"),
    ("output", "metrics_filename"),
)

METRIC_KEYS = ("schema_version", "target_metric", "metric_value")


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _check_tree(tree: ast.AST, path: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def add(code: str, message: str) -> None:
        issues.append(ValidationIssue(code=code, message=message, path=path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_MODULES:
                    add("banned_import", f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_MODULES:
                add("banned_import", f"forbidden import from: {node.module}")
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRS:
                add("banned_attr", f"forbidden attribute access: .{node.attr}")
            dotted = _dotted(node)
            if any(dotted.startswith(b) for b in BANNED_DOTTED):
                add("banned_attr", f"forbidden network/host access: {dotted}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
                add("banned_call", f"forbidden call: {node.func.id}()")
            for keyword in node.keywords:
                if (
                    keyword.arg == "download"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    add("network_download", "runtime data download is forbidden (download=True)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            for marker in SENSITIVE_SUBSTRINGS:
                if marker in lowered:
                    add("sensitive_path", f"reference to sensitive resource: {marker}")
                    break
    return issues


def _check_config(bundle: CodeBundle) -> list[ValidationIssue]:
    config = next((f for f in bundle.files if f.path == "config.yaml"), None)
    if config is None:
        return [ValidationIssue(code="missing_config", message="config.yaml is required")]
    try:
        data = yaml.safe_load(config.content)
    except yaml.YAMLError as exc:
        return [ValidationIssue(code="config_syntax", message=f"config.yaml invalid: {exc}")]
    if not isinstance(data, dict):
        return [ValidationIssue(code="config_syntax", message="config.yaml must be a mapping")]
    issues: list[ValidationIssue] = []
    for keys in REQUIRED_CONFIG_KEYS:
        cursor = data
        for key in keys:
            if not isinstance(cursor, dict) or key not in cursor:
                issues.append(
                    ValidationIssue(
                        code="missing_config_key",
                        message=f"config.yaml missing key: {'.'.join(keys)}",
                        path="config.yaml",
                    )
                )
                break
            cursor = cursor[key]
    return issues


def _check_metrics_interface(bundle: CodeBundle) -> list[ValidationIssue]:
    entry = next((f for f in bundle.files if f.path == bundle.entrypoint), None)
    if entry is None:
        return []
    issues: list[ValidationIssue] = []
    if "metrics_filename" not in entry.content and "metrics.json" not in entry.content:
        issues.append(
            ValidationIssue(
                code="missing_metrics_output",
                message="entrypoint must write the metrics file",
                path=bundle.entrypoint,
            )
        )
    for key in METRIC_KEYS:
        if key not in entry.content:
            issues.append(
                ValidationIssue(
                    code="missing_metric_key",
                    message=f"entrypoint must emit metric key: {key}",
                    path=bundle.entrypoint,
                )
            )
    return issues


def _check_requirements(bundle: CodeBundle) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for requirement in bundle.requirements:
        if not is_whitelisted(requirement):
            issues.append(
                ValidationIssue(
                    code="dependency_not_whitelisted",
                    message=f"dependency not in pinned whitelist: {requirement}",
                    path="requirements.txt",
                )
            )
    req_file = next((f for f in bundle.files if f.path == "requirements.txt"), None)
    if req_file is not None:
        for line in req_file.content.splitlines():
            line = line.strip()
            if line and not is_whitelisted(line):
                issues.append(
                    ValidationIssue(
                        code="dependency_not_whitelisted",
                        message=f"requirements.txt line not whitelisted: {line}",
                        path="requirements.txt",
                    )
                )
    expected = {f"{n}=={v}" for n, v in DEPENDENCY_WHITELIST.items()}
    if req_file is not None:
        present = {line.strip() for line in req_file.content.splitlines() if line.strip()}
        if present != expected:
            issues.append(
                ValidationIssue(
                    code="requirements_mismatch",
                    message="requirements.txt must exactly match the pinned whitelist",
                    path="requirements.txt",
                )
            )
    return issues


def validate_bundle(bundle: CodeBundle) -> ValidationReport:
    issues: list[ValidationIssue] = []
    paths = [f.path for f in bundle.files]
    if len(set(paths)) != len(paths):
        issues.append(ValidationIssue(code="duplicate_path", message="duplicate file path"))
    if bundle.entrypoint not in paths:
        issues.append(
            ValidationIssue(code="missing_entrypoint", message="entrypoint file is missing")
        )

    for code_file in bundle.files:
        if not code_file.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(code_file.content, filename=code_file.path)
        except SyntaxError as exc:
            issues.append(
                ValidationIssue(
                    code="syntax_error",
                    message=f"{code_file.path}:{exc.lineno}: {exc.msg}",
                    path=code_file.path,
                )
            )
            continue
        issues.extend(_check_tree(tree, code_file.path))

    issues.extend(_check_config(bundle))
    issues.extend(_check_metrics_interface(bundle))
    issues.extend(_check_requirements(bundle))
    return ValidationReport(ok=not issues, issues=issues)