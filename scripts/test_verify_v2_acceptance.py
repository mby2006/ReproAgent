"""Contract tests for the read-only, final-commit-bound v2 acceptance ledger."""

import copy
import hashlib
import importlib.util
import json
import math
import sys
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


def g01_evidence() -> dict:
    return {
        "commitSha": COMMIT,
        "passed": True,
        "archive": {
            "run": "01993dd36ff17102",
            "model_sha256": "01993dd36ff1710272c3c50e16a0ffb10dc2015c08e8080b22b7e480b2c83b47",
            "report_sha256": "5a7fcba954b496bd5e87483f7cad81b7155f048a111bcd81d3e769eca9d7349d",
        },
        "visitor": {"authenticated": False, "auth_requests": 0, "session_requests": 0},
        "evidence_paths": ["g01/public.png", "g01/public.har"],
        "assertions": passed_assertions("public archive", "visitor without auth"),
    }


def g03_evidence() -> dict:
    return {
        "commitSha": COMMIT,
        "passed": True,
        "exit_code": 0,
        "matrix": {
            name: {"status": "passed", "evidence_path": f"g03/{name}.json"}
            for name in ("FAILED", "TIMEOUT", "CANCELLED", "OOM", "repair", "restart")
        },
        "assertions": passed_assertions("negative and recovery matrix"),
    }


def g04_evidence(root: Path) -> dict:
    hashes = {f"migration-{index}.sql": f"{index:064x}" for index in range(16)}
    audit = {
        "ok": True,
        "blocking_consumers": [],
        "scan_errors": [],
        "migrations": {
            "ok": True,
            "canonical_sha256": hashes,
            "deploy_sha256": hashes,
            "errors": [],
            "missing": [],
            "mismatched": [],
        },
    }
    audit_path = write_json(root, "g04/legacy-consumers.json", audit)
    return {
        "commitSha": COMMIT,
        "passed": True,
        "container_security": {
            "non_root": True,
            "read_only_root_filesystem": True,
            "network_mode": "none",
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "host_credentials_absent": True,
        },
        "ownership_matrix": [
            {"account": "owner", "run_id": "run-1", "http_status": 200},
            {"account": "other", "run_id": "run-1", "http_status": 404},
        ],
        "legacy_audit": {
            "path": "g04/legacy-consumers.json",
            "sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        },
        "assertions": passed_assertions(
            "container security", "two-account ownership", "legacy audit"
        ),
    }


def g05_evidence(root: Path) -> dict:
    browser = {"commitSha": COMMIT, "passed": True, "browser": "chromium"}
    write_json(root, "g05/browser.json", browser)
    return {
        "commitSha": COMMIT,
        "passed": True,
        "commands": [
            {"name": "check", "exit_code": 0},
            {"name": "check_integration", "exit_code": 0},
            {"name": "browser_smoke", "exit_code": 0},
        ],
        "browser_result": "g05/browser.json",
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


def p1_evidence() -> dict:
    return {
        "commitSha": COMMIT,
        "passed": True,
        "first_screen_summary": {
            "desktop": {"columns": 3, "status": "passed"},
            "narrow": {"columns": 3, "status": "passed"},
        },
        "evidence_index": {
            "browser": {"categories": 5, "status": "passed"},
            "api": {"categories": 5, "status": "passed"},
        },
        "evidence_paths": ["p1/desktop.png", "p1/narrow.png", "p1/evidence-index.png"],
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
    evidence = g01_evidence()
    evidence["visitor"]["auth_requests"] = 1
    with pytest.raises(ValueError, match="auth"):
        verifier.validate_g01(evidence, expected_commit_sha=COMMIT)


def test_g01_rejects_wrong_public_model_identity(verifier):
    evidence = g01_evidence()
    evidence["archive"]["model_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="model_sha256"):
        verifier.validate_g01(evidence, expected_commit_sha=COMMIT)


def test_public_archive_verifier_runs_without_starting_artifact_cli(verifier):
    result = verifier.run_archive_verifier()
    assert result["run"] == "01993dd36ff17102"
    assert result["verified_files"] == 9
    assert result["model_sha256"] == verifier.ARCHIVE_MODEL_SHA256
    assert result["report_sha256"] == verifier.ARCHIVE_REPORT_SHA256


def test_g03_requires_complete_passing_negative_matrix(verifier):
    evidence = g03_evidence()
    evidence["matrix"]["OOM"]["status"] = "failed"
    with pytest.raises(ValueError, match="OOM"):
        verifier.validate_g03(evidence, expected_commit_sha=COMMIT)


def test_g04_requires_security_ownership_and_bound_legacy_audit(verifier, tmp_path):
    evidence = g04_evidence(tmp_path)
    evidence["container_security"]["host_credentials_absent"] = False
    with pytest.raises(ValueError, match="host_credentials_absent"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g04_rejects_legacy_audit_hash_mismatch(verifier, tmp_path):
    evidence = g04_evidence(tmp_path)
    evidence["legacy_audit"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="legacy_audit"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g04_malformed_audit_payload_is_a_validation_error(verifier, tmp_path):
    evidence = g04_evidence(tmp_path)
    audit_path = tmp_path / "g04/legacy-consumers.json"
    audit_path.write_text("[]", encoding="utf-8")
    evidence["legacy_audit"]["sha256"] = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="legacy_audit"):
        verifier.validate_g04(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_g05_requires_bound_browser_record_and_zero_exit_codes(verifier, tmp_path):
    evidence = g05_evidence(tmp_path)
    write_json(tmp_path, "g05/browser.json", {"commitSha": OTHER_COMMIT, "passed": True})
    with pytest.raises(ValueError, match="browser_result"):
        verifier.validate_g05(evidence, expected_commit_sha=COMMIT, evidence_root=tmp_path)


def test_p1_requires_desktop_narrow_summary_and_five_category_index(verifier):
    evidence = p1_evidence()
    evidence["evidence_index"]["api"]["categories"] = 4
    with pytest.raises(ValueError, match="categories"):
        verifier.validate_p1(evidence, expected_commit_sha=COMMIT)


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
    evidence = g03_evidence()
    evidence["assertions"][0]["status"] = "failed"
    write_json(tmp_path, "g03/result.json", evidence)
    entry = verifier.evaluate_gate("G-03", tmp_path, COMMIT)
    assert entry["status"] == "pending"
    assert "assertions" in entry["details"]["reason"]


def test_complete_current_evidence_can_pass_every_gate(verifier, tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir()
    evidence_by_gate = {
        "G-02": g02_evidence(root),
        "G-03": g03_evidence(),
        "G-04": g04_evidence(root),
        "G-05": g05_evidence(root),
        "NFR-05": nfr05_evidence(verifier),
        "P1": p1_evidence(),
        "G-01": g01_evidence(),
    }
    for relative in (
        "g01/public.png",
        "g01/public.har",
        "p1/desktop.png",
        "p1/narrow.png",
        "p1/evidence-index.png",
        *(
            "g03/" + name + ".json"
            for name in ("FAILED", "TIMEOUT", "CANCELLED", "OOM", "repair", "restart")
        ),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"evidence")
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
    assert all(entry["status"] == "passed" for entry in ledger["gates"].values())
    assert ledger["v2_deliverable"] is True


def test_cli_writes_ledger_and_table_then_returns_nonzero(verifier, tmp_path, monkeypatch, capsys):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    output = evidence / "ledger.json"
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


def test_verifier_does_not_echo_credential_like_evidence(verifier, tmp_path, monkeypatch, capsys):
    secret = "fake-secret-that-must-not-be-printed"
    write_json(
        tmp_path,
        "g03/result.json",
        {"commitSha": COMMIT, "passed": True, "access_token": secret},
    )
    entry = verifier.evaluate_gate("G-03", tmp_path, COMMIT)
    assert secret not in json.dumps(entry)
    output = tmp_path / "ledger.json"
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
