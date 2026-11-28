"""Task 12: final-attempt evidence provenance and private artifact downloads."""

import hashlib
import json
from uuid import uuid4

import pytest
from test_runs import (
    AUTH,
    BOB_AUTH,
    EXPERIMENT_ID,
    PLAN_ID,
    PROJECT_ID,
    USER_ID,
    FakeAdmin,
    _seed_run,
    seed_tables,
)
from test_runs import (
    env as env,
)


def _seed_final_attempt():
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin)
    run = admin.tables["execution_runs"][0]
    final_plan_id = str(uuid4())
    admin.tables["experiment_plans"].append(
        {
            "id": final_plan_id,
            "experiment_id": str(EXPERIMENT_ID),
            "version": 2,
            "config_json": admin.tables["experiment_plans"][0]["config_json"],
            "approved_at": "2026-01-01T00:00:00+00:00",
            "origin": "oom_retry",
            "changes_json": [],
        }
    )
    file_ids = {}
    artifact_ids = {}
    for attempt in (1, 2):
        admin.tables["execution_attempts"].append(
            {
                "id": str(uuid4()),
                "run_id": run_id,
                "attempt": attempt,
                "plan_id": str(PLAN_ID) if attempt == 1 else final_plan_id,
                "plan_version": attempt,
                "code_version": attempt,
                "status": "failed" if attempt == 1 else "succeeded",
                "runner_request_id": str(uuid4()),
                "runner_run_id": f"runner-{attempt}",
            }
        )
        for name in ("train.py", "config.yaml"):
            file_id = str(uuid4())
            content = f"attempt-{attempt}:{name}"
            admin.tables["generated_files"].append(
                {
                    "id": file_id,
                    "experiment_id": str(EXPERIMENT_ID),
                    "version": attempt,
                    "path": name,
                    "content": content,
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                }
            )
            admin.tables["execution_attempt_files"].append(
                {
                    "run_id": run_id,
                    "attempt": attempt,
                    "generated_file_id": file_id,
                }
            )
            file_ids[attempt, name] = file_id
        for name in ("metrics.json", "model.pt"):
            artifact_id = str(uuid4())
            path = f"{run_id}/attempt-{attempt}/artifacts/{name}"
            admin.tables["execution_artifacts"].append(
                {
                    "id": artifact_id,
                    "run_id": run_id,
                    "attempt": attempt,
                    "artifact_type": name,
                    "storage_path": path,
                    "metadata_json": {},
                }
            )
            admin.storage.files[path] = json.dumps({"attempt": attempt}).encode()
            artifact_ids[attempt, name] = artifact_id
    # Mutable projection deliberately points at old evidence; it must not be used.
    admin.tables["execution_run_files"].append(
        {
            "run_id": run_id,
            "generated_file_id": file_ids[1, "train.py"],
        }
    )
    run.update(
        {
            "plan_id": str(PLAN_ID),
            "plan_version": 1,
            "repair_attempt": 1,
            "audit_json": {
                "code_version": 2,
                "plan_version": 2,
                "image_digest": "sha256:final-image",
                "dataset_fingerprint": "dataset-final",
            },
        }
    )
    admin.tables["papers"][0].update(
        file_sha256="a" * 64, source_url="https://example.test/paper.pdf"
    )
    admin.tables["reproduction_reports"].append(
        {
            "id": str(uuid4()),
            "run_id": run_id,
            "version": 1,
            "attempt": 2,
            "markdown": "# final report",
            "content_sha256": "b" * 64,
            "report_json": {"references": {"review_trace_ids": []}},
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    return admin, run_id, final_plan_id, file_ids, artifact_ids


def test_index_points_to_exact_final_attempt(db=None, run=None):
    admin, run_id, final_plan_id, file_ids, artifact_ids = _seed_final_attempt()
    from reproagent_api.services.evidence_index import build_evidence_index

    index = build_evidence_index(admin, run_id, actor=USER_ID)
    assert {item.category for item in index.items} == {
        "paper",
        "plan",
        "code",
        "execution",
        "review_report",
    }
    plan = next(item for item in index.items if item.category == "plan")
    assert plan.target_id == final_plan_id
    assert plan.locator["version"] == 2
    assert plan.locator["approved_at"] == "2026-01-01T00:00:00+00:00"
    approval_trace = next(item for item in index.items if item.target_type == "approval_trace")
    assert approval_trace.category == "plan"
    assert approval_trace.target_id == final_plan_id
    assert approval_trace.locator["plan_id"] == final_plan_id
    assert approval_trace.locator["version"] == 2
    assert "trace_id" not in approval_trace.locator
    assert approval_trace.hash is None
    assert "不可变" in approval_trace.unavailable_reason
    code = [item for item in index.items if item.category == "code"]
    assert {item.target_id for item in code} == {
        file_ids[2, "train.py"],
        file_ids[2, "config.yaml"],
    }
    assert all(item.locator["attempt"] == 2 for item in code)
    artifacts = [item for item in index.items if item.target_type == "artifact"]
    assert {item.target_id for item in artifacts} == {
        artifact_ids[2, "metrics.json"],
        artifact_ids[2, "model.pt"],
    }
    assert all(item.locator["attempt"] == 2 for item in artifacts)
    assert {item.locator["logical_path"] for item in artifacts} == {
        "attempt-2/artifacts/metrics.json",
        "attempt-2/artifacts/model.pt",
    }
    report = next(item for item in index.items if item.target_type == "report")
    assert report.locator["version"] == 1
    assert report.hash == hashlib.sha256(b"# final report").hexdigest()
    assert report.hash != "b" * 64
    assert (
        next(item for item in index.items if item.target_type == "image").hash
        == "sha256:final-image"
    )
    assert (
        next(item for item in index.items if item.target_type == "dataset").hash == "dataset-final"
    )
    assert "storage_path" not in json.dumps([item.model_dump() for item in index.items])
    assert all(
        item.locator and all(value != "" for value in item.locator.values()) for item in index.items
    )


def test_other_owner_cannot_read_index(db=None, other_user=None):
    from reproagent_api.services.evidence_index import build_evidence_index
    from test_runs import BOB_ID

    admin, run_id, *_ = _seed_final_attempt()
    with pytest.raises(PermissionError):
        build_evidence_index(admin, run_id, actor=BOB_ID)
    with pytest.raises(PermissionError):
        build_evidence_index(admin, uuid4(), actor=USER_ID)


def test_missing_evidence_is_explicit_and_never_reuses_earlier_attempt():
    from reproagent_api.services.evidence_index import build_evidence_index

    admin, run_id, *_ = _seed_final_attempt()
    admin.tables["execution_attempt_files"] = [
        row for row in admin.tables["execution_attempt_files"] if row["attempt"] == 1
    ]
    admin.tables["execution_artifacts"] = []
    admin.tables["reproduction_reports"] = []
    admin.tables["execution_runs"][0]["audit_json"] = {
        "code_version": 1,
        "plan_version": 1,
        "image_digest": "sha256:old",
    }
    index = build_evidence_index(admin, run_id, actor=USER_ID)
    unavailable = [item for item in index.items if item.unavailable_reason]
    assert {item.category for item in unavailable} == {"plan", "code", "execution", "review_report"}
    assert all(item.hash is None for item in unavailable)
    metrics = [item for item in index.items if item.target_type == "metrics"]
    assert len(metrics) == 1
    assert metrics[0].locator["attempt"] == 2
    assert metrics[0].unavailable_reason
    assert not any(item.target_type == "artifact" for item in index.items)
    assert all("attempt-1" not in json.dumps(item.locator) for item in index.items)
    assert next(item for item in index.items if item.target_type == "image").hash is None

    admin.tables["execution_runs"][0]["audit_json"] = {
        "image_digest": "sha256:stale-without-version",
        "dataset_fingerprint": "stale-without-version",
    }
    stale_audit_index = build_evidence_index(admin, run_id, actor=USER_ID)
    assert all(
        item.hash is None
        for item in stale_audit_index.items
        if item.target_type in {"image", "dataset"}
    )


def test_report_from_earlier_attempt_is_marked_unavailable():
    from reproagent_api.services.evidence_index import build_evidence_index

    admin, run_id, *_ = _seed_final_attempt()
    admin.tables["reproduction_reports"][0]["attempt"] = 1
    report = next(
        item
        for item in build_evidence_index(admin, run_id, actor=USER_ID).items
        if item.target_type == "report"
    )
    assert "最终尝试" in report.unavailable_reason


def test_matching_report_without_persisted_review_traces_is_explicit():
    from reproagent_api.services.evidence_index import build_evidence_index

    admin, run_id, *_ = _seed_final_attempt()
    index = build_evidence_index(admin, run_id, actor=USER_ID)
    traces = [item for item in index.items if item.target_type == "review_trace"]
    assert len(traces) == 2
    assert {item.locator["source"] for item in traces} == {"card", "plan"}
    assert all(item.category == "review_report" for item in traces)
    assert all(item.hash is None and item.unavailable_reason for item in traces)
    assert all("trace_id" not in item.locator for item in traces)
    report = next(item for item in index.items if item.target_type == "report")
    assert all(item.locator["report_id"] == report.target_id for item in traces)
    assert all(item.locator["version"] == report.locator["version"] for item in traces)


@pytest.mark.parametrize("present_source", ["card", "plan"])
def test_each_missing_review_trace_source_is_explicit(present_source):
    from reproagent_api.services.evidence_index import build_evidence_index

    admin, run_id, *_ = _seed_final_attempt()
    event = {
        "id": str(uuid4()),
        "field_path": "optimizer",
        "before": "sgd",
        "after": "sgd",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    if present_source == "card":
        admin.tables["reproduction_specs"][0]["spec_json"]["review_decisions"] = [event]
    else:
        admin.tables["experiment_plans"][1]["changes_json"] = [event]
    index = build_evidence_index(admin, run_id, actor=USER_ID)
    traces = [item for item in index.items if item.target_type == "review_trace"]
    assert len(traces) == 2
    available = next(item for item in traces if not item.unavailable_reason)
    missing = next(item for item in traces if item.unavailable_reason)
    assert available.target_id == event["id"]
    assert available.locator["source"] == present_source
    assert missing.locator["source"] == ("plan" if present_source == "card" else "card")
    assert "trace_id" not in missing.locator


def test_review_trace_ids_only_include_real_persisted_card_and_plan_events():
    from reproagent_api.services.evidence_index import build_evidence_index

    admin, run_id, final_plan_id, *_ = _seed_final_attempt()
    card_event = {
        "id": str(uuid4()),
        "field_path": "optimizer",
        "before": "sgd",
        "after": "sgd",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    plan_event = {**card_event, "id": str(uuid4()), "field_path": "batch_size"}
    admin.tables["reproduction_specs"][0]["spec_json"]["review_decisions"] = [card_event]
    admin.tables["experiment_plans"][1]["changes_json"] = [plan_event]
    admin.tables["reproduction_reports"][0]["report_json"]["references"]["review_trace_ids"] = [
        card_event["id"],
        plan_event["id"],
        str(uuid4()),
    ]
    index = build_evidence_index(admin, run_id, actor=USER_ID)
    traces = [item for item in index.items if item.target_type == "review_trace"]
    assert {item.target_id for item in traces} == {card_event["id"], plan_event["id"]}
    assert {item.locator["source"] for item in traces} == {"card", "plan"}
    assert all(
        item.locator["plan_id"] == final_plan_id
        for item in traces
        if item.locator["source"] == "plan"
    )


def test_missing_final_attempt_never_falls_back_to_mutable_run_projection():
    from reproagent_api.services.evidence_index import build_evidence_index

    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    run_id = _seed_run(admin)
    file_id = str(uuid4())
    admin.tables["generated_files"].append(
        {
            "id": file_id,
            "experiment_id": str(EXPERIMENT_ID),
            "version": 1,
            "path": "legacy.py",
            "content": "legacy",
            "sha256": hashlib.sha256(b"legacy").hexdigest(),
        }
    )
    admin.tables["execution_run_files"].append(
        {"run_id": run_id, "generated_file_id": file_id}
    )
    index = build_evidence_index(admin, run_id, actor=USER_ID)
    assert all(
        item.target_type != "generated_file" or item.unavailable_reason for item in index.items
    )
    for category in ("plan", "code", "execution", "review_report"):
        category_items = [item for item in index.items if item.category == category]
        assert category_items and all(item.unavailable_reason for item in category_items)


def test_code_without_hash_is_explicitly_unavailable():
    from reproagent_api.services.evidence_index import build_evidence_index

    admin, run_id, *_ = _seed_final_attempt()
    admin.tables["generated_files"][-1]["sha256"] = None
    item = next(
        item
        for item in build_evidence_index(admin, run_id, actor=USER_ID).items
        if item.category == "code" and item.locator["path"] == "config.yaml"
    )
    assert item.hash is None
    assert item.unavailable_reason


def test_review_version_queries_use_only_real_table_columns(monkeypatch):
    from reproagent_api.services.evidence_index import build_evidence_index

    admin, run_id, *_ = _seed_final_attempt()
    original_table = admin.table
    allowed = {
        "reproduction_specs": {"id", "parent_version_id", "spec_json"},
        "experiment_plans": {"id", "parent_version_id", "changes_json"},
    }

    def checked_table(table, *args, **kwargs):
        query = original_table(table, *args, **kwargs)
        if table in allowed:
            original_select = query.select

            def checked_select(columns, *select_args, **select_kwargs):
                if "parent_version_id" not in columns:
                    return original_select(columns, *select_args, **select_kwargs)
                selected = {column.strip() for column in columns.split(",")}
                assert selected <= allowed[table], (
                    f"{table} selected nonexistent columns: {selected}"
                )
                return original_select(columns, *select_args, **select_kwargs)

            query.select = checked_select
        return query

    monkeypatch.setattr(admin, "table", checked_table)
    build_evidence_index(admin, run_id, actor=USER_ID)


def test_evidence_index_router_masks_foreign_and_missing_runs(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    for run in (run_id, str(uuid4())):
        response = client.get(f"/v1/runs/{run}/evidence-index", headers=BOB_AUTH)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"
    response = client.get(f"/v1/runs/{run_id}/evidence-index", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["run_id"] == run_id


def test_code_evidence_read_uses_final_attempt_pins_not_mutable_projection(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    admin.tables["execution_attempts"].append(
        {
            "id": str(uuid4()),
            "run_id": run_id,
            "attempt": 1,
            "plan_id": str(PLAN_ID),
            "plan_version": 1,
            "code_version": 1,
            "status": "succeeded",
        }
    )
    for path, content in (("old.py", "old"), ("final.py", "final")):
        file_id = str(uuid4())
        admin.tables["generated_files"].append(
            {
                "id": file_id,
                "experiment_id": str(EXPERIMENT_ID),
                "version": 1,
                "path": path,
                "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
        if path == "old.py":
            admin.tables["execution_run_files"].append(
                {"run_id": run_id, "generated_file_id": file_id}
            )
        else:
            admin.tables["execution_attempt_files"].append(
                {"run_id": run_id, "attempt": 1, "generated_file_id": file_id}
            )
    response = client.get(f"/v1/runs/{run_id}/files?attempt=1", headers=AUTH)
    assert response.status_code == 200
    assert [row["path"] for row in response.json()] == ["final.py"]


def _seed_download(admin, run_id, artifact_id, path, content=b"private bytes"):
    admin.tables["execution_artifacts"].append(
        {
            "id": artifact_id,
            "run_id": run_id,
            "artifact_type": "../../metrics.json",
            "storage_path": path,
            "metadata_json": {},
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    admin.storage.files[path] = content


def test_artifact_download_owner_success_foreign_wrong_run_and_missing(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    artifact_id = str(uuid4())
    path = f"{run_id}/attempt-1/artifacts/metrics.json"
    _seed_download(admin, run_id, artifact_id, path)
    url = f"/v1/runs/{run_id}/artifacts/{artifact_id}"
    denied = client.get(url, headers=BOB_AUTH)
    assert denied.status_code == 404
    assert denied.json()["detail"]["code"] == "RUN_NOT_FOUND"
    response = client.get(url, headers=AUTH)
    assert response.status_code == 200
    assert response.content == b"private bytes"
    assert response.headers["cache-control"] == "private, no-store"
    assert "attachment" in response.headers["content-disposition"]
    assert "metrics.json" in response.headers["content-disposition"]
    assert path not in response.text
    assert (
        client.get(f"/v1/runs/{uuid4()}/artifacts/{artifact_id}", headers=AUTH).status_code == 404
    )
    assert client.get(f"/v1/runs/{run_id}/artifacts/{uuid4()}", headers=AUTH).status_code == 404
