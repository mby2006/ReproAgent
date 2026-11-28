#!/usr/bin/env python3
"""Validate final-commit-bound v2 acceptance evidence without running live systems.

The only external checks are a read-only Git HEAD lookup and the public archive
hash verifier. No credentialed workflow, database mutation, or evidence synthesis
is performed. Missing, stale, malformed, or incomplete evidence remains pending;
only an explicit failure in current-commit evidence is reported as failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.measure_run_segments import (
        assert_same_environment,
        environment_hash,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/, not the repository, on sys.path.
    from measure_run_segments import assert_same_environment, environment_hash

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ARCHIVE = REPOSITORY_ROOT / "apps/web/public/examples/lenet"
ARCHIVE_RUN = "01993dd36ff17102"
ARCHIVE_MODEL_SHA256 = "01993dd36ff1710272c3c50e16a0ffb10dc2015c08e8080b22b7e480b2c83b47"
ARCHIVE_REPORT_SHA256 = "5a7fcba954b496bd5e87483f7cad81b7155f048a111bcd81d3e769eca9d7349d"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[a-f0-9]{40}$")
GATE_PATHS = {
    "G-01": "g01/result.json",
    "G-02": "g02/result.json",
    "G-03": "g03/result.json",
    "G-04": "g04/result.json",
    "G-05": "g05/result.json",
    "NFR-05": "nfr05/result.json",
    "P1": "p1/result.json",
}
STAGES = (
    "queue_seconds",
    "container_lifecycle_seconds",
    "training_evaluation_seconds",
    "report_generation_seconds",
)
IDENTITY_FIELDS = (
    "run_id",
    "workflow_id",
    "experiment_id",
    "plan_id",
    "plan_version",
    "code_id",
    "code_version",
    "code_hash",
    "report_id",
    "report_version",
    "execution_attempt",
)
REPORT_REFERENCE_FIELDS = (
    "run_id",
    "workflow_id",
    "experiment_id",
    "plan_id",
    "plan_version",
    "code_version",
    "code_hash",
    "execution_attempt",
)


def _mapping(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def validate_commit_binding(evidence: dict, expected_commit_sha: str) -> None:
    if not COMMIT_RE.fullmatch(expected_commit_sha):
        raise ValueError("expected commit SHA must be a full lowercase Git SHA")
    actual = evidence.get("commitSha")
    if not isinstance(actual, str) or not COMMIT_RE.fullmatch(actual):
        raise ValueError(
            "commitSha is missing or malformed; exact final-commit binding is required"
        )
    if actual != expected_commit_sha:
        raise ValueError(
            f"stale or different commitSha: evidence={actual}, expected={expected_commit_sha}"
        )


def _assertions(evidence: dict) -> None:
    assertions = evidence.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise ValueError("assertions must be a nonempty list")
    for index, assertion in enumerate(assertions):
        assertion = _mapping(assertion, f"assertions[{index}]")
        _text(assertion.get("name"), f"assertions[{index}].name")
        if assertion.get("status") != "passed":
            raise ValueError(f"assertions[{index}] did not pass")


def _safe_file(root: Path | None, relative: Any, field: str) -> Path:
    relative = _text(relative, field)
    if root is None:
        raise ValueError(f"{field} requires an evidence root")
    root = root.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"{field} must stay inside the evidence root")
    if not candidate.is_file():
        raise ValueError(f"{field} is missing: {relative}")
    return candidate


def _evidence_paths(evidence: dict, root: Path | None, field: str = "evidence_paths") -> list[str]:
    paths = evidence.get(field)
    if not isinstance(paths, list) or not paths:
        raise ValueError(f"{field} must be a nonempty list")
    result = []
    for index, path in enumerate(paths):
        candidate = _safe_file(root, path, f"{field}[{index}]")
        result.append(candidate.relative_to(root.resolve()).as_posix())
    return result


def _report_identity(report: dict, field: str = "report") -> dict:
    report = _mapping(report, field)
    report_json = _mapping(report.get("report_json"), f"{field}.report_json")
    return _mapping(report_json.get("references"), f"{field}.references")


def validate_g02(
    evidence: dict,
    *,
    expected_commit_sha: str | None = None,
    evidence_root: Path | None = None,
) -> dict:
    # Check the report hash first: the minimal negative contract must explain the
    # missing hash rather than merely complaining that a commit is absent.
    markdown_sha256 = _sha256(evidence.get("markdown_sha256"), "markdown_sha256")
    if expected_commit_sha is not None:
        validate_commit_binding(evidence, expected_commit_sha)
    if evidence.get("passed") is not True:
        raise ValueError("passed must be true")
    identities = {field: evidence.get(field) for field in IDENTITY_FIELDS}
    for field, value in identities.items():
        if field in {"plan_version", "code_version", "report_version", "execution_attempt"}:
            _positive_int(value, field)
        elif field == "code_hash":
            _sha256(value, field)
        else:
            _text(value, field)
    if identities["execution_attempt"] != evidence.get("final_attempt"):
        raise ValueError("final_attempt must equal execution_attempt")
    if evidence.get("status") != "succeeded":
        raise ValueError("G-02 Run status must be succeeded")

    report = _mapping(evidence.get("report"), "report")
    if report.get("id") != identities["report_id"]:
        raise ValueError("report.id does not match report_id")
    if report.get("run_id") != identities["run_id"]:
        raise ValueError("report.run_id does not match run_id")
    if report.get("version") != identities["report_version"]:
        raise ValueError("report_version does not match the immutable report version")
    if report.get("execution_attempt") != identities["execution_attempt"]:
        raise ValueError("execution_attempt does not match the report final attempt")
    references = _report_identity(report)
    for field in REPORT_REFERENCE_FIELDS:
        if references.get(field) != identities[field]:
            raise ValueError(f"{field} is inconsistent with report references")
    generated_file_ids = references.get("generated_file_ids")
    if not isinstance(generated_file_ids, list) or identities["code_id"] not in generated_file_ids:
        raise ValueError("code_id is inconsistent with report references.generated_file_ids")

    qwen = _mapping(evidence.get("qwen"), "qwen")
    _text(qwen.get("provider"), "qwen.provider")
    _text(qwen.get("model"), "qwen.model")
    if qwen.get("real_request") is not True:
        raise ValueError("Qwen evidence must record a real request")
    _sha256(evidence.get("environment_hash"), "environment_hash")
    environment = _mapping(evidence.get("environment"), "environment")
    if not environment:
        raise ValueError("environment must record the execution environment")

    report_path = _safe_file(evidence_root, evidence.get("report_path"), "report_path")
    actual_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    if actual_hash != markdown_sha256:
        raise ValueError("markdown_sha256 does not match the report Markdown bytes")
    _assertions(evidence)
    return {
        "run_id": identities["run_id"],
        "workflow_id": identities["workflow_id"],
        "experiment_id": identities["experiment_id"],
        "plan_id": identities["plan_id"],
        "plan_version": identities["plan_version"],
        "code_hash": identities["code_hash"],
        "report_id": identities["report_id"],
        "report_version": identities["report_version"],
        "final_attempt": identities["execution_attempt"],
        "markdown_sha256": markdown_sha256,
        "environment_hash": evidence["environment_hash"],
        "report_path": report_path.relative_to(evidence_root.resolve()).as_posix(),
    }


def run_archive_verifier(archive_root: Path = PUBLIC_ARCHIVE) -> dict:
    """Run only the public nine-file verifier; never contact a browser or API."""
    script = """
import { pathToFileURL } from 'node:url';
const verifier = await import(pathToFileURL(process.argv[2]).href);
const archive = await verifier.verifyArchive(process.argv[3], '/examples/lenet');
console.log(JSON.stringify({ run: archive.run, verified_files: archive.verifiedFiles }));
"""
    verifier = REPOSITORY_ROOT / "apps/web/scripts/verify-artifact.mjs"
    try:
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                script,
                "verify-v2-acceptance",
                str(verifier),
                str(archive_root.parent.parent),
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("public archive verifier is unavailable") from exc
    if result.returncode != 0:
        raise ValueError("public archive verifier failed")
    try:
        summary = json.loads(result.stdout)
        manifest = json.loads((archive_root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("public archive verifier returned malformed evidence") from exc
    summary = _mapping(summary, "archive verifier output")
    manifest = _mapping(manifest, "archive manifest")
    files = _mapping(manifest.get("files"), "archive manifest files")
    return {
        "run": summary.get("run"),
        "verified_files": summary.get("verified_files"),
        "model_sha256": manifest.get("model_sha256"),
        "report_sha256": files.get("report.md"),
    }


def validate_g01(
    evidence: dict,
    *,
    expected_commit_sha: str,
    archive_result: dict | None = None,
    evidence_root: Path | None = None,
) -> dict:
    validate_commit_binding(evidence, expected_commit_sha)
    if evidence.get("passed") is not True:
        raise ValueError("passed must be true")
    archive = _mapping(evidence.get("archive"), "archive")
    expected_archive = {
        "run": ARCHIVE_RUN,
        "model_sha256": ARCHIVE_MODEL_SHA256,
        "report_sha256": ARCHIVE_REPORT_SHA256,
    }
    for field, expected in expected_archive.items():
        if archive.get(field) != expected:
            raise ValueError(f"{field} does not match the public archive identity")
    visitor = _mapping(evidence.get("visitor"), "visitor")
    if visitor.get("authenticated") is not False:
        raise ValueError("public visitor must be unauthenticated")
    if visitor.get("auth_requests") != 0 or visitor.get("session_requests") != 0:
        raise ValueError("public visitor must make no auth or session requests")
    archive_result = _mapping(archive_result, "archive verifier result")
    if archive_result.get("verified_files") != 9:
        raise ValueError("public archive verifier must verify exactly nine files")
    for field, expected in expected_archive.items():
        if archive_result.get(field) != expected:
            raise ValueError(f"public archive verifier {field} mismatch")
    paths = _evidence_paths(evidence, evidence_root)
    _assertions(evidence)
    return {"archive_run": ARCHIVE_RUN, "verified_files": 9, "evidence_paths": paths}


def validate_g03(
    evidence: dict,
    *,
    expected_commit_sha: str,
    evidence_root: Path | None = None,
) -> dict:
    validate_commit_binding(evidence, expected_commit_sha)
    if evidence.get("passed") is not True:
        raise ValueError("passed must be true")
    if evidence.get("exit_code") != 0 or isinstance(evidence.get("exit_code"), bool):
        raise ValueError("exit_code must be zero")
    matrix = _mapping(evidence.get("matrix"), "matrix")
    required = ("FAILED", "TIMEOUT", "CANCELLED", "OOM", "repair", "restart")
    for name in required:
        case = _mapping(matrix.get(name), f"matrix.{name}")
        if case.get("status") != "passed":
            raise ValueError(f"matrix.{name} must pass")
    _assertions(evidence)
    paths = []
    for name in required:
        case = matrix[name]
        path = _safe_file(evidence_root, case.get("evidence_path"), f"matrix.{name}.evidence_path")
        paths.append(path.relative_to(evidence_root.resolve()).as_posix())
    return {"exit_code": 0, "matrix": list(required), "evidence_paths": paths}


def validate_g04(
    evidence: dict,
    *,
    expected_commit_sha: str,
    evidence_root: Path | None = None,
) -> dict:
    validate_commit_binding(evidence, expected_commit_sha)
    if evidence.get("passed") is not True:
        raise ValueError("passed must be true")
    security = _mapping(evidence.get("container_security"), "container_security")
    for field in (
        "non_root",
        "read_only_root_filesystem",
        "no_new_privileges",
        "host_credentials_absent",
    ):
        if security.get(field) is not True:
            raise ValueError(f"container_security.{field} must be true")
    if security.get("network_mode") != "none":
        raise ValueError("container_security.network_mode must be none")
    cap_drop = security.get("cap_drop")
    if not isinstance(cap_drop, list) or "ALL" not in cap_drop:
        raise ValueError("container_security.cap_drop must contain ALL")

    ownership = evidence.get("ownership_matrix")
    if not isinstance(ownership, list) or len(ownership) != 2:
        raise ValueError("ownership_matrix must contain exactly two accounts")
    accounts = set()
    statuses = []
    for index, row in enumerate(ownership):
        row = _mapping(row, f"ownership_matrix[{index}]")
        accounts.add(_text(row.get("account"), f"ownership_matrix[{index}].account"))
        statuses.append(row.get("http_status"))
    if len(accounts) != 2 or 200 not in statuses or 404 not in statuses:
        raise ValueError("ownership_matrix must show owner access and masked non-owner denial")

    audit_ref = _mapping(evidence.get("legacy_audit"), "legacy_audit")
    audit_path = _safe_file(evidence_root, audit_ref.get("path"), "legacy_audit.path")
    audit_hash = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    if audit_hash != _sha256(audit_ref.get("sha256"), "legacy_audit.sha256"):
        raise ValueError("legacy_audit sha256 does not match its bytes")
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("legacy_audit is malformed JSON") from exc
    audit = _mapping(audit, "legacy_audit")
    if audit.get("ok") is not True or audit.get("blocking_consumers") != []:
        raise ValueError("legacy_audit must have no blocking consumers")
    if audit.get("scan_errors") != []:
        raise ValueError("legacy_audit must have no scan errors")
    migrations = _mapping(audit.get("migrations"), "legacy_audit.migrations")
    if migrations.get("ok") is not True:
        raise ValueError("legacy_audit migrations must pass")
    for field in ("errors", "missing", "mismatched"):
        if migrations.get(field) != []:
            raise ValueError(f"legacy_audit migrations.{field} must be empty")
    canonical = _mapping(migrations.get("canonical_sha256"), "canonical_sha256")
    deploy = _mapping(migrations.get("deploy_sha256"), "deploy_sha256")
    if len(canonical) != 16 or canonical != deploy:
        raise ValueError("legacy_audit must bind 16 identical canonical/deploy migrations")
    for name, digest in canonical.items():
        _text(name, "canonical migration name")
        _sha256(digest, f"canonical migration {name} sha256")
    _assertions(evidence)
    return {
        "ownership_accounts": sorted(accounts),
        "canonical_migrations": len(canonical),
        "deploy_migrations": len(deploy),
        "legacy_audit_sha256": audit_hash,
    }


def validate_g05(
    evidence: dict,
    *,
    expected_commit_sha: str,
    evidence_root: Path | None = None,
) -> dict:
    validate_commit_binding(evidence, expected_commit_sha)
    if evidence.get("passed") is not True:
        raise ValueError("passed must be true")
    commands = evidence.get("commands")
    if not isinstance(commands, list):
        raise ValueError("commands must be a list")
    by_name = {}
    for index, command in enumerate(commands):
        command = _mapping(command, f"commands[{index}]")
        name = _text(command.get("name"), f"commands[{index}].name")
        if name in by_name:
            raise ValueError(f"duplicate command at commands[{index}]")
        if command.get("exit_code") != 0 or isinstance(command.get("exit_code"), bool):
            raise ValueError(f"commands[{index}].exit_code must be zero")
        by_name[name] = command
    for name in ("check", "check_integration", "browser_smoke"):
        if name not in by_name:
            raise ValueError(f"missing command: {name}")
    browser_path = _safe_file(evidence_root, evidence.get("browser_result"), "browser_result")
    try:
        browser = json.loads(browser_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("browser_result is malformed JSON") from exc
    try:
        validate_commit_binding(_mapping(browser, "browser_result"), expected_commit_sha)
    except ValueError as exc:
        raise ValueError(f"browser_result {exc}") from exc
    if browser.get("passed") is not True:
        raise ValueError("browser_result did not pass")
    _assertions(evidence)
    return {
        "commands": ["check", "check_integration", "browser_smoke"],
        "browser_result": browser_path.relative_to(evidence_root.resolve()).as_posix(),
    }


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be finite and nonnegative")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be finite and nonnegative") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return number


def validate_nfr05(evidence: dict, *, expected_commit_sha: str | None = None) -> dict:
    runs = evidence.get("runs")
    if not isinstance(runs, list) or len(runs) != 3:
        raise ValueError("exactly 3 Runs are required")
    run_ids = []
    for index, run in enumerate(runs):
        run = _mapping(run, f"runs[{index}]")
        run_ids.append(_text(run.get("run_id"), f"runs[{index}].run_id"))
        _text(run.get("experiment_id"), f"runs[{index}].experiment_id")
        _text(run.get("plan_id"), f"runs[{index}].plan_id")
        _positive_int(run.get("plan_version"), f"runs[{index}].plan_version")
        _positive_int(run.get("code_version"), f"runs[{index}].code_version")
        _positive_int(run.get("attempt"), f"runs[{index}].attempt")
        if run.get("status") != "succeeded":
            raise ValueError(f"runs[{index}] must have succeeded status")
    if len(set(run_ids)) != 3:
        raise ValueError("exactly 3 distinct Run IDs are required")
    if expected_commit_sha is not None:
        validate_commit_binding(evidence, expected_commit_sha)
    if evidence.get("passed") is not True:
        raise ValueError("passed must be true")
    assert_same_environment(runs)
    canonical_hash = environment_hash(runs[0]["environment"])
    summary = _mapping(evidence.get("summary"), "summary")
    if summary.get("environment_hash") != canonical_hash:
        raise ValueError("summary environment_hash does not match canonical environment bytes")
    for index, run in enumerate(runs):
        values = {
            stage: _finite_nonnegative(run.get(stage), f"runs[{index}].{stage}")
            for stage in STAGES
        }
        report = _mapping(run.get("report"), f"runs[{index}].report")
        _text(report.get("id"), f"runs[{index}].report.id")
        if report.get("run_id") != run["run_id"]:
            raise ValueError(f"runs[{index}].report.run_id does not match run_id")
        _positive_int(report.get("version"), f"runs[{index}].report.version")
        if report.get("execution_attempt") != run.get("attempt"):
            raise ValueError(f"runs[{index}].execution_attempt must match the final attempt")
        references = _report_identity(report, f"runs[{index}].report")
        expected = {
            "run_id": run["run_id"],
            "experiment_id": run["experiment_id"],
            "plan_id": run["plan_id"],
            "plan_version": run["plan_version"],
            "code_version": run["code_version"],
            "code_hash": run["environment"]["code_hash"],
            "execution_attempt": run["attempt"],
        }
        for field, value in expected.items():
            if references.get(field) != value:
                raise ValueError(f"runs[{index}].{field} is inconsistent with report references")
        if values["container_lifecycle_seconds"] < values["training_evaluation_seconds"]:
            raise ValueError(
                f"runs[{index}].container_lifecycle_seconds is below training/evaluation"
            )
    _assertions(evidence)
    return {"run_ids": run_ids, "environment_hash": canonical_hash, "stages": list(STAGES)}


def validate_p1(
    evidence: dict,
    *,
    expected_commit_sha: str,
    evidence_root: Path | None = None,
) -> dict:
    validate_commit_binding(evidence, expected_commit_sha)
    if evidence.get("passed") is not True:
        raise ValueError("passed must be true")
    summary = _mapping(evidence.get("first_screen_summary"), "first_screen_summary")
    for viewport in ("desktop", "narrow"):
        row = _mapping(summary.get(viewport), f"first_screen_summary.{viewport}")
        if row.get("status") != "passed" or row.get("columns") != 3:
            raise ValueError(f"first_screen_summary.{viewport} must pass with three columns")
    index = _mapping(evidence.get("evidence_index"), "evidence_index")
    for surface in ("browser", "api"):
        row = _mapping(index.get(surface), f"evidence_index.{surface}")
        if row.get("status") != "passed" or row.get("categories") != 5:
            raise ValueError(f"evidence_index.{surface} must pass with five categories")
    paths = _evidence_paths(evidence, evidence_root)
    _assertions(evidence)
    return {"viewports": ["desktop", "narrow"], "evidence_categories": 5, "paths": paths}


VALIDATORS = {
    "G-01": validate_g01,
    "G-02": validate_g02,
    "G-03": validate_g03,
    "G-04": validate_g04,
    "G-05": validate_g05,
    "NFR-05": validate_nfr05,
    "P1": validate_p1,
}


def _entry(
    status: str,
    commit_sha: str,
    evidence_paths: list[str],
    details: dict,
) -> dict:
    return {
        "status": status,
        "commit_sha": commit_sha,
        "evidence_paths": evidence_paths,
        "details": details,
    }


def _historical_note(gate: str, root: Path) -> str | None:
    if gate == "G-02":
        candidates = list((root / "g02").glob("full-run-*/result.json"))
        candidates.sort(
            key=lambda path: (
                int(match.group(1)) if (match := re.search(r"(\d+)$", path.parent.name)) else -1,
                path.parent.name,
            )
        )
        if candidates:
            path = candidates[-1]
            try:
                evidence = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return f"historical {path.name} is malformed and cannot bind the final commit"
            if not isinstance(evidence, dict):
                return f"historical {path.name} is malformed and cannot bind the final commit"
            if not evidence.get("commitSha"):
                return f"historical {path.parent.name}/result.json has no commitSha"
            return f"historical {path.parent.name}/result.json is stale for the final commit"
    if gate == "G-04" and (root / "legacy-consumers.json").is_file():
        return "legacy-consumers.json exists but is not a structured final G-04 result"
    if gate == "P1" and (root / "g02/task-12-fix-browser/result.json").is_file():
        return "historical Task 12 browser result has no final commitSha binding"
    return None


def evaluate_gate(gate: str, evidence_root: Path, commit_sha: str) -> dict:
    if gate not in GATE_PATHS:
        raise ValueError(f"unknown gate: {gate}")
    relative = GATE_PATHS[gate]
    path = evidence_root / relative
    if not path.is_file():
        reason = f"missing required evidence: {relative}"
        historical = _historical_note(gate, evidence_root)
        if historical:
            reason = f"{reason}; {historical}"
        return _entry("pending", commit_sha, [relative], {"reason": reason})
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _entry(
            "pending",
            commit_sha,
            [relative],
            {"reason": "malformed JSON; evidence cannot be validated"},
        )
    if not isinstance(evidence, dict):
        return _entry(
            "pending", commit_sha, [relative], {"reason": "evidence root must be a JSON object"}
        )
    try:
        validate_commit_binding(evidence, commit_sha)
    except ValueError as exc:
        return _entry("pending", commit_sha, [relative], {"reason": str(exc)})
    if evidence.get("passed") is False or evidence.get("status") == "failed":
        return _entry(
            "failed",
            commit_sha,
            [relative],
            {"reason": "current-commit evidence explicitly failed"},
        )
    if evidence.get("status") == "pending":
        return _entry(
            "pending", commit_sha, [relative], {"reason": "current-commit evidence is pending"}
        )
    if evidence.get("passed") is not True:
        return _entry(
            "pending",
            commit_sha,
            [relative],
            {"reason": "passed=true is missing; evidence is incomplete"},
        )
    try:
        kwargs = {"expected_commit_sha": commit_sha}
        if gate != "NFR-05":
            kwargs["evidence_root"] = evidence_root
        if gate == "G-01":
            kwargs["archive_result"] = run_archive_verifier()
        details = VALIDATORS[gate](evidence, **kwargs)
    except ValueError as exc:
        return _entry("pending", commit_sha, [relative], {"reason": str(exc)})
    return _entry("passed", commit_sha, [relative], details)


def _normalize_status(value: Any) -> str:
    if isinstance(value, str) and value in {"passed", "failed", "pending"}:
        return value
    return "pending"


def build_ledger(
    statuses: dict,
    *,
    commit_sha: str | None = None,
    evidence_root: str | Path = ".",
) -> dict:
    commit_sha = commit_sha or _head_commit()
    gates = {}
    for gate in GATE_PATHS:
        value = statuses.get(gate, "pending")
        if isinstance(value, dict):
            status = value.get("status", "pending")
            entry = {
                "status": _normalize_status(status),
                "commit_sha": value.get("commit_sha", commit_sha),
                "evidence_paths": value.get("evidence_paths", [GATE_PATHS[gate]]),
                "details": value.get("details", {}),
            }
        else:
            entry = _entry(
                _normalize_status(value),
                commit_sha,
                [GATE_PATHS[gate]],
                {"reason": "no validated evidence entry"},
            )
        gates[gate] = entry
    return {
        "schema_version": 1,
        "commit_sha": commit_sha,
        "evidence_root": Path(evidence_root).resolve().as_posix(),
        "generated_at": datetime.now(UTC).isoformat(),
        "v2_deliverable": all(entry["status"] == "passed" for entry in gates.values()),
        "gates": gates,
    }


def evaluate_all(evidence_root: Path, commit_sha: str) -> dict:
    statuses = {gate: evaluate_gate(gate, evidence_root, commit_sha) for gate in GATE_PATHS}
    return build_ledger(statuses, commit_sha=commit_sha, evidence_root=evidence_root)


def render_table(ledger: dict) -> str:
    rows = [("GATE", "STATUS", "REASON")]
    for gate, entry in ledger["gates"].items():
        reason = entry["details"].get("reason") or "evidence complete and bound to final commit"
        reason = " ".join(str(reason).split())
        if len(reason) > 120:
            reason = reason[:117] + "..."
        rows.append((gate, entry["status"], reason))
    widths = [max(len(row[index]) for row in rows) for index in range(3)]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    ]
    deliverable = str(ledger["v2_deliverable"]).lower()
    lines.append(f"v2_deliverable={deliverable} commit={ledger['commit_sha']}")
    return "\n".join(lines)


def _head_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or not COMMIT_RE.fullmatch(commit):
        raise ValueError("current Git HEAD is unavailable; supply --commit-sha")
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, default=Path("_bmad-output/acceptance/v2"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--commit-sha", default=None, help="full final Git SHA (default: HEAD)")
    args = parser.parse_args(argv)
    try:
        commit_sha = args.commit_sha or _head_commit()
        if not COMMIT_RE.fullmatch(commit_sha):
            raise ValueError("--commit-sha must be a full lowercase Git SHA")
        evidence_root = args.evidence_root.resolve()
        output = (args.output or evidence_root / "acceptance-ledger.json").resolve()
        if not output.is_relative_to(evidence_root):
            raise ValueError("--output must stay inside --evidence-root")
        ledger = evaluate_all(evidence_root, commit_sha)
        output.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(
            ledger, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        output.write_text(serialized + "\n", encoding="utf-8")
    except ValueError as exc:
        parser.error(str(exc))
    print(render_table(ledger))
    print(f"ledger={output.as_posix()}")
    return 0 if ledger["v2_deliverable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
