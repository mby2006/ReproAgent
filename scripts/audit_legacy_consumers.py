#!/usr/bin/env python3
"""Statically audit dormant ReproAgent consumers without importing application code.

The audit reads Python ASTs and text files only. It never imports, executes, or
shells out to discovered code. Runtime route/import behavior is proven separately
by ``apps/api/tests/test_active_surface.py``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

MODULE_TARGETS = (
    "reproagent_agent.clients.runner",
    "reproagent_agent.graph",
    "reproagent_agent.routers.reproductions",
)
ROUTE_TARGETS = ("/v1/jobs", "/v1/reproductions")
RETIRED_TARGETS = {
    "reproagent_agent.clients.runner",
    "reproagent_agent.routers.reproductions",
    *ROUTE_TARGETS,
}
TARGETS = (*MODULE_TARGETS, *ROUTE_TARGETS)
RETAINED_FILES = {
    "reproagent_agent.clients.runner": ("apps/agent/src/reproagent_agent/clients/runner.py",),
    "reproagent_agent.graph": (
        "apps/agent/src/reproagent_agent/graph/build.py",
        "apps/agent/src/reproagent_agent/graph/nodes.py",
        "apps/agent/src/reproagent_agent/graph/state.py",
    ),
    "reproagent_agent.routers.reproductions": (
        "apps/agent/src/reproagent_agent/routers/reproductions.py",
    ),
    "/v1/reproductions": (
        "apps/agent/src/reproagent_agent/routers/reproductions.py",
        "apps/api/src/reproagent_api/routers/reproductions.py",
    ),
}
CANONICAL_MIGRATIONS = Path("infra/supabase/migrations")
DEPLOY_MIGRATIONS = Path("deploy/server/supabase/migrations")
AUDIT_FILES = {
    "scripts/audit_legacy_consumers.py",
    "scripts/test_audit_legacy_consumers.py",
}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".superpowers",
    ".venv",
    "__pycache__",
    "_bmad-output",
    "node_modules",
}
TEXT_SUFFIXES = {
    ".js",
    ".jsx",
    ".md",
    ".mjs",
    ".cjs",
    ".py",
    ".rst",
    ".sh",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
ROUTE_RE = re.compile(r"(?<![\w/])/v1/(jobs|reproductions)(?=$|[/?#\s'\"`),;\]\\])")
MODULE_RES = {
    target: re.compile(rf"(?<![\w.]){re.escape(target)}(?!\w)") for target in MODULE_TARGETS
}


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _iter_files(root: Path):
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRECTORIES)
        for name in sorted(files):
            path = Path(directory) / name
            relative = _relative(path, root)
            if relative in AUDIT_FILES or path.is_symlink():
                continue
            if path.suffix.lower() in TEXT_SUFFIXES or name == "README":
                yield path, relative


def _classification(relative: str) -> str:
    path = Path(relative)
    parts = path.parts
    if (
        "tests" in parts
        or "test" in parts
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    ):
        return "test"
    if path.suffix.lower() in {".md", ".rst"} or "docs" in parts:
        return "docs-only"
    return "production"


def _module_name(relative: str) -> str:
    path = Path(relative)
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    if "src" in parts:
        return ".".join(parts[parts.index("src") + 1 :])
    return ".".join(parts)


def _matches_module(target: str, imported: str) -> bool:
    return imported == target or imported.startswith(target + ".")


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _resolve_from(module: str, is_package: bool, node: ast.ImportFrom) -> str:
    package = module if is_package else module.rpartition(".")[0]
    if node.level:
        parts = package.split(".") if package else []
        remove = node.level - 1
        if remove:
            parts = parts[:-remove]
        base = ".".join(parts)
        return ".".join(part for part in (base, node.module or "") if part)
    return node.module or ""


def _imports(tree: ast.AST, module: str, is_package: bool) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(module, is_package, node)
            for alias in node.names:
                aliases[alias.asname or alias.name] = ".".join(
                    part for part in (base, alias.name) if part
                )
    return aliases


def _expression_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expression_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _join_path(prefix: str, route: str) -> str:
    if not prefix:
        return route
    return prefix.rstrip("/") + "/" + route.lstrip("/")


def _router_routes(tree: ast.AST) -> list[dict[str, Any]]:
    prefixes: dict[str, str] = {}
    routes: list[dict[str, Any]] = []
    methods = {"get", "post", "put", "patch", "delete", "options", "head"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            function = _expression_name(value.func)
            if function not in {"APIRouter", "fastapi.APIRouter"}:
                continue
            prefix = next((_literal(k.value) for k in value.keywords if k.arg == "prefix"), "")
            for target in targets:
                if isinstance(target, ast.Name):
                    prefixes[target.id] = prefix or ""
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                function = _expression_name(decorator.func)
                if not function or "." not in function:
                    continue
                router_name, method = function.rsplit(".", 1)
                route = _literal(decorator.args[0]) if decorator.args else None
                if method in methods and router_name in prefixes and route is not None:
                    routes.append(
                        {
                            "path": _join_path(prefixes[router_name], route),
                            "methods": [method.upper()],
                        }
                    )
    return sorted(routes, key=lambda route: (route["path"], route["methods"]))


def _route_targets(path: str) -> set[str]:
    return {f"/v1/{match.group(1)}" for match in ROUTE_RE.finditer(path)}


def _migration_hashes(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file() and not path.is_symlink()
    }


def compare_migrations(root: Path) -> dict[str, Any]:
    canonical = _migration_hashes(root / CANONICAL_MIGRATIONS)
    deploy = _migration_hashes(root / DEPLOY_MIGRATIONS)
    missing = sorted(set(canonical) - set(deploy))
    extra = sorted(set(deploy) - set(canonical))
    mismatched = sorted(
        name for name in set(canonical) & set(deploy) if canonical[name] != deploy[name]
    )
    errors = []
    if not (root / CANONICAL_MIGRATIONS).is_dir():
        errors.append(f"missing canonical directory: {CANONICAL_MIGRATIONS.as_posix()}")
    if not (root / DEPLOY_MIGRATIONS).is_dir():
        errors.append(f"missing deploy directory: {DEPLOY_MIGRATIONS.as_posix()}")
    return {
        "ok": not missing and not extra and not mismatched and not errors,
        "canonical_directory": CANONICAL_MIGRATIONS.as_posix(),
        "deploy_directory": DEPLOY_MIGRATIONS.as_posix(),
        "canonical_sha256": canonical,
        "deploy_sha256": deploy,
        "missing": missing,
        "extra": extra,
        "mismatched": mismatched,
        "errors": errors,
    }


def audit_repository(root: Path | str) -> dict[str, Any]:
    """Return a deterministic static audit; no discovered module is imported."""

    root = Path(root).resolve()
    targets: dict[str, Any] = {
        target: {
            "status": "retained-active"
            if target == "reproagent_agent.graph"
            else "retired-by-nonregistration",
            "retained_files": list(RETAINED_FILES.get(target, ())),
            "consumers": [],
            "registrations": [],
            "live_routes": [],
        }
        for target in TARGETS
    }
    consumer_keys: set[tuple[str, str, int, str]] = set()
    scan_errors: list[dict[str, str]] = []
    python_files: dict[str, tuple[ast.AST, str, bool]] = {}
    router_definitions: dict[str, list[dict[str, Any]]] = {}

    def add_consumer(
        target: str,
        relative: str,
        line: int,
        kind: str,
        classification: str,
        *,
        detail: str | None = None,
    ) -> None:
        key = (target, relative, line, kind)
        if key in consumer_keys:
            return
        consumer_keys.add(key)
        blocking = classification == "production" and target in RETIRED_TARGETS
        if blocking:
            disposition = "remove-production-consumer-before-retirement"
        elif classification == "test":
            disposition = "test-only-nonregistration-assertion"
        elif classification == "docs-only":
            disposition = "docs-only-reference"
        else:
            disposition = "retained-active-consumer"
        consumer = {
            "path": relative,
            "line": line,
            "kind": kind,
            "classification": classification,
            "disposition": disposition,
            "blocking": blocking,
        }
        if detail:
            consumer["detail"] = detail
        targets[target]["consumers"].append(consumer)

    for path, relative in _iter_files(root):
        classification = _classification(relative)
        if path.suffix.lower() != ".py":
            for line_number, line in enumerate(_read_text(path).splitlines(), 1):
                for target, pattern in MODULE_RES.items():
                    if pattern.search(line):
                        kind = (
                            "compose-command"
                            if path.suffix.lower() in {".yml", ".yaml"}
                            else "script-reference"
                        )
                        if classification == "docs-only":
                            kind = "documentation-reference"
                        add_consumer(target, relative, line_number, kind, classification)
                for target in ROUTE_TARGETS:
                    if re.search(rf"(?<![\w/]){re.escape(target)}(?=$|[/?#\s'\"`),;\]\\])", line):
                        if classification == "docs-only":
                            kind = "documentation-reference"
                        elif path.suffix.lower() in {".yml", ".yaml"}:
                            kind = "compose-command"
                        elif path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                            kind = "frontend-api-call"
                        else:
                            kind = "script-reference"
                        add_consumer(target, relative, line_number, kind, classification)
            continue

        try:
            source = _read_text(path)
            tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError) as exc:
            scan_errors.append({"path": relative, "error": f"{type(exc).__name__}: {exc}"})
            continue
        module = _module_name(relative)
        is_package = path.name == "__init__.py"
        python_files[relative] = (tree, module, is_package)
        routes = _router_routes(tree)
        if routes:
            router_definitions[module] = routes
        aliases = _imports(tree, module, is_package)
        dormant_file = relative in {
            file for target in RETIRED_TARGETS for file in RETAINED_FILES.get(target, ())
        }
        if dormant_file:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names = [(alias.asname or alias.name, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_from(module, is_package, node)
                imported_names = [
                    (
                        alias.asname or alias.name,
                        ".".join(part for part in (base, alias.name) if part),
                    )
                    for alias in node.names
                ]
            else:
                imported_names = []
            for _, imported in imported_names:
                for target in MODULE_TARGETS:
                    if _matches_module(target, imported):
                        add_consumer(target, relative, node.lineno, "python-import", classification)
            if isinstance(node, ast.Call):
                function = _expression_name(node.func) or ""
                if function in {"importlib.import_module", "__import__"} and node.args:
                    imported = _literal(node.args[0])
                    if imported:
                        for target in MODULE_TARGETS:
                            if _matches_module(target, imported):
                                add_consumer(
                                    target,
                                    relative,
                                    node.lineno,
                                    "python-dynamic-import",
                                    classification,
                                )
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for target in ROUTE_TARGETS:
                    if re.search(
                        rf"(?<![\w/]){re.escape(target)}(?=$|[/?#\s'\"`),;\]\\])", node.value
                    ):
                        add_consumer(
                            target, relative, node.lineno, "python-route-reference", classification
                        )
                for target, pattern in MODULE_RES.items():
                    if pattern.search(node.value):
                        add_consumer(
                            target, relative, node.lineno, "python-reference", classification
                        )

    for relative, (tree, _, _) in python_files.items():
        if _classification(relative) != "production":
            continue
        aliases = _imports(tree, _module_name(relative), Path(relative).name == "__init__.py")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = _expression_name(node.func) or ""
            if not function.endswith(".include_router") or not node.args:
                continue
            argument = _expression_name(node.args[0])
            if not argument:
                continue
            if argument.endswith(".router"):
                argument = argument.removesuffix(".router")
            router_module = aliases.get(argument, argument)
            routes = router_definitions.get(router_module, [])
            registration = {
                "path": relative,
                "line": node.lineno,
                "router_module": router_module,
                "routes": routes,
            }
            if router_module in targets:
                targets[router_module]["registrations"].append(registration)
                targets[router_module]["live_routes"].extend(
                    {
                        "path": route["path"],
                        "methods": route["methods"],
                        "registered_in": relative,
                        "router_module": router_module,
                    }
                    for route in routes
                )
                add_consumer(
                    router_module,
                    relative,
                    node.lineno,
                    "fastapi-router-registration",
                    "production",
                )
            for route in routes:
                for target in _route_targets(route["path"]):
                    targets[target]["registrations"].append(registration)
                    add_consumer(
                        target,
                        relative,
                        node.lineno,
                        "fastapi-router-registration",
                        "production",
                    )
                    targets[target]["live_routes"].append(
                        {
                            "path": route["path"],
                            "methods": route["methods"],
                            "registered_in": relative,
                            "router_module": router_module,
                        }
                    )

    for target, report in targets.items():
        report["consumers"].sort(
            key=lambda consumer: (
                consumer["path"],
                consumer["line"],
                consumer["kind"],
                consumer["classification"],
            )
        )
        report["registrations"].sort(key=lambda item: (item["path"], item["line"]))
        report["live_routes"].sort(
            key=lambda item: (
                item["path"],
                item["methods"],
                item["registered_in"],
                item["router_module"],
            )
        )
        if target in RETIRED_TARGETS and (report["live_routes"] or report["registrations"]):
            report["status"] = "live-registered"

    migrations = compare_migrations(root)
    blocking = [
        {"target": target, **consumer}
        for target, report in targets.items()
        for consumer in report["consumers"]
        if consumer["blocking"]
    ]
    return {
        "ok": not blocking and not scan_errors and migrations["ok"],
        "root": str(root),
        "method": "static AST/text inspection; discovered code is never executed",
        "targets": targets,
        "blocking_consumers": blocking,
        "scan_errors": scan_errors,
        "migrations": migrations,
    }


def render_human(report: dict[str, Any]) -> str:
    if "targets" not in report:
        migrations = report["migrations"]
        return (
            "\n".join(
                [
                    "Migration consistency gate",
                    f"Result: {'PASS' if migrations['ok'] else 'FAIL'}",
                    f"  missing: {', '.join(migrations['missing']) or 'none'}",
                    f"  extra: {', '.join(migrations['extra']) or 'none'}",
                    f"  mismatched: {', '.join(migrations['mismatched']) or 'none'}",
                ]
            )
            + "\n"
        )
    lines = [
        "Legacy consumer audit",
        f"Result: {'PASS' if report['ok'] else 'FAIL'}",
        f"Method: {report['method']}",
    ]
    for target, result in report["targets"].items():
        lines.append(f"\n{target}: {result['status']}")
        lines.append(
            "  live routes: "
            + (
                ", ".join(
                    f"{'/'.join(route['methods'])} {route['path']}"
                    for route in result["live_routes"]
                )
                or "none"
            )
        )
        lines.append("  retained files: " + (", ".join(result["retained_files"]) or "none"))
        if not result["consumers"]:
            lines.append("  consumers: none")
        for consumer in result["consumers"]:
            blocker = " BLOCKING" if consumer["blocking"] else ""
            lines.append(
                f"  - {consumer['classification']}/{consumer['kind']} "
                f"{consumer['path']}:{consumer['line']} "
                f"({consumer['disposition']}){blocker}"
            )
    migrations = report["migrations"]
    lines.extend(
        [
            "",
            f"Migrations: {'PASS' if migrations['ok'] else 'FAIL'}",
            f"  missing: {', '.join(migrations['missing']) or 'none'}",
            f"  extra: {', '.join(migrations['extra']) or 'none'}",
            f"  mismatched: {', '.join(migrations['mismatched']) or 'none'}",
        ]
    )
    if report["scan_errors"]:
        lines.append("Scan errors: " + json.dumps(report["scan_errors"], sort_keys=True))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--output", type=Path, help="write output to this path instead of stdout")
    parser.add_argument(
        "--check-migrations",
        action="store_true",
        help="run only the exact filename-set and SHA-256 migration consistency gate",
    )
    args = parser.parse_args(argv)
    if args.check_migrations:
        migrations = compare_migrations(args.root)
        report = {"ok": migrations["ok"], "migrations": migrations}
    else:
        report = audit_repository(args.root)
    rendered = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_human(report)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
