#!/usr/bin/env python3
"""Validate final-commit-bound v2 acceptance evidence without running live systems.

The only external checks are a read-only Git HEAD lookup, the public archive
hash verifier, and the repository's static legacy-consumer audit. No credentialed
workflow, database mutation, or evidence synthesis is performed. Missing, stale,
malformed, or incomplete evidence remains pending; only an explicit failure in
current-commit evidence is reported as failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.audit_legacy_consumers import audit_repository
    from scripts.measure_run_segments import (
        assert_same_environment,
        environment_hash,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/, not the repository, on sys.path.
    from audit_legacy_consumers import audit_repository
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


def _hash_bound_artifact(
    evidence_root: Path | None, descriptor: Any, field: str
) -> tuple[Path, bytes]:
    descriptor = _mapping(descriptor, field)
    relative = _text(descriptor.get("path"), f"{field}.path")
    expected_hash = _sha256(descriptor.get("sha256"), f"{field}.sha256")
    if evidence_root is None:
        raise ValueError(f"{field} requires an evidence root")
    root = evidence_root.resolve()
    lexical = root / relative
    candidate = lexical.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"{field}.path must stay inside the evidence root")
    try:
        mode = lexical.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"{field}.path is missing: {relative}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{field}.path must be a regular file")
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise ValueError(f"{field}.path is unreadable: {relative}") from exc
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError(f"{field}.sha256 does not match its bytes")
    return candidate, content


def _hash_bound_json_artifact(
    evidence_root: Path | None, descriptor: Any, field: str
) -> tuple[Path, dict]:
    path, content = _hash_bound_artifact(evidence_root, descriptor, field)
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{field} is malformed JSON") from exc
    return path, _mapping(payload, field)


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


def _validate_g03_case(payload: dict, name: str) -> None:
    field = f"matrix.{name}.artifact"
    if payload.get("case") != name:
        raise ValueError(f"{field}.case must equal {name}")
    if payload.get("passed") is not True:
        raise ValueError(f"{field}.passed must be true")
    _text(payload.get("run_id"), f"{field}.run_id")
    _text(payload.get("workflow_id"), f"{field}.workflow_id")

    def require(field_name: str, expected: Any) -> None:
        if payload.get(field_name) != expected:
            raise ValueError(f"{field}.{field_name} must be {expected!r}")

    if name in {"FAILED", "OOM", "repair"}:
        require("run_status", "failed")
        require("workflow_status", "failed")
    if name == "FAILED":
        _text(payload.get("error_code"), f"{field}.error_code")
    elif name == "TIMEOUT":
        require("run_status", "timeout")
        require("workflow_status", "failed")
        require("error_code", "WORKFLOW_TIMEOUT")
    elif name == "CANCELLED":
        require("run_status", "cancelled")
        require("workflow_status", "cancelled")
    elif name == "OOM":
        if isinstance(payload.get("exit_code"), bool) or payload.get("exit_code") != 137:
            raise ValueError(f"{field}.exit_code must be 137")
        if (
            isinstance(payload.get("replan_count"), bool)
            or payload.get("replan_count") != 2
        ):
            raise ValueError(f"{field}.replan_count must be 2")
        if payload.get("third_replan_refused") is not True:
            raise ValueError(f"{field}.third_replan_refused must be true")
        if "report" in payload:
            report = _mapping(payload["report"], f"{field}.report")
            attempt = _positive_int(report.get("execution_attempt"), f"{field}.execution_attempt")
            final = _positive_int(payload.get("final_attempt"), f"{field}.final_attempt")
            if attempt != final:
                raise ValueError(f"{field}.execution_attempt must equal final_attempt")
    elif name == "repair":
        if (
            isinstance(payload.get("repair_attempt"), bool)
            or payload.get("repair_attempt") != 3
        ):
            raise ValueError(f"{field}.repair_attempt must be 3")
        if payload.get("fourth_repair_refused") is not True:
            raise ValueError(f"{field}.fourth_repair_refused must be true")
    else:
        terminal = payload.get("terminal_status")
        if terminal not in {"failed", "timeout", "cancelled"}:
            raise ValueError(f"{field}.terminal_status must be a terminal failure")
        require("run_status_before_restart", terminal)
        require("run_status_after_restart", terminal)
        before = _text(
            payload.get("workflow_status_before_restart"),
            f"{field}.workflow_status_before_restart",
        )
        after = _text(
            payload.get("workflow_status_after_restart"),
            f"{field}.workflow_status_after_restart",
        )
        if before != after:
            raise ValueError(f"{field}.workflow_status_before_restart/after_restart must agree")
        available = payload.get("report_available")
        if not isinstance(available, bool):
            raise ValueError(f"{field}.report_available must be a boolean")
        if available:
            _positive_int(payload.get("report_version"), f"{field}.report_version")
            report_attempt = _positive_int(
                payload.get("report_execution_attempt"), f"{field}.report_execution_attempt"
            )
            final_attempt = _positive_int(payload.get("final_attempt"), f"{field}.final_attempt")
            if report_attempt != final_attempt:
                raise ValueError(f"{field}.report_execution_attempt must equal final_attempt")
        else:
            _text(payload.get("report_error_code"), f"{field}.report_error_code")


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
    paths = []
    for name in required:
        case = _mapping(matrix.get(name), f"matrix.{name}")
        if case.get("status") != "passed":
            raise ValueError(f"matrix.{name} must pass")
        path, payload = _hash_bound_json_artifact(
            evidence_root, case.get("artifact"), f"matrix.{name}.artifact"
        )
        _validate_g03_case(payload, name)
        paths.append(path.relative_to(evidence_root.resolve()).as_posix())
    _assertions(evidence)
    assertion_names = {assertion["name"] for assertion in evidence["assertions"]}
    if not set(required).issubset(assertion_names):
        raise ValueError("assertions must cover every negative/recovery case")
    return {"exit_code": 0, "matrix": list(required), "evidence_paths": paths}


def _string_rows(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return [_text(row, f"{field}[{index}]") for index, row in enumerate(value)]


def _mount_rows(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    rows = []
    for index, row in enumerate(value):
        if isinstance(row, str):
            rows.append(_text(row, f"{field}[{index}]"))
        else:
            rows.append(json.dumps(_mapping(row, f"{field}[{index}]"), sort_keys=True))
    return rows


def _assert_no_host_access(rows: list[str], field: str) -> None:
    for row in rows:
        normalized = row.lower().replace("\\", "/")
        if "docker.sock" in normalized or "/var/run/docker" in normalized:
            raise ValueError(f"{field} must not mount the Docker socket")


def _positive_log_size(value: Any, field: str) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive log-size limit")
    if isinstance(value, int):
        if value > 0:
            return
    elif isinstance(value, str):
        match = re.fullmatch(r"([1-9][0-9]*)([bkmg]?)b?", value.strip(), re.IGNORECASE)
        if match:
            return
    raise ValueError(f"{field} must be a positive log-size limit")


def _validate_container_inspect(payload: Any, field: str) -> None:
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError(f"{field} must be an object or one-element list")
        payload = payload[0]
    inspect = _mapping(payload, field)
    config = _mapping(inspect.get("Config"), f"{field}.Config")
    user = _text(config.get("User"), f"{field}.Config.User")
    if user.split(":", 1)[0].lower() in {"root", "0"}:
        raise ValueError(f"{field}.Config.User must be non-root")
    host = _mapping(inspect.get("HostConfig"), f"{field}.HostConfig")
    if host.get("Privileged") is not False:
        raise ValueError(f"{field}.HostConfig.Privileged must be false")
    if host.get("ReadonlyRootfs") is not True:
        raise ValueError(f"{field}.HostConfig.ReadonlyRootfs must be true")
    if host.get("NetworkMode") != "none":
        raise ValueError(f"{field}.HostConfig.NetworkMode must be none")
    cap_drop = _string_rows(host.get("CapDrop"), f"{field}.HostConfig.CapDrop")
    if "ALL" not in cap_drop:
        raise ValueError(f"{field}.HostConfig.CapDrop must contain ALL")
    options = _string_rows(host.get("SecurityOpt"), f"{field}.HostConfig.SecurityOpt")
    if "no-new-privileges:true" not in options:
        raise ValueError(f"{field}.HostConfig.SecurityOpt must contain no-new-privileges:true")
    mounts = _mount_rows(host.get("Mounts", []), f"{field}.HostConfig.Mounts")
    binds = _string_rows(host.get("Binds", []), f"{field}.HostConfig.Binds")
    _assert_no_host_access(mounts + binds, field)
    nano_cpus = host.get("NanoCpus")
    quota = host.get("CpuQuota")
    if not (
        (isinstance(nano_cpus, int) and not isinstance(nano_cpus, bool) and nano_cpus > 0)
        or (isinstance(quota, int) and not isinstance(quota, bool) and quota > 0)
    ):
        raise ValueError(f"{field}.HostConfig.NanoCpus/CpuQuota must specify a positive CPU limit")
    _positive_int(host.get("Memory"), f"{field}.HostConfig.Memory")
    _positive_int(host.get("PidsLimit"), f"{field}.HostConfig.PidsLimit")
    log_config = _mapping(host.get("LogConfig"), f"{field}.HostConfig.LogConfig")
    log_options = _mapping(log_config.get("Config"), f"{field}.HostConfig.LogConfig.Config")
    _positive_log_size(log_options.get("MaxSize"), f"{field}.HostConfig.LogConfig.Config.MaxSize")


def _validate_host_credentials(payload: dict, field: str) -> None:
    mounts = _mount_rows(payload.get("mounts"), f"{field}.mounts")
    binds = _string_rows(payload.get("binds"), f"{field}.binds")
    environment = _string_rows(payload.get("environment"), f"{field}.environment")
    _assert_no_host_access(mounts + binds, field)
    credential_markers = (".ssh", ".aws", ".docker", "credentials", "id_rsa", "/etc/shadow")
    for row in mounts + binds:
        if any(marker in row.lower() for marker in credential_markers):
            raise ValueError(f"{field} must not expose host credentials")
    credential_name = re.compile(
        r"(?:password|passwd|secret|token|credential|api[_-]?key|access[_-]?key|private[_-]?key)",
        re.IGNORECASE,
    )
    for row in environment:
        name = row.split("=", 1)[0]
        if credential_name.search(name):
            raise ValueError(f"{field}.environment must not contain credential-like names")


def _repository_migration_hashes(root: Path, directory: str) -> dict[str, str]:
    path = root / directory
    if not path.is_dir():
        raise ValueError(f"repository migration directory is missing: {directory}")
    hashes = {}
    for migration in sorted(path.iterdir()):
        if migration.is_symlink() or not migration.is_file():
            continue
        hashes[migration.name] = hashlib.sha256(migration.read_bytes()).hexdigest()
    return hashes


def _validate_legacy_audit(audit: dict, audit_hash: str) -> dict:
    current = _mapping(audit_repository(REPOSITORY_ROOT), "current legacy audit")
    if audit.get("ok") is not True or current.get("ok") is not True:
        raise ValueError("legacy_audit and current repository audit must pass")
    for field in ("blocking_consumers", "scan_errors"):
        if (
            audit.get(field) != []
            or current.get(field) != []
            or audit.get(field) != current.get(field)
        ):
            raise ValueError(f"legacy_audit {field} must be empty and match the current audit")
    recorded_targets = _mapping(audit.get("targets"), "legacy_audit.targets")
    current_targets = _mapping(current.get("targets"), "current legacy audit.targets")
    if set(recorded_targets) != set(current_targets):
        raise ValueError("legacy_audit targets do not match the current audit")
    for target, recorded in recorded_targets.items():
        recorded = _mapping(recorded, f"legacy_audit.targets.{target}")
        live = _mapping(current_targets[target], f"current legacy audit.targets.{target}")
        for field in ("status", "registrations", "live_routes"):
            if recorded.get(field) != live.get(field):
                raise ValueError(f"legacy_audit target {target} {field} is stale")
    migrations = _mapping(audit.get("migrations"), "legacy_audit.migrations")
    current_migrations = _mapping(current.get("migrations"), "current legacy audit.migrations")
    if migrations != current_migrations:
        raise ValueError("legacy_audit migrations do not match the current audit")
    if migrations.get("ok") is not True:
        raise ValueError("legacy_audit migrations must pass")
    for field in ("errors", "missing", "extra", "mismatched"):
        if migrations.get(field) != []:
            raise ValueError(f"legacy_audit migrations.{field} must be empty")
    canonical = _mapping(migrations.get("canonical_sha256"), "migrations.canonical_sha256")
    deploy = _mapping(migrations.get("deploy_sha256"), "migrations.deploy_sha256")
    if len(canonical) != 16 or len(deploy) != 16 or canonical != deploy:
        raise ValueError("legacy_audit must bind 16 identical canonical/deploy migrations")
    repository_maps = (
        _repository_migration_hashes(REPOSITORY_ROOT, "infra/supabase/migrations"),
        _repository_migration_hashes(REPOSITORY_ROOT, "deploy/server/supabase/migrations"),
    )
    for recorded, repository in ((canonical, repository_maps[0]), (deploy, repository_maps[1])):
        for name, digest in recorded.items():
            _text(name, "migration name")
            _sha256(digest, f"migration {name} sha256")
        if recorded != repository:
            raise ValueError("legacy_audit migration hashes do not match repository files")
    return {
        "canonical_migrations": len(canonical),
        "deploy_migrations": len(deploy),
        "legacy_audit_sha256": audit_hash,
    }


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
    _, inspect = _hash_bound_json_artifact(
        evidence_root, security.get("inspect"), "container_security.inspect"
    )
    _validate_container_inspect(inspect, "container_security.inspect")
    _, credentials = _hash_bound_json_artifact(
        evidence_root, security.get("host_credentials"), "container_security.host_credentials"
    )
    _validate_host_credentials(credentials, "container_security.host_credentials")
    _positive_int(security.get("disk_limit_bytes"), "container_security.disk_limit_bytes")
    timeout = _finite_nonnegative(
        security.get("run_timeout_seconds"), "container_security.run_timeout_seconds"
    )
    if timeout <= 0:
        raise ValueError("container_security.run_timeout_seconds must be positive")

    ownership = evidence.get("ownership_matrix")
    if not isinstance(ownership, list) or len(ownership) != 2:
        raise ValueError("ownership_matrix must contain exactly two accounts")
    accounts = set()
    roles = set()
    shared = {}
    responses = {}
    for index, row in enumerate(ownership):
        field = f"ownership_matrix[{index}]"
        row = _mapping(row, field)
        accounts.add(_text(row.get("account"), f"{field}.account"))
        roles.add(_text(row.get("role"), f"{field}.role"))
        identity = {
            key: _text(row.get(key), f"{field}.{key}")
            for key in ("run_id", "endpoint", "method")
        }
        if shared and identity != shared:
            differing = next(key for key in identity if identity[key] != shared[key])
            raise ValueError(f"{field}.{differing} must match the other ownership row")
        shared = identity
        _, response = _hash_bound_json_artifact(
            evidence_root, row.get("response"), f"{field}.response"
        )
        responses[row["role"]] = response
    if len(accounts) != 2 or roles != {"owner", "non_owner"}:
        raise ValueError("ownership_matrix must contain distinct owner and non_owner accounts")
    if responses["owner"].get("status") != 200:
        raise ValueError("owner response status must be 200")
    non_owner = responses["non_owner"]
    if non_owner.get("status") != 404 or non_owner.get("code") != "RUN_NOT_FOUND":
        raise ValueError("non_owner response must be 404 RUN_NOT_FOUND")

    audit_path, audit = _hash_bound_json_artifact(
        evidence_root, evidence.get("legacy_audit"), "legacy_audit"
    )
    audit_hash = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    details = _validate_legacy_audit(audit, audit_hash)
    details["ownership_accounts"] = sorted(accounts)
    _assertions(evidence)
    details["legacy_audit_path"] = audit_path.relative_to(evidence_root.resolve()).as_posix()
    return details


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


def _commit_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not COMMIT_RE.fullmatch(value):
        raise ValueError(f"{field} must be a full lowercase Git commit SHA")
    return value


def build_ledger(
    statuses: dict,
    *,
    commit_sha: str | None = None,
    evidence_root: str | Path = ".",
) -> dict:
    commit_sha = _commit_sha(commit_sha or _head_commit(), "commit_sha")
    gates = {}
    for gate in GATE_PATHS:
        value = statuses.get(gate, "pending")
        if isinstance(value, dict):
            entry = {
                "status": _normalize_status(value.get("status", "pending")),
                "commit_sha": commit_sha,
                "evidence_paths": value.get("evidence_paths", [GATE_PATHS[gate]]),
                "details": dict(value.get("details", {})),
            }
            gate_commit = value.get("commit_sha")
            if not isinstance(gate_commit, str) or not COMMIT_RE.fullmatch(gate_commit):
                entry["status"] = "pending"
                entry["details"]["reason"] = "missing or malformed per-gate commit_sha binding"
            elif gate_commit != commit_sha:
                entry["status"] = "pending"
                entry["details"]["reason"] = (
                    f"stale per-gate commit_sha: evidence={gate_commit}, expected={commit_sha}"
                )
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
        "v2_deliverable": all(
            entry["status"] == "passed" and entry["commit_sha"] == commit_sha
            for entry in gates.values()
        ),
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


def _ledger_output_path(evidence_root: Path, requested: Path | None) -> Path:
    try:
        root = evidence_root.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("--evidence-root cannot be resolved") from exc
    if not root.is_dir():
        raise ValueError("--evidence-root must already exist and be a directory")
    canonical = root / "acceptance-ledger.json"
    output = requested if requested is not None else canonical
    if ".." in output.parts:
        raise ValueError("--output must not contain path traversal")
    try:
        if output.is_symlink() or canonical.is_symlink():
            raise ValueError("--output must not be a symlink destination")
        resolved = output.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("--output cannot be resolved safely") from exc
    if resolved != canonical:
        raise ValueError("--output must be exactly <evidence-root>/acceptance-ledger.json")
    if canonical.exists() and not canonical.is_file():
        raise ValueError("--output must be a regular ledger file")
    return canonical


def _write_ledger_atomic(output: Path, ledger: dict) -> None:
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise ValueError("--output must be a regular ledger file, not a symlink")
    serialized = json.dumps(
        ledger, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".acceptance-ledger-", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, default=Path("_bmad-output/acceptance/v2"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--commit-sha", default=None, help="full final Git SHA (default: HEAD)")
    args = parser.parse_args(argv)
    try:
        commit_sha = _commit_sha(args.commit_sha or _head_commit(), "--commit-sha")
        evidence_root = args.evidence_root.resolve()
        output = _ledger_output_path(evidence_root, args.output)
        ledger = evaluate_all(evidence_root, commit_sha)
        _write_ledger_atomic(output, ledger)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(render_table(ledger))
    print(f"ledger={output.as_posix()}")
    return 0 if ledger["v2_deliverable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
