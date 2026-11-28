"""Contract tests for the read-only, final-commit-bound v2 acceptance ledger."""

import copy
import hashlib
import importlib.util
import json
import math
import struct
import sys
import zlib
from pathlib import Path

import pytest

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
GATES = ("G-01", "G-02", "G-03", "G-04", "G-05", "NFR-05", "P1")
GATE_PATHS = {
    "G-01": "g01/result.json",
    "G-02": "g02/result.json",
    "G-03": "g03/result.json",
    "G-04": "g04/result.json",
    "G-05": "g05/result.json",
    "NFR-05": "nfr05/result.json",
    "P1": "p1/result.json",
}


class MissingVerifier:
    def __getattr__(self, name):
        raise AssertionError(f"v2 acceptance verifier is unavailable: {name}")


@pytest.fixture
def verifier():
    path = Path(__file__).with_name("verify_v2_acceptance.py")
    if not path.exists():
        return MissingVerifier()
    spec = importlib.util.spec_from_file_location("task15_verify_v2_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(root: Path, relative: str, payload: dict) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def passed_assertions(*names):
    return [{"name": name, "status": "passed"} for name in names]


def g02_evidence(root: Path) -> dict:
    markdown = b"# bound report\n"
    (root / "g02").mkdir(parents=True, exist_ok=True)
    (root / "g02/report.md").write_bytes(markdown)
    references = {
        "run_id": "run-1",
        "workflow_id": "workflow-1",
        "experiment_id": "experiment-1",
        "plan_id": "plan-1",
        "plan_version": 2,
        "code_version": 3,
        "code_hash": "c" * 64,
        "execution_attempt": 2,
        "generated_file_ids": ["code-1"],
    }
    return {
        "commitSha": COMMIT,
        "passed": True,
        "status": "succeeded",
        "report_path": "g02/report.md",
        "markdown_sha256": hashlib.sha256(markdown).hexdigest(),
        "qwen": {"provider": "dashscope", "model": "qwen-real", "real_request": True},
        "environment_hash": "e" * 64,
        "environment": {"image_digest": "sha256:image"},
        "assertions": passed_assertions("real Qwen closed loop", "same Run report downloaded"),
        "report": {
            "id": "report-1",
            "run_id": "run-1",
            "version": 1,
            "execution_attempt": 2,
            "report_json": {"references": references},
        },
        **references,
        "code_id": "code-1",
        "report_id": "report-1",
        "report_version": 1,
        "final_attempt": 2,
    }


def png_bytes(width: int = 320, height: int = 200) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    # Deterministic noisy RGB data keeps this valid PNG comfortably above 1024 bytes.
    raw = b"".join(
        bytes([0]) + bytes((index * 37 + row * 17) % 256 for index in range(width * 3))
        for row in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def g01_evidence(root: Path) -> dict:
    har = artifact(
        root,
        "g01/public.har",
        {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://archive.example/examples/lenet/",
                            "headers": [],
                        },
                        "response": {
                            "status": 200,
                            "content": {"mimeType": "text/html; charset=utf-8"},
                        },
                    }
                ]
            }
        },
    )
    screenshot = artifact(root, "g01/public.png", png_bytes())
    return {
        "commitSha": COMMIT,
        "passed": True,
        "archive": {
            "run": "01993dd36ff17102",
            "model_sha256": "01993dd36ff1710272c3c50e16a0ffb10dc2015c08e8080b22b7e480b2c83b47",
            "report_sha256": "5a7fcba954b496bd5e87483f7cad81b7155f048a111bcd81d3e769eca9d7349d",
        },
        "visitor": {"authenticated": False, "auth_requests": 0, "session_requests": 0},
        "har": har,
        "screenshot": screenshot,
        "evidence_paths": ["g01/public.png", "g01/public.har"],
        "assertions": passed_assertions("public archive", "visitor without auth"),
    }


def archive_result(verifier) -> dict:
    return {
        "run": verifier.ARCHIVE_RUN,
        "verified_files": 9,
        "model_sha256": verifier.ARCHIVE_MODEL_SHA256,
        "report_sha256": verifier.ARCHIVE_REPORT_SHA256,
    }


G03_CASES = ("FAILED", "TIMEOUT", "CANCELLED", "OOM", "repair", "restart")


def artifact(root: Path, relative: str, payload: object) -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def g03_payload(name: str) -> dict:
    payload = {
        "case": name,
        "passed": True,
        "run_id": f"run-{name}",
        "workflow_id": f"workflow-{name}",
    }
    if name in {"FAILED", "OOM", "repair"}:
        payload.update(run_status="failed", workflow_status="failed")
    if name == "FAILED":
        payload["error_code"] = "TRAINING_FAILED"
    elif name == "TIMEOUT":
        payload.update(
            run_status="timeout", workflow_status="failed", error_code="WORKFLOW_TIMEOUT"
        )
    elif name == "CANCELLED":
        payload.update(run_status="cancelled", workflow_status="cancelled")
    elif name == "OOM":
        payload.update(
            exit_code=137,
            replan_count=2,
            third_replan_refused=True,
            final_attempt=3,
            report={"execution_attempt": 3},
        )
    elif name == "repair":
        payload.update(repair_attempt=3, fourth_repair_refused=True)
    else:
        payload.update(
            terminal_status="failed",
            run_status_before_restart="failed",
            run_status_after_restart="failed",
            workflow_status_before_restart="failed",
            workflow_status_after_restart="failed",
            report_available=True,
            report_version=2,
            report_execution_attempt=3,
            final_attempt=3,
        )
    return payload


def g03_evidence(root: Path) -> dict:
    return {
        "commitSha": COMMIT,
        "passed": True,
        "exit_code": 0,
        "matrix": {
            name: {
                "status": "passed",
                "artifact": artifact(root, f"g03/{name}.json", g03_payload(name)),
            }
            for name in G03_CASES
        },
        "assertions": passed_assertions(*G03_CASES),
    }


def g04_evidence(root: Path) -> dict:
    hashes = {
        f"migration-{index}.sql": hashlib.sha256(f"migration-{index}.sql".encode()).hexdigest()
        for index in range(16)
    }
    audit = {
        "ok": True,
        "blocking_consumers": [],
        "scan_errors": [],
        "targets": {
            "legacy.worker": {
                "status": "retired-by-nonregistration",
                "registrations": [],
                "live_routes": [],
            }
        },
        "migrations": {
            "ok": True,
            "canonical_sha256": hashes,
            "deploy_sha256": hashes,
            "errors": [],
            "missing": [],
            "mismatched": [],
            "extra": [],
        },
    }
    inspect = {
        "Config": {"User": "1000:1000"},
        "HostConfig": {
            "Privileged": False,
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Mounts": [],
            "Binds": [],
            "NanoCpus": 1_000_000_000,
            "Memory": 512 * 1024 * 1024,
            "PidsLimit": 256,
            "LogConfig": {"Config": {"MaxSize": "10m"}},
        },
    }
    credentials = {"mounts": [], "binds": [], "environment": []}
    responses = [
        ("owner", "alice", {"status": 200}),
        ("non_owner", "bob", {"status": 404, "code": "RUN_NOT_FOUND"}),
    ]
    ownership = []
    for role, account, response in responses:
        ownership.append(
            {
                "account": account,
                "role": role,
                "run_id": "run-1",
                "endpoint": "/v1/runs/run-1",
                "method": "GET",
                "response": artifact(root, f"g04/{role}-response.json", response),
            }
        )
    return {
        "commitSha": COMMIT,
        "passed": True,
        "container_security": {
            "inspect": artifact(root, "g04/inspect.json", inspect),
            "host_credentials": artifact(root, "g04/host-credentials.json", credentials),
            "disk_limit_bytes": 256 * 1024 * 1024,
            "run_timeout_seconds": 600,
        },
        "ownership_matrix": ownership,
        "legacy_audit": artifact(root, "g04/legacy-consumers.json", audit),
        "assertions": passed_assertions(
            "container security", "two-account ownership", "legacy audit"
        ),
    }


def g04_current_audit(root: Path, hashes: dict) -> dict:
    for directory in ("infra/supabase/migrations", "deploy/server/supabase/migrations"):
        for name in hashes:
            path = root / directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
    return {
        "ok": True,
        "blocking_consumers": [],
        "scan_errors": [],
        "targets": {
            "legacy.worker": {
                "status": "retired-by-nonregistration",
                "registrations": [],
                "live_routes": [],
            }
        },
        "migrations": {
            "ok": True,
            "canonical_sha256": hashes,
            "deploy_sha256": hashes,
            "errors": [],
            "missing": [],
            "mismatched": [],
            "extra": [],
        },
    }


def g05_evidence(root: Path) -> dict:
    markdown = b"# Real report\n\nDownloaded report bytes.\n"
    markdown_hash = hashlib.sha256(markdown).hexdigest()
    browser = {
        "commitSha": COMMIT,
        "passed": True,
        "browser": "chromium",
        "upload": {"paperId": "paper-1", "workflowId": "workflow-1"},
        "planApproval": {"experimentId": "experiment-1", "planId": "plan-1", "planVersion": 2},
        "execution": {
            "runId": "run-1",
            "workflowId": "workflow-1",
            "experimentId": "experiment-1",
            "planId": "plan-1",
            "planVersion": 2,
            "status": "succeeded",
            "attempt": 3,
            "codeHash": "c" * 64,
        },
        "reportDownload": {
            "reportId": "report-1",
            "version": 4,
            "runId": "run-1",
            "markdownHash": markdown_hash,
            "finalAttempt": 3,
        },
    }
    commands = []
    for name in ("check", "check_integration", "browser_smoke"):
        payload = {"name": name, "exit_code": 0, "output": f"captured {name} output\n"}
        commands.append(
            {
                "name": name,
                "exit_code": 0,
                "output": artifact(root, f"g05/{name}.json", payload),
            }
        )
    return {
        "commitSha": COMMIT,
        "passed": True,
        "commands": commands,
        "browser_result": artifact(root, "g05/browser.json", browser),
        "report_download": artifact(root, "g05/report.md", markdown),
        "assertions": passed_assertions("offline gate", "integration", "real browser smoke"),
    }


def environment():
    return {
        "image_digest": "sha256:image",
        "dataset_fingerprint": "dataset-v1",
        "dependency_lock_hash": "deps-v1",
        "plan_hash": "plan-v1",
        "code_hash": "code-v1",
        "seed": 42,
        "resource_budget": {
            "max_seconds": 600,
            "memory_mb": 512,
            "cpu_count": 1.0,
            "pids_limit": 256,
            "disk_mb": 256,
            "device": "cpu",
        },
    }


def nfr05_evidence(verifier) -> dict:
    runs = [
        {
            "run_id": f"run-{index}",
            "experiment_id": "experiment-1",
            "plan_id": "plan-1",
            "plan_version": 2,
            "code_version": 3,
            "attempt": 1,
            "status": "succeeded",
            "queue_seconds": float(index),
            "container_lifecycle_seconds": 6.0,
            "training_evaluation_seconds": 2.0,
            "report_generation_seconds": 0.5,
            "environment": environment(),
            "report": {
                "id": f"report-{index}",
                "run_id": f"run-{index}",
                "version": 1,
                "execution_attempt": 1,
                "report_json": {
                    "references": {
                        "run_id": f"run-{index}",
                        "experiment_id": "experiment-1",
                        "plan_id": "plan-1",
                        "plan_version": 2,
                        "code_version": 3,
                        "code_hash": "code-v1",
                        "execution_attempt": 1,
                    }
                },
            },
        }
        for index in range(3)
    ]
    return {
        "commitSha": COMMIT,
        "passed": True,
        "runs": runs,
        "summary": {"environment_hash": verifier.environment_hash(environment())},
        "assertions": passed_assertions("three fixed-environment Runs"),
    }


P1_CATEGORIES = ["paper", "plan", "code", "execution", "review_report"]


def p1_evidence(root: Path) -> dict:
    summary = {
        viewport: {
            "status": "passed",
            "columns": 3,
            "run_id": "run-1",
            "paper": "Paper summary text",
            "plan": "Plan summary text",
            "actual": "Actual result text",
            "screenshot": artifact(root, f"p1/{viewport}.png", png_bytes()),
        }
        for viewport in ("desktop", "narrow")
    }
    browser = {
        "status": "passed",
        "run_id": "run-1",
        "categories": P1_CATEGORIES,
        "screenshot": artifact(root, "p1/browser.png", png_bytes()),
    }
    api = {
        "run_id": "run-1",
        "categories": P1_CATEGORIES,
        "items": [
            {
                "id": f"item-{category}",
                "category": category,
                "target_type": "artifact",
                "target_id": f"target-{category}",
                "locator": {"page": "evidence", "selector": f"#{category}"},
            }
            for category in P1_CATEGORIES
        ],
    }
    evidence_index = {
        "browser": browser,
        "api": {
            "status": "passed",
            "run_id": "run-1",
            "categories": P1_CATEGORIES,
            "result": artifact(root, "p1/api.json", api),
        },
    }
    return {
        "commitSha": COMMIT,
        "passed": True,
        "first_screen_summary": summary,
        "evidence_index": evidence_index,
        "evidence_paths": [
            f"p1/{name}" for name in ("desktop.png", "narrow.png", "browser.png", "api.json")
        ],
        "assertions": passed_assertions("first-screen summary", "single Run evidence index"),
    }


def test_static_archive_alone_does_not_make_v2_deliverable(verifier):
    ledger = verifier.build_ledger({"G-01": "passed"})
    assert ledger["v2_deliverable"] is False
    assert ledger["gates"]["G-02"]["status"] == "pending"


@pytest.mark.parametrize("missing", ["NFR-05", "P1"])
def test_deliverable_requires_nfr05_and_p1(verifier, missing):
    statuses = {gate: "passed" for gate in GATES}
    statuses[missing] = "pending"
    assert verifier.build_ledger(statuses)["v2_deliverable"] is False


def test_deliverable_requires_every_gate_to_pass(verifier):
    statuses = {gate: "passed" for gate in GATES}
    assert verifier.build_ledger(statuses)["v2_deliverable"] is True
    statuses["G-04"] = "failed"
    assert verifier.build_ledger(statuses)["v2_deliverable"] is False


@pytest.mark.parametrize("status", ["pending", "failed", "unknown", None])
def test_nonpassed_status_never_becomes_deliverable(verifier, status):
    statuses = {gate: "passed" for gate in GATES}
    statuses["G-05"] = status
    assert verifier.build_ledger(statuses)["v2_deliverable"] is False


def test_missing_run_report_hash_cannot_pass_g02(verifier, tmp_path):
    evidence = {"run_id": "r", "report_id": "p", "markdown_sha256": None}
    with pytest.raises(ValueError, match="markdown_sha256"):
        verifier.validate_g02(evidence)


@pytest.mark.parametrize("binding", [None, OTHER_COMMIT, "not-a-sha", COMMIT.upper()])
def test_g02_requires_exact_final_commit_binding(verifier, tmp_path, binding):
    evidence = g02_evidence(tmp_path)
    if binding is None:
        evidence.pop("commitSha")
    else:
        evidence["commitSha"] = binding
    with pytest.raises(ValueError, match="commitSha"):
        verifier.validate_g02(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g02_rejects_report_bytes_hash_mismatch(verifier, tmp_path):
    evidence = g02_evidence(tmp_path)
    (tmp_path / "g02/report.md").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="markdown_sha256"):
        verifier.validate_g02(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g02_malformed_report_payload_is_a_validation_error(verifier, tmp_path):
    evidence = g02_evidence(tmp_path)
    evidence["report"]["report_json"] = []
    with pytest.raises(ValueError, match="report_json"):
        verifier.validate_g02(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "other-run"),
        ("workflow_id", "other-workflow"),
        ("experiment_id", "other-experiment"),
        ("plan_id", "other-plan"),
        ("plan_version", 99),
        ("code_id", "other-code"),
        ("code_version", 99),
        ("code_hash", "d" * 64),
        ("report_id", "other-report"),
        ("report_version", 99),
        ("execution_attempt", 99),
        ("final_attempt", 99),
    ],
)
def test_g02_rejects_inconsistent_report_identities(verifier, tmp_path, field, value):
    evidence = g02_evidence(tmp_path)
    evidence[field] = value
    with pytest.raises(ValueError, match=field):
        verifier.validate_g02(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "other-report"), ("run_id", "other-run")],
)
def test_g02_rejects_inconsistent_report_envelope(verifier, tmp_path, field, value):
    evidence = g02_evidence(tmp_path)
    evidence["report"][field] = value
    with pytest.raises(ValueError, match=field):
        verifier.validate_g02(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g02_requires_code_id_in_report_generated_files(verifier, tmp_path):
    evidence = g02_evidence(tmp_path)
    evidence["report"]["report_json"]["references"]["generated_file_ids"] = []
    with pytest.raises(ValueError, match="generated_file_ids"):
        verifier.validate_g02(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g02_requires_real_qwen_and_environment_record(verifier, tmp_path):
    evidence = g02_evidence(tmp_path)
    evidence["qwen"]["real_request"] = False
    with pytest.raises(ValueError, match="Qwen"):
        verifier.validate_g02(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_report_path_cannot_escape_evidence_root(verifier, tmp_path):
    evidence = g02_evidence(tmp_path)
    outside = tmp_path.parent / "outside-report.md"
    outside.write_bytes(b"# outside\n")
    evidence["report_path"] = "../outside-report.md"
    evidence["markdown_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="report_path"):
        verifier.validate_g02(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_three_timing_runs_are_required(verifier):
    sample = nfr05_evidence(verifier)
    with pytest.raises(ValueError, match="exactly 3"):
        verifier.validate_nfr05({"runs": [sample, sample]})


def test_four_timing_runs_are_rejected(verifier):
    evidence = nfr05_evidence(verifier)
    evidence["runs"].append(copy.deepcopy(evidence["runs"][0]))
    with pytest.raises(ValueError, match="exactly 3"):
        verifier.validate_nfr05(evidence)


def test_nfr05_requires_three_distinct_successful_runs(verifier):
    evidence = nfr05_evidence(verifier)
    evidence["runs"][1]["run_id"] = evidence["runs"][0]["run_id"]
    with pytest.raises(ValueError, match="distinct"):
        verifier.validate_nfr05(evidence)
    evidence = nfr05_evidence(verifier)
    evidence["runs"][0]["status"] = "failed"
    with pytest.raises(ValueError, match="succeeded"):
        verifier.validate_nfr05(evidence)


def test_nfr05_requires_one_canonical_environment(verifier):
    evidence = nfr05_evidence(verifier)
    evidence["runs"][1]["environment"]["seed"] = 43
    with pytest.raises(ValueError, match="environment"):
        verifier.validate_nfr05(evidence)


def test_nfr05_summary_environment_hash_must_match_canonical_bytes(verifier):
    evidence = nfr05_evidence(verifier)
    evidence["summary"]["environment_hash"] = "f" * 64
    with pytest.raises(ValueError, match="environment_hash"):
        verifier.validate_nfr05(evidence)


@pytest.mark.parametrize(
    "value",
    [-0.1, math.nan, math.inf, -math.inf, True, None, "1", 10**1000],
)
@pytest.mark.parametrize(
    "stage",
    [
        "queue_seconds",
        "container_lifecycle_seconds",
        "training_evaluation_seconds",
        "report_generation_seconds",
    ],
)
def test_nfr05_rejects_invalid_stage_values(verifier, stage, value):
    evidence = nfr05_evidence(verifier)
    evidence["runs"][0][stage] = value
    with pytest.raises(ValueError, match=stage):
        verifier.validate_nfr05(evidence)


def test_nfr05_accepts_zero_for_every_stage(verifier):
    evidence = nfr05_evidence(verifier)
    for run in evidence["runs"]:
        for stage in (
            "queue_seconds",
            "container_lifecycle_seconds",
            "training_evaluation_seconds",
            "report_generation_seconds",
        ):
            run[stage] = 0
    assert verifier.validate_nfr05(evidence)["run_ids"] == ["run-0", "run-1", "run-2"]


def test_nfr05_requires_exact_commit_binding(verifier):
    evidence = nfr05_evidence(verifier)
    evidence["commitSha"] = OTHER_COMMIT
    with pytest.raises(ValueError, match="commitSha"):
        verifier.validate_nfr05(evidence, expected_commit_sha=COMMIT)


def test_nfr05_rejects_report_final_attempt_mismatch(verifier):
    evidence = nfr05_evidence(verifier)
    evidence["runs"][0]["report"]["execution_attempt"] = 2
    with pytest.raises(ValueError, match="execution_attempt"):
        verifier.validate_nfr05(evidence)


@pytest.mark.parametrize(
    "field",
    ["experiment_id", "plan_id", "plan_version", "code_version", "attempt"],
)
def test_nfr05_requires_complete_run_identity(verifier, field):
    evidence = nfr05_evidence(verifier)
    evidence["runs"][0].pop(field)
    with pytest.raises(ValueError, match=field):
        verifier.validate_nfr05(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", None), ("run_id", "other-run"), ("version", 0)],
)
def test_nfr05_rejects_invalid_report_envelope(verifier, field, value):
    evidence = nfr05_evidence(verifier)
    evidence["runs"][0]["report"][field] = value
    with pytest.raises(ValueError, match=field):
        verifier.validate_nfr05(evidence)


def test_g01_requires_bound_visitor_and_archive_identity(verifier, tmp_path):
    evidence = g01_evidence(tmp_path)
    evidence["visitor"]["auth_requests"] = 1
    with pytest.raises(ValueError, match="auth"):
        verifier.validate_g01(
            evidence,
            expected_commit_sha=COMMIT,
            archive_result=archive_result(verifier),
            evidence_root=tmp_path,
        )


def test_g01_rejects_wrong_public_model_identity(verifier, tmp_path):
    evidence = g01_evidence(tmp_path)
    evidence["archive"]["model_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="model_sha256"):
        verifier.validate_g01(evidence, expected_commit_sha=COMMIT)


def test_public_archive_verifier_runs_without_starting_artifact_cli(verifier):
    result = verifier.run_archive_verifier()
    assert result["run"] == "01993dd36ff17102"
    assert result["verified_files"] == 9
    assert result["model_sha256"] == verifier.ARCHIVE_MODEL_SHA256
    assert result["report_sha256"] == verifier.ARCHIVE_REPORT_SHA256


def test_g03_requires_complete_passing_negative_matrix(verifier, tmp_path):
    evidence = g03_evidence(tmp_path)
    evidence["matrix"]["OOM"]["status"] = "failed"
    with pytest.raises(ValueError, match="OOM"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g03_dummy_files_cannot_pass(verifier, tmp_path):
    evidence = g03_evidence(tmp_path)
    for name in G03_CASES:
        (tmp_path / f"g03/{name}.json").write_bytes(b"evidence")
    with pytest.raises(ValueError, match="sha256"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("name", G03_CASES)
def test_g03_requires_hash_bound_descriptor(verifier, tmp_path, name):
    evidence = g03_evidence(tmp_path)
    evidence["matrix"][name]["artifact"] = evidence["matrix"][name].pop("evidence_path", name)
    with pytest.raises(ValueError, match="artifact"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("name", G03_CASES)
def test_g03_requires_case_identity_and_run_identifiers(verifier, tmp_path, name):
    for field, value in (("case", "unrelated"), ("run_id", ""), ("workflow_id", "")):
        evidence = g03_evidence(tmp_path)
        payload = g03_payload(name)
        payload[field] = value
        evidence["matrix"][name]["artifact"] = artifact(tmp_path, f"g03/{name}.json", payload)
        with pytest.raises(ValueError, match=field):
            verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("name", G03_CASES)
def test_g03_status_only_artifact_cannot_pass(verifier, tmp_path, name):
    evidence = g03_evidence(tmp_path)
    evidence["matrix"][name]["artifact"] = artifact(
        tmp_path, f"g03/{name}.json", {"case": name, "passed": True}
    )
    with pytest.raises(ValueError, match="run_id|workflow_id"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("name", "field", "value"),
    [
        ("FAILED", "run_status", "succeeded"),
        ("FAILED", "workflow_status", "succeeded"),
        ("FAILED", "error_code", ""),
        ("TIMEOUT", "run_status", "failed"),
        ("TIMEOUT", "workflow_status", "timeout"),
        ("TIMEOUT", "error_code", "OTHER"),
        ("CANCELLED", "run_status", "failed"),
        ("CANCELLED", "workflow_status", "failed"),
        ("OOM", "exit_code", 1),
        ("OOM", "replan_count", 3),
        ("OOM", "third_replan_refused", False),
        ("repair", "repair_attempt", 2),
        ("repair", "fourth_repair_refused", False),
    ],
)
def test_g03_rejects_incomplete_case_semantics(verifier, tmp_path, name, field, value):
    evidence = g03_evidence(tmp_path)
    payload = g03_payload(name)
    payload[field] = value
    evidence["matrix"][name]["artifact"] = artifact(tmp_path, f"g03/{name}.json", payload)
    with pytest.raises(ValueError, match=field):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    "field",
    ["replan_count", "third_replan_refused", "repair_attempt", "fourth_repair_refused"],
)
def test_g03_rejects_missing_repair_and_replan_limits(verifier, tmp_path, field):
    name = "OOM" if field in {"replan_count", "third_replan_refused"} else "repair"
    evidence = g03_evidence(tmp_path)
    payload = g03_payload(name)
    payload.pop(field)
    evidence["matrix"][name]["artifact"] = artifact(tmp_path, f"g03/{name}.json", payload)
    with pytest.raises(ValueError, match=field):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g03_oom_report_must_reference_final_attempt(verifier, tmp_path):
    evidence = g03_evidence(tmp_path)
    payload = g03_payload("OOM")
    payload["report"]["execution_attempt"] = 2
    evidence["matrix"]["OOM"]["artifact"] = artifact(tmp_path, "g03/OOM.json", payload)
    with pytest.raises(ValueError, match="execution_attempt"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("terminal_status", "succeeded"),
        ("run_status_before_restart", "timeout"),
        ("run_status_after_restart", "cancelled"),
        ("workflow_status_before_restart", "cancelled"),
        ("report_available", "yes"),
        ("report_version", 0),
        ("report_execution_attempt", 2),
    ],
)
def test_g03_rejects_invalid_restart_behavior(verifier, tmp_path, field, value):
    evidence = g03_evidence(tmp_path)
    payload = g03_payload("restart")
    payload[field] = value
    evidence["matrix"]["restart"]["artifact"] = artifact(tmp_path, "g03/restart.json", payload)
    with pytest.raises(ValueError, match=field):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g03_unavailable_report_requires_stable_error(verifier, tmp_path):
    evidence = g03_evidence(tmp_path)
    payload = g03_payload("restart")
    payload.update(report_available=False, report_error_code="")
    evidence["matrix"]["restart"]["artifact"] = artifact(tmp_path, "g03/restart.json", payload)
    with pytest.raises(ValueError, match="report_error_code"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g03_assertions_must_cover_every_case(verifier, tmp_path):
    evidence = g03_evidence(tmp_path)
    evidence["assertions"] = passed_assertions("FAILED")
    with pytest.raises(ValueError, match="assertions"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def g04_fixture(verifier, tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(verifier, "REPOSITORY_ROOT", repository)
    evidence = g04_evidence(tmp_path)
    ref = evidence["legacy_audit"]
    recorded = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    hashes = recorded["migrations"]["canonical_sha256"]
    current = g04_current_audit(verifier.REPOSITORY_ROOT, hashes)
    monkeypatch.setattr(verifier, "audit_repository", lambda root: copy.deepcopy(current))
    return evidence, recorded, current


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b'"string"'])
def test_g03_artifact_json_must_be_an_object(verifier, tmp_path, payload):
    evidence = g03_evidence(tmp_path)
    evidence["matrix"]["FAILED"]["artifact"] = artifact(tmp_path, "g03/bad.json", payload)
    with pytest.raises(ValueError, match="FAILED"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g03_artifact_cannot_escape_evidence_root(verifier, tmp_path):
    evidence = g03_evidence(tmp_path)
    outside = tmp_path.parent / "outside-case.json"
    outside.write_text(json.dumps(g03_payload("FAILED")), encoding="utf-8")
    evidence["matrix"]["FAILED"]["artifact"] = {
        "path": "../outside-case.json",
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    with pytest.raises(ValueError, match="inside the evidence root"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g03_artifact_must_be_a_regular_file(verifier, tmp_path):
    evidence = g03_evidence(tmp_path)
    evidence["matrix"]["FAILED"]["artifact"] = {
        "path": "g03",
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    with pytest.raises(ValueError, match="regular file"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g04_requires_security_ownership_and_bound_legacy_audit(verifier, tmp_path, monkeypatch):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    evidence["container_security"].pop("inspect")
    with pytest.raises(ValueError, match="inspect"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g04_rejects_legacy_audit_hash_mismatch(verifier, tmp_path, monkeypatch):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    evidence["legacy_audit"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="legacy_audit"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g04_malformed_audit_payload_is_a_validation_error(verifier, tmp_path, monkeypatch):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    evidence["legacy_audit"] = artifact(tmp_path, "g04/legacy-consumers.json", b"[]")
    with pytest.raises(ValueError, match="legacy_audit"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Config.User", "root"),
        ("HostConfig.Privileged", True),
        ("HostConfig.ReadonlyRootfs", False),
        ("HostConfig.NetworkMode", "bridge"),
        ("HostConfig.CapDrop", []),
        ("HostConfig.SecurityOpt", []),
        ("HostConfig.NanoCpus", 0),
        ("HostConfig.Memory", 0),
        ("HostConfig.PidsLimit", 0),
        ("HostConfig.LogConfig.Config.MaxSize", "0m"),
    ],
)
def test_g04_rejects_declarative_or_insecure_container_claims(
    verifier, tmp_path, monkeypatch, field, value
):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    ref = evidence["container_security"]["inspect"]
    payload = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    target = payload
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    evidence["container_security"]["inspect"] = artifact(tmp_path, ref["path"], payload)
    with pytest.raises(ValueError, match=parts[-1]):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        b"docker inspect passed",
        {"passed": True},
        {"mounts": [], "binds": ["/root/.ssh:/host-ssh"], "environment": []},
        {"mounts": [], "binds": [], "environment": ["API_TOKEN=secret"]},
    ],
)
def test_g04_rejects_dummy_host_credentials_claims(verifier, tmp_path, monkeypatch, payload):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    ref = evidence["container_security"]["host_credentials"]
    evidence["container_security"]["host_credentials"] = artifact(tmp_path, ref["path"], payload)
    with pytest.raises(ValueError, match="host_credentials"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("field", ["disk_limit_bytes", "run_timeout_seconds"])
def test_g04_requires_positive_normalized_resource_limits(verifier, tmp_path, monkeypatch, field):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    evidence["container_security"][field] = 0
    with pytest.raises(ValueError, match=field):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g04_rejects_docker_socket_mount(verifier, tmp_path, monkeypatch):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    ref = evidence["container_security"]["inspect"]
    payload = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    payload["HostConfig"]["Binds"] = ["/var/run/docker.sock:/var/run/docker.sock"]
    evidence["container_security"]["inspect"] = artifact(tmp_path, ref["path"], payload)
    with pytest.raises(ValueError, match="socket"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("row_index", "field", "value"),
    [
        (1, "run_id", "other-run"),
        (1, "endpoint", "/v1/runs/other-run"),
        (1, "method", "POST"),
        (1, "response", b"not found"),
        (0, "response", {"status": 404, "code": "RUN_NOT_FOUND"}),
        (1, "response", {"status": 404, "code": "FORBIDDEN"}),
    ],
)
def test_g04_rejects_unrelated_or_dummy_ownership_responses(
    verifier, tmp_path, monkeypatch, row_index, field, value
):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    row = evidence["ownership_matrix"][row_index]
    if field == "response":
        row[field] = artifact(tmp_path, f"g04/changed-{row_index}.json", value)
    else:
        row[field] = value
    with pytest.raises(ValueError, match=field):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("change", ["status", "registrations", "routes", "migrations"])
def test_g04_rejects_stale_current_audit(verifier, tmp_path, monkeypatch, change):
    evidence, recorded, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    if change == "status":
        recorded["targets"]["legacy.worker"]["status"] = "live-registered"
    elif change == "registrations":
        recorded["targets"]["legacy.worker"]["registrations"] = [{"path": "old.py"}]
    elif change == "routes":
        recorded["targets"]["legacy.worker"]["live_routes"] = [{"path": "/v1/old"}]
    else:
        recorded["migrations"]["canonical_sha256"]["migration-0.sql"] = "0" * 64
    evidence["legacy_audit"] = artifact(tmp_path, "g04/legacy-consumers.json", recorded)
    with pytest.raises(ValueError, match="audit|migration"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g04_rejects_recorded_migrations_not_in_repository(verifier, tmp_path, monkeypatch):
    evidence, recorded, current = g04_fixture(verifier, tmp_path, monkeypatch)
    hashes = dict(recorded["migrations"]["canonical_sha256"])
    hashes["ghost.sql"] = "0" * 64
    recorded["migrations"]["canonical_sha256"] = hashes
    recorded["migrations"]["deploy_sha256"] = hashes
    evidence["legacy_audit"] = artifact(tmp_path, "g04/legacy-consumers.json", recorded)
    monkeypatch.setattr(verifier, "audit_repository", lambda root: copy.deepcopy(current))
    with pytest.raises(ValueError, match="migration"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g05_requires_bound_browser_record_and_zero_exit_codes(verifier, tmp_path):
    evidence = g05_evidence(tmp_path)
    ref = evidence["browser_result"]
    browser = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    browser["commitSha"] = OTHER_COMMIT
    evidence["browser_result"] = artifact(tmp_path, ref["path"], browser)
    with pytest.raises(ValueError, match="browser_result"):
        verifier.validate_g05(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_p1_requires_desktop_narrow_summary_and_five_category_index(verifier, tmp_path):
    evidence = p1_evidence(tmp_path)
    evidence["evidence_index"]["api"]["categories"] = 4
    with pytest.raises(ValueError, match="categories"):
        verifier.validate_p1(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g04_accepts_real_docker_inspect_array(verifier, tmp_path, monkeypatch):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    ref = evidence["container_security"]["inspect"]
    inspect = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    evidence["container_security"]["inspect"] = artifact(tmp_path, ref["path"], [inspect])
    details = verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)
    assert details["legacy_audit_sha256"] == evidence["legacy_audit"]["sha256"]


@pytest.mark.parametrize("payload", [[], [{}, {}], [{}], b"[{", [1], ["inspect"]])
def test_g04_rejects_invalid_docker_inspect_arrays(verifier, tmp_path, monkeypatch, payload):
    evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    ref = evidence["container_security"]["inspect"]
    inspect = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    if payload == [{}]:
        inspect["HostConfig"]["Privileged"] = True
        payload = [inspect]
    evidence["container_security"]["inspect"] = artifact(tmp_path, ref["path"], payload)
    with pytest.raises(ValueError, match="inspect"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g01_accepts_hash_bound_har_and_png(verifier, tmp_path):
    details = verifier.validate_g01(
        g01_evidence(tmp_path),
        expected_commit_sha=COMMIT,
        archive_result=archive_result(verifier),
        evidence_root=tmp_path,
    )
    assert details["evidence_paths"] == ["g01/public.png", "g01/public.har"]


@pytest.mark.parametrize(
    "payload",
    [
        b"dummy HAR",
        {},
        {"log": {"entries": []}},
        {"log": {"entries": [{}]}},
        {"log": {"entries": [{"request": {}, "response": {"status": 200}}]}},
        {
            "log": {
                "entries": [
                    {
                        "request": {"method": "GET", "url": "https://example.test", "headers": []},
                        "response": {"status": 200, "content": {"mimeType": "application/json"}},
                    }
                ]
            }
        },
        [],
    ],
)
def test_g01_rejects_dummy_or_malformed_har(verifier, tmp_path, payload):
    evidence = g01_evidence(tmp_path)
    evidence["har"] = artifact(tmp_path, evidence["har"]["path"], payload)
    with pytest.raises(ValueError, match="har"):
        verifier.validate_g01(
            evidence,
            expected_commit_sha=COMMIT,
            archive_result=archive_result(verifier),
            evidence_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("method", ""), ("url", ""), ("status", True), ("status", 99), ("status", 600)],
)
def test_g01_rejects_invalid_har_entries(verifier, tmp_path, field, value):
    evidence = g01_evidence(tmp_path)
    har = json.loads((tmp_path / evidence["har"]["path"]).read_text(encoding="utf-8"))
    target = (
        har["log"]["entries"][0]["request"]
        if field in {"method", "url"}
        else har["log"]["entries"][0]["response"]
    )
    target[field] = value
    evidence["har"] = artifact(tmp_path, evidence["har"]["path"], har)
    with pytest.raises(ValueError, match="har"):
        verifier.validate_g01(
            evidence,
            expected_commit_sha=COMMIT,
            archive_result=archive_result(verifier),
            evidence_root=tmp_path,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/auth/start",
        "https://example.test/LOGIN",
        "https://example.test/logout",
        "https://example.test/session",
        "https://example.test/oauth/authorize",
        "https://example.test/token",
        "https://example.test/?AUTH=1",
        "https://example.test/?access_token=1",
        "https://example.test/?refresh_token=1",
        "https://example.test/?session_id=1",
        "https://example.test/?api_key=1",
        "https://example.test/?session=1",
    ],
)
def test_g01_rejects_auth_or_session_urls_even_with_zero_claims(verifier, tmp_path, url):
    evidence = g01_evidence(tmp_path)
    har = json.loads((tmp_path / evidence["har"]["path"]).read_text(encoding="utf-8"))
    har["log"]["entries"].append(copy.deepcopy(har["log"]["entries"][0]))
    har["log"]["entries"][1]["request"]["url"] = url
    evidence["har"] = artifact(tmp_path, evidence["har"]["path"], har)
    with pytest.raises(ValueError, match="auth|session"):
        verifier.validate_g01(
            evidence,
            expected_commit_sha=COMMIT,
            archive_result=archive_result(verifier),
            evidence_root=tmp_path,
        )


@pytest.mark.parametrize("header", ["Authorization", "aUtHoRiZaTiOn", "Cookie", "cOoKiE"])
def test_g01_rejects_credential_headers(verifier, tmp_path, header):
    evidence = g01_evidence(tmp_path)
    har = json.loads((tmp_path / evidence["har"]["path"]).read_text(encoding="utf-8"))
    har["log"]["entries"][0]["request"]["headers"] = [{"name": header, "value": "redacted"}]
    evidence["har"] = artifact(tmp_path, evidence["har"]["path"], har)
    with pytest.raises(ValueError, match="Authorization|Cookie"):
        verifier.validate_g01(
            evidence,
            expected_commit_sha=COMMIT,
            archive_result=archive_result(verifier),
            evidence_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("authenticated", True), ("auth_requests", False), ("session_requests", 0.0)],
)
def test_g01_requires_exact_unauthenticated_integer_zero_counts(verifier, tmp_path, field, value):
    evidence = g01_evidence(tmp_path)
    evidence["visitor"][field] = value
    with pytest.raises(ValueError, match="visitor"):
        verifier.validate_g01(
            evidence,
            expected_commit_sha=COMMIT,
            archive_result=archive_result(verifier),
            evidence_root=tmp_path,
        )


@pytest.mark.parametrize(
    "screenshot",
    [
        b"dummy screenshot",
        b"\x89PNG\r\n\x1a\n",
        png_bytes(319, 200),
        png_bytes(320, 199),
        png_bytes(320, 200)[:100],
        png_bytes(320, 200)[:29] + b"\x00\x00\x00\x00" + png_bytes(320, 200)[33:],
    ],
)
def test_g01_rejects_invalid_screenshot_bytes(verifier, tmp_path, screenshot):
    evidence = g01_evidence(tmp_path)
    evidence["screenshot"] = artifact(tmp_path, evidence["screenshot"]["path"], screenshot)
    with pytest.raises(ValueError, match="screenshot"):
        verifier.validate_g01(
            evidence,
            expected_commit_sha=COMMIT,
            archive_result=archive_result(verifier),
            evidence_root=tmp_path,
        )


@pytest.mark.parametrize("change", ["hash", "traversal", "symlink", "omitted_path"])
def test_g01_rejects_unsafe_or_unbound_screenshots(verifier, tmp_path, change):
    evidence = g01_evidence(tmp_path)
    if change == "hash":
        evidence["screenshot"]["sha256"] = "0" * 64
    elif change == "traversal":
        evidence["screenshot"]["path"] = "../outside.png"
    elif change == "symlink":
        ref = evidence["screenshot"]
        target = tmp_path / ref["path"]
        outside = tmp_path / "outside.png"
        target.rename(outside)
        try:
            target.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable on this platform: {exc}")
    else:
        evidence["evidence_paths"] = ["g01/public.har"]
    with pytest.raises(ValueError, match="screenshot|evidence_paths"):
        verifier.validate_g01(
            evidence,
            expected_commit_sha=COMMIT,
            archive_result=archive_result(verifier),
            evidence_root=tmp_path,
        )


def test_g05_accepts_closed_loop_artifacts(verifier, tmp_path):
    details = verifier.validate_g05(
        g05_evidence(tmp_path), expected_commit_sha=COMMIT, evidence_root=tmp_path
    )
    assert details["browser_result"] == "g05/browser.json"


@pytest.mark.parametrize("field", ["exit_code", "output", "name"])
def test_g05_rejects_status_only_commands(verifier, tmp_path, field):
    evidence = g05_evidence(tmp_path)
    evidence["commands"][0].pop(field)
    with pytest.raises(ValueError, match="commands"):
        verifier.validate_g05(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "other"), ("exit_code", 1), ("exit_code", False), ("output", "  \n")],
)
def test_g05_rejects_invalid_embedded_command_records(verifier, tmp_path, field, value):
    evidence = g05_evidence(tmp_path)
    ref = evidence["commands"][0]["output"]
    payload = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    payload[field] = value
    evidence["commands"][0]["output"] = artifact(tmp_path, ref["path"], payload)
    with pytest.raises(ValueError, match="check"):
        verifier.validate_g05(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    "change", ["hash", "malformed", "duplicate", "bool_exit", "missing_required"]
)
def test_g05_rejects_invalid_command_wrappers(verifier, tmp_path, change):
    evidence = g05_evidence(tmp_path)
    if change == "hash":
        evidence["commands"][0]["output"]["sha256"] = "0" * 64
    elif change == "malformed":
        ref = evidence["commands"][0]["output"]
        evidence["commands"][0]["output"] = artifact(tmp_path, ref["path"], b"not json")
    elif change == "duplicate":
        evidence["commands"][1]["name"] = "check"
    elif change == "bool_exit":
        evidence["commands"][0]["exit_code"] = False
    else:
        evidence["commands"].pop()
    with pytest.raises(ValueError, match="check|commands|command"):
        verifier.validate_g05(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("browser", "browser", ""),
        ("upload", "paperId", ""),
        ("upload", "workflowId", "other"),
        ("planApproval", "experimentId", "other"),
        ("planApproval", "planId", "other"),
        ("planApproval", "planVersion", True),
        ("planApproval", "planVersion", 0),
        ("execution", "runId", ""),
        ("execution", "workflowId", "other"),
        ("execution", "experimentId", "other"),
        ("execution", "planId", "other"),
        ("execution", "planVersion", 0),
        ("execution", "status", "failed"),
        ("execution", "attempt", True),
        ("execution", "codeHash", "C" * 64),
        ("reportDownload", "reportId", ""),
        ("reportDownload", "version", 0),
        ("reportDownload", "runId", "other"),
        ("reportDownload", "markdownHash", "0" * 64),
        ("reportDownload", "finalAttempt", 4),
    ],
)
def test_g05_rejects_unrelated_or_incomplete_closed_loop_identities(
    verifier, tmp_path, section, field, value
):
    evidence = g05_evidence(tmp_path)
    ref = evidence["browser_result"]
    browser = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    if section == "browser":
        browser[field] = value
    else:
        browser[section][field] = value
    evidence["browser_result"] = artifact(tmp_path, ref["path"], browser)
    with pytest.raises(ValueError, match=field):
        verifier.validate_g05(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        "placeholder",
        "bare_path",
        "stale",
        "markdown_hash",
        "empty_markdown",
        "changed_markdown",
        "missing_upload",
        "missing_execution_attempt",
    ],
)
def test_g05_rejects_placeholder_or_unbound_download_evidence(verifier, tmp_path, change):
    evidence = g05_evidence(tmp_path)
    if change == "placeholder":
        ref = evidence["browser_result"]
        evidence["browser_result"] = artifact(
            tmp_path, ref["path"], {"commitSha": COMMIT, "passed": True}
        )
    elif change == "bare_path":
        evidence["browser_result"] = evidence["browser_result"]["path"]
    elif change == "stale":
        ref = evidence["browser_result"]
        browser = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
        browser["commitSha"] = OTHER_COMMIT
        evidence["browser_result"] = artifact(tmp_path, ref["path"], browser)
    elif change == "markdown_hash":
        evidence["report_download"]["sha256"] = "0" * 64
    elif change == "changed_markdown":
        evidence["report_download"] = artifact(
            tmp_path, evidence["report_download"]["path"], b"# Different report\n"
        )
    else:
        ref = evidence["browser_result"]
        browser = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
        if change == "missing_upload":
            browser.pop("upload")
        elif change == "missing_execution_attempt":
            browser["execution"].pop("attempt")
        else:
            browser["reportDownload"]["markdownHash"] = hashlib.sha256(b"").hexdigest()
            evidence["report_download"] = artifact(
                tmp_path, evidence["report_download"]["path"], b""
            )
        evidence["browser_result"] = artifact(tmp_path, ref["path"], browser)
    with pytest.raises(ValueError, match="browser_result|report_download|reportDownload|sha256"):
        verifier.validate_g05(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g05_allows_additional_uniquely_named_commands(verifier, tmp_path):
    evidence = g05_evidence(tmp_path)
    payload = {"name": "lint_extra", "exit_code": 0, "output": "extra captured output"}
    evidence["commands"].append(
        {
            "name": "lint_extra",
            "exit_code": 0,
            "output": artifact(tmp_path, "g05/extra.json", payload),
        }
    )
    details = verifier.validate_g05(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)
    assert "lint_extra" in details["commands"]


def test_p1_accepts_summary_and_canonical_category_artifacts(verifier, tmp_path):
    details = verifier.validate_p1(
        p1_evidence(tmp_path), expected_commit_sha=COMMIT, evidence_root=tmp_path
    )
    assert details["evidence_categories"] == 5
    assert set(details["paths"]) == {
        "p1/desktop.png",
        "p1/narrow.png",
        "p1/browser.png",
        "p1/api.json",
    }


@pytest.mark.parametrize(
    ("surface", "field", "value"),
    [
        ("desktop", "columns", True),
        ("narrow", "columns", 4),
        ("narrow", "run_id", "other"),
        ("desktop", "paper", " "),
        ("narrow", "plan", ""),
        ("desktop", "actual", ""),
    ],
)
def test_p1_rejects_invalid_summary_rows(verifier, tmp_path, surface, field, value):
    evidence = p1_evidence(tmp_path)
    evidence["first_screen_summary"][surface][field] = value
    with pytest.raises(ValueError, match=field):
        verifier.validate_p1(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    "categories",
    [
        5,
        ["paper", "plan", "code", "execution"],
        ["paper"] * 5,
        list(reversed(P1_CATEGORIES)),
        ["paper", "plan", "code", "execution", "unknown"],
    ],
)
def test_p1_rejects_noncanonical_category_lists(verifier, tmp_path, categories):
    evidence = p1_evidence(tmp_path)
    evidence["evidence_index"]["browser"]["categories"] = categories
    with pytest.raises(ValueError, match="categories"):
        verifier.validate_p1(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("surface", ["browser", "api"])
def test_p1_rejects_unrelated_run_ids(verifier, tmp_path, surface):
    evidence = p1_evidence(tmp_path)
    evidence["evidence_index"][surface]["run_id"] = "other"
    with pytest.raises(ValueError, match="run_id"):
        verifier.validate_p1(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("change", ["browser_png", "dummy_png", "api_hash", "api_malformed"])
def test_p1_rejects_invalid_bound_artifacts(verifier, tmp_path, change):
    evidence = p1_evidence(tmp_path)
    if change == "browser_png":
        ref = evidence["evidence_index"]["browser"]["screenshot"]
        evidence["evidence_index"]["browser"]["screenshot"] = artifact(
            tmp_path, ref["path"], b"dummy"
        )
    elif change == "dummy_png":
        ref = evidence["first_screen_summary"]["desktop"]["screenshot"]
        evidence["first_screen_summary"]["desktop"]["screenshot"] = artifact(
            tmp_path, ref["path"], b"dummy"
        )
    elif change == "api_hash":
        evidence["evidence_index"]["api"]["result"]["sha256"] = "0" * 64
    else:
        ref = evidence["evidence_index"]["api"]["result"]
        evidence["evidence_index"]["api"]["result"] = artifact(tmp_path, ref["path"], b"not json")
    with pytest.raises(ValueError, match="screenshot|result"):
        verifier.validate_p1(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "other"),
        ("categories", ["paper"]),
        ("items", []),
        ("items", [{"id": "x"}]),
        (
            "items",
            [
                {
                    "id": f"i-{c}",
                    "category": c,
                    "target_type": "artifact",
                    "target_id": "t",
                    "locator": {},
                }
                for c in P1_CATEGORIES[:-1]
            ],
        ),
        (
            "items",
            [
                {
                    "id": f"i-{c}",
                    "category": c,
                    "target_type": "artifact",
                    "target_id": "t",
                    "locator": [],
                }
                for c in P1_CATEGORIES
            ],
        ),
        (
            "items",
            [
                {"id": f"i-{c}", "category": c, "target_type": "", "target_id": "t", "locator": {}}
                for c in P1_CATEGORIES
            ],
        ),
    ],
)
def test_p1_rejects_invalid_api_items(verifier, tmp_path, field, value):
    evidence = p1_evidence(tmp_path)
    ref = evidence["evidence_index"]["api"]["result"]
    payload = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))
    payload[field] = value
    evidence["evidence_index"]["api"]["result"] = artifact(tmp_path, ref["path"], payload)
    with pytest.raises(ValueError, match="result|items"):
        verifier.validate_p1(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("missing", ["p1/api.json", "p1/browser.png", "p1/desktop.png"])
def test_p1_evidence_paths_include_every_bound_artifact(verifier, tmp_path, missing):
    evidence = p1_evidence(tmp_path)
    evidence["evidence_paths"].remove(missing)
    with pytest.raises(ValueError, match="evidence_paths"):
        verifier.validate_p1(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


@pytest.mark.parametrize("gate", GATES)
def test_missing_gate_evidence_is_pending_with_reason(verifier, tmp_path, gate):
    entry = verifier.evaluate_gate(gate, tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert entry["commit_sha"] == COMMIT
    assert entry["evidence_paths"] == [GATE_PATHS[gate]]
    assert entry["details"]["reason"]


@pytest.mark.parametrize("gate", GATES)
def test_malformed_gate_evidence_is_pending_not_failed(verifier, tmp_path, gate):
    path = tmp_path / GATE_PATHS[gate]
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")
    entry = verifier.evaluate_gate(gate, tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert "malformed JSON" in entry["details"]["reason"]


@pytest.mark.parametrize("gate", GATES)
def test_structurally_incomplete_gate_evidence_is_pending(verifier, tmp_path, gate):
    write_json(
        tmp_path,
        GATE_PATHS[gate],
        {"commitSha": COMMIT, "passed": True},
    )
    entry = verifier.evaluate_gate(gate, tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert entry["details"]["reason"]


def test_historical_g02_note_uses_latest_numeric_attempt(verifier, tmp_path):
    write_json(tmp_path, "g02/full-run-9/result.json", {"passed": True})
    write_json(tmp_path, "g02/full-run-10/result.json", {"passed": True})
    entry = verifier.evaluate_gate("G-02", tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert "full-run-10/result.json has no commitSha" in entry["details"]["reason"]


def test_stale_evidence_is_pending_even_if_it_explicitly_failed(verifier, tmp_path):
    write_json(
        tmp_path,
        "g03/result.json",
        {"commitSha": OTHER_COMMIT, "passed": False, "exit_code": 1},
    )
    entry = verifier.evaluate_gate("G-03", tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert "stale" in entry["details"]["reason"]


def test_explicitly_failed_current_evidence_is_failed(verifier, tmp_path):
    write_json(
        tmp_path,
        "g03/result.json",
        {"commitSha": COMMIT, "passed": False, "exit_code": 1},
    )
    entry = verifier.evaluate_gate("G-03", tmp_path, COMMIT)
    assert entry["status"] == "failed"
    assert "explicitly failed" in entry["details"]["reason"]


def test_incomplete_current_evidence_is_pending_not_failed(verifier, tmp_path):
    write_json(tmp_path, "g03/result.json", {"commitSha": COMMIT, "passed": True})
    entry = verifier.evaluate_gate("G-03", tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert "exit_code" in entry["details"]["reason"]


def test_explicit_pending_status_never_passes(verifier, tmp_path):
    write_json(
        tmp_path,
        "g03/result.json",
        {"commitSha": COMMIT, "passed": True, "status": "pending", "exit_code": 0},
    )
    entry = verifier.evaluate_gate("G-03", tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert "pending" in entry["details"]["reason"]


def test_failed_assertion_in_current_evidence_is_pending(verifier, tmp_path):
    evidence = g03_evidence(tmp_path)
    evidence["assertions"][0]["status"] = "failed"
    write_json(tmp_path, "g03/result.json", evidence)
    entry = verifier.evaluate_gate("G-03", tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert "assertions" in entry["details"]["reason"]


def test_complete_current_evidence_can_pass_every_gate(verifier, tmp_path, monkeypatch):
    root = tmp_path
    g04_evidence, _, _ = g04_fixture(verifier, tmp_path, monkeypatch)
    evidence_by_gate = {
        "G-02": g02_evidence(root),
        "G-03": g03_evidence(root),
        "G-04": g04_evidence,
        "G-05": g05_evidence(root),
        "NFR-05": nfr05_evidence(verifier),
        "P1": p1_evidence(root),
        "G-01": g01_evidence(root),
    }
    for gate, evidence in evidence_by_gate.items():
        write_json(root, GATE_PATHS[gate], evidence)
    monkeypatch.setattr(
        verifier,
        "run_archive_verifier",
        lambda: {
            "run": verifier.ARCHIVE_RUN,
            "verified_files": 9,
            "model_sha256": verifier.ARCHIVE_MODEL_SHA256,
            "report_sha256": verifier.ARCHIVE_REPORT_SHA256,
        },
    )
    ledger = verifier.evaluate_all(root, COMMIT)
    assert all(entry["status"] == "passed" for entry in ledger["gates"].values()), ledger["gates"]
    assert ledger["v2_deliverable"] is True


def test_cli_writes_ledger_and_table_then_returns_nonzero(verifier, tmp_path, monkeypatch, capsys):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    output = evidence / "acceptance-ledger.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_v2_acceptance.py",
            "--evidence-root",
            str(evidence),
            "--output",
            str(output),
            "--commit-sha",
            COMMIT,
        ],
    )
    assert verifier.main() != 0
    ledger = json.loads(output.read_text(encoding="utf-8"))
    assert ledger["commit_sha"] == COMMIT
    assert ledger["v2_deliverable"] is False
    assert all(ledger["gates"][gate]["status"] == "pending" for gate in GATES)
    terminal = capsys.readouterr().out
    assert "GATE" in terminal and "STATUS" in terminal and "REASON" in terminal
    assert "v2_deliverable=false" in terminal


def test_deliverable_rejects_stale_per_gate_commit(verifier):
    statuses = {gate: {"status": "passed", "commit_sha": COMMIT} for gate in GATES}
    statuses["G-03"]["commit_sha"] = OTHER_COMMIT
    ledger = verifier.build_ledger(statuses, commit_sha=COMMIT)
    assert ledger["gates"]["G-03"]["status"] == "pending"
    assert ledger["gates"]["G-03"]["commit_sha"] == COMMIT
    assert "commit" in ledger["gates"]["G-03"]["details"]["reason"]
    assert ledger["v2_deliverable"] is False


def test_deliverable_rejects_missing_per_gate_commit(verifier):
    statuses = {gate: {"status": "passed", "commit_sha": COMMIT} for gate in GATES}
    statuses["G-04"].pop("commit_sha")
    ledger = verifier.build_ledger(statuses, commit_sha=COMMIT)
    assert ledger["gates"]["G-04"]["status"] == "pending"
    assert ledger["v2_deliverable"] is False


@pytest.mark.parametrize("commit_sha", ["bad", COMMIT.upper()])
def test_ledger_rejects_invalid_top_level_commit(verifier, commit_sha):
    with pytest.raises(ValueError, match="commit SHA"):
        verifier.build_ledger({}, commit_sha=commit_sha)


@pytest.mark.parametrize(
    "relative",
    [
        "README.md",
        "g03/result.json",
        "report.md",
        "acceptance-ledger.json.backup",
        "../victim.json",
    ],
)
def test_cli_rejects_noncanonical_output_without_changing_victim(verifier, tmp_path, relative):
    root = tmp_path / "evidence"
    root.mkdir()
    victim = (root / relative).resolve()
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"do not overwrite")
    with pytest.raises(SystemExit) as error:
        verifier.main(
            [
                "--evidence-root",
                str(root),
                "--output",
                str(root / relative),
                "--commit-sha",
                COMMIT,
            ]
        )
    assert error.value.code == 2
    assert victim.read_bytes() == b"do not overwrite"
    assert not (root / "acceptance-ledger.json").exists()


def test_cli_rejects_missing_evidence_root(verifier, tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(SystemExit) as error:
        verifier.main(["--evidence-root", str(root), "--commit-sha", COMMIT])
    assert error.value.code == 2
    assert not root.exists()


def test_cli_rejects_symlink_output_without_changing_target(verifier, tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"do not overwrite")
    output = root / "acceptance-ledger.json"
    try:
        output.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    with pytest.raises(SystemExit) as error:
        verifier.main(
            [
                "--evidence-root",
                str(root),
                "--output",
                str(output),
                "--commit-sha",
                COMMIT,
            ]
        )
    assert error.value.code == 2
    assert victim.read_bytes() == b"do not overwrite"
    assert output.is_symlink()


def test_cli_atomically_replaces_only_canonical_ledger(verifier, tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir()
    output = root / "acceptance-ledger.json"
    replaced = []
    original_replace = verifier.os.replace

    def checked_replace(source, destination):
        source = Path(source)
        destination = Path(destination)
        assert source.parent == root
        assert destination == output
        assert source != output
        replaced.append(source)
        original_replace(source, destination)

    monkeypatch.setattr(verifier.os, "replace", checked_replace)
    for _ in range(2):
        assert (
            verifier.main(
                [
                    "--evidence-root",
                    str(root),
                    "--commit-sha",
                    COMMIT,
                ]
            )
            == 1
        )
    assert len(replaced) == 2
    assert all(not path.exists() for path in replaced)
    assert json.loads(output.read_text(encoding="utf-8"))["commit_sha"] == COMMIT


def test_verifier_does_not_echo_credential_like_evidence(verifier, tmp_path, monkeypatch, capsys):
    secret = "fake-secret-that-must-not-be-printed"
    write_json(
        tmp_path,
        "g03/result.json",
        {"commitSha": COMMIT, "passed": True, "access_token": secret},
    )
    entry = verifier.evaluate_gate("G-03", tmp_path, COMMIT)
    assert secret not in json.dumps(entry)
    output = tmp_path / "acceptance-ledger.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_v2_acceptance.py",
            "--evidence-root",
            str(tmp_path),
            "--output",
            str(output),
            "--commit-sha",
            COMMIT,
        ],
    )
    assert verifier.main() != 0
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert secret not in output.read_text(encoding="utf-8")
