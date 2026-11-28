"""Build a provenance-safe evidence index for one owned execution run.

The greatest immutable attempt is authoritative. Mutable Run projections and
earlier attempts are never used to fill gaps in final-attempt evidence.
"""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .review_audit import collect_review_decisions

EvidenceCategory = Literal["paper", "plan", "code", "execution", "review_report"]


class EvidenceIndexItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: EvidenceCategory
    target_type: str
    target_id: str
    locator: dict[str, str | int | float] = Field(min_length=1)
    hash: str | None = None
    unavailable_reason: str | None = None


class EvidenceIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    items: list[EvidenceIndexItem]


def _item(
    category, target_type, target_id, locator, *, hash=None, unavailable_reason=None, suffix=""
):
    return EvidenceIndexItem(
        id=f"{category}:{target_type}:{target_id}:{suffix}",
        category=category,
        target_type=target_type,
        target_id=str(target_id),
        locator={key: value for key, value in locator.items() if value is not None and value != ""},
        hash=hash,
        unavailable_reason=unavailable_reason,
    )


def _missing(category, target_type, run_id, reason, *, attempt=None, suffix=""):
    locator = {"run_id": str(run_id)}
    if attempt is not None:
        locator["attempt"] = attempt
    return _item(category, target_type, run_id, locator, unavailable_reason=reason, suffix=suffix)


def _owned_run(admin, run_id, actor):
    rows = (
        admin.table("execution_runs")
        .select("*, experiments!inner(project_id, paper_id, spec_id, projects!inner(user_id))")
        .eq("id", str(run_id))
        .eq("experiments.projects.user_id", str(actor))
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise PermissionError("Run not found")
    return rows[0]


def _review_event_sources(admin, table, owner_key, owner_id, version_id):
    """Return only IDs literally persisted on the selected immutable version."""
    if not version_id:
        return {}
    event_column = "spec_json" if table == "reproduction_specs" else "changes_json"
    columns = f"id,parent_version_id,{event_column}"
    rows = (
        admin.table(table)
        .select(columns)
        .eq(owner_key, str(owner_id))
        .eq("id", str(version_id))
        .limit(1)
        .execute()
        .data
    )
    sources = {}
    seen = set()
    while rows:
        row = rows[0]
        if row["id"] in seen:
            break
        seen.add(row["id"])
        events = (
            (row.get("spec_json") or {}).get("review_decisions", [])
            if table == "reproduction_specs"
            else row.get("changes_json") or []
        )
        for event in events:
            if isinstance(event, dict) and event.get("id"):
                sources.setdefault(
                    str(event["id"]), "card" if table == "reproduction_specs" else "plan"
                )
        parent = row.get("parent_version_id")
        rows = (
            (
                admin.table(table)
                .select(columns)
                .eq(owner_key, str(owner_id))
                .eq("id", str(parent))
                .limit(1)
                .execute()
                .data
            )
            if parent
            else []
        )
    return sources


def build_evidence_index(admin, run_id: UUID | str, actor: UUID | str) -> EvidenceIndex:
    """Return final-attempt evidence, raising PermissionError for non-owners/not-found."""
    run_id = str(run_id)
    run = _owned_run(admin, run_id, actor)
    experiment_id = run["experiment_id"]
    attempts = (
        admin.table("execution_attempts")
        .select("*")
        .eq("run_id", run_id)
        .order("attempt", desc=True)
        .limit(1)
        .execute()
        .data
    )
    final = attempts[0] if attempts else None
    attempt = final["attempt"] if final else None
    plan_id = final["plan_id"] if final else None
    plan_version = final["plan_version"] if final else None
    code_version = final["code_version"] if final else None
    items = []

    experiment = (
        admin.table("experiments")
        .select("id,paper_id,spec_id")
        .eq("id", experiment_id)
        .limit(1)
        .execute()
        .data
    )
    experiment = experiment[0] if experiment else {}
    papers = (
        (
            admin.table("papers")
            .select("id,file_sha256,source_url")
            .eq("id", experiment.get("paper_id"))
            .limit(1)
            .execute()
            .data
        )
        if experiment.get("paper_id")
        else []
    )
    if papers:
        paper = papers[0]
        locator = {"paper_id": paper["id"]}
        if paper.get("source_url"):
            locator["source_url"] = paper["source_url"]
        items.append(
            _item(
                "paper",
                "paper",
                paper["id"],
                locator,
                hash=paper.get("file_sha256"),
                unavailable_reason=None
                if paper.get("file_sha256")
                else "论文 PDF 的 SHA-256 未持久化",
            )
        )
    else:
        items.append(_missing("paper", "paper", run_id, "运行关联的论文资源不可用"))

    plans = (
        (
            admin.table("experiment_plans")
            .select("id,version,config_json,approved_at")
            .eq("id", plan_id)
            .eq("experiment_id", experiment_id)
            .limit(1)
            .execute()
            .data
        )
        if plan_id
        else []
    )
    if plans and plans[0]["version"] == plan_version:
        plan = plans[0]
        plan_hash = hashlib.sha256(
            json.dumps(
                plan["config_json"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        plan_locator = {
            "plan_id": plan["id"],
            "version": plan["version"],
            "experiment_id": experiment_id,
        }
        if plan.get("approved_at"):
            plan_locator["approved_at"] = plan["approved_at"]
        items.append(
            _item(
                "plan",
                "plan",
                plan["id"],
                plan_locator,
                hash=plan_hash,
                suffix=f"v{plan['version']}",
            )
        )
        items.append(
            _item(
                "plan",
                "approval_trace",
                plan["id"],
                {
                    "plan_id": plan["id"],
                    "version": plan["version"],
                    "experiment_id": experiment_id,
                    "run_id": run_id,
                    "attempt": attempt,
                },
                unavailable_reason=(
                    "计划审批未持久化不可变 trace ID；approved_at 仅作为补充元数据"
                ),
                suffix="approval-trace",
            )
        )
    else:
        items.append(
            _missing("plan", "plan", run_id, "最终尝试绑定的计划版本不可用", attempt=attempt)
        )

    pin_table = "execution_attempt_files" if final else None
    pin_query = (
        admin.table(pin_table).select("generated_file_id").eq("run_id", run_id)
        if final
        else None
    )
    if final:
        pin_query = pin_query.eq("attempt", attempt)
    file_ids = (
        sorted({row["generated_file_id"] for row in pin_query.execute().data}) if final else []
    )
    files = (
        (
            admin.table("generated_files")
            .select("id,path,version,sha256")
            .in_("id", file_ids)
            .execute()
            .data
        )
        if file_ids
        else []
    )
    if len(files) != len(file_ids):
        items.append(
            _missing(
                "code", "generated_file", run_id, "最终尝试 pin 的代码资源不完整", attempt=attempt
            )
        )
    for file in sorted(files, key=lambda row: (row["path"], row["id"])):
        locator = {
            "generated_file_id": file["id"],
            "run_id": run_id,
            "path": file["path"],
            "version": file["version"],
            "code_version": code_version,
        }
        if attempt is not None:
            locator["attempt"] = attempt
        items.append(
            _item(
                "code",
                "generated_file",
                file["id"],
                locator,
                hash=file.get("sha256"),
                unavailable_reason=None if file.get("sha256") else "代码文件未持久化 SHA-256",
            )
        )
    if not file_ids:
        items.append(
            _missing(
                "code", "generated_file", run_id, "最终尝试没有持久化的代码 pin", attempt=attempt
            )
        )

    execution_locator = {"run_id": run_id}
    if code_version is not None:
        execution_locator["code_version"] = code_version
    if final:
        execution_locator["attempt"] = attempt
        if final.get("runner_request_id"):
            execution_locator["runner_request_id"] = final["runner_request_id"]
        if final.get("runner_run_id"):
            execution_locator["runner_run_id"] = final["runner_run_id"]
        items.append(
            _item(
                "execution",
                "run",
                run_id,
                execution_locator,
                suffix=f"attempt-{attempt}",
            )
        )
    else:
        items.append(
            _missing("execution", "run", run_id, "运行没有持久化的最终执行尝试")
        )

    artifact_query = (
        admin.table("execution_artifacts")
        .select("id,artifact_type,attempt,metadata_json")
        .eq("run_id", run_id)
    )
    if final:
        artifact_query = artifact_query.eq("attempt", attempt)
    artifacts = (
        sorted(
            (row for row in artifact_query.execute().data if final or row.get("attempt") is None),
            key=lambda row: (row["artifact_type"], row["id"]),
        )
        if final
        else []
    )
    for artifact in artifacts:
        artifact_name = (
            str(artifact.get("artifact_type") or "artifact")
            .replace("\\", "/")
            .rsplit("/", 1)[-1]
        )
        locator = {
            "artifact_id": artifact["id"],
            "run_id": run_id,
            "name": artifact["artifact_type"],
            "logical_path": f"attempt-{attempt}/artifacts/{artifact_name}",
        }
        if attempt is not None:
            locator["attempt"] = attempt
        items.append(_item("execution", "artifact", artifact["id"], locator))
    if final and not any(row.get("artifact_type") == "metrics.json" for row in artifacts):
        items.append(
            _item(
                "execution",
                "metrics",
                run_id,
                {"run_id": run_id, "attempt": attempt},
                unavailable_reason="最终尝试没有持久化的 metrics.json 产物",
                suffix="metrics",
            )
        )

    audit = run.get("audit_json") or {}
    audit_matches = final is not None and (
        audit.get("code_version") == code_version and audit.get("plan_version") == plan_version
    )
    for field, target_type, label in (
        ("image_digest", "image", "执行镜像 digest"),
        ("dataset_fingerprint", "dataset", "数据集 fingerprint"),
    ):
        value = audit.get(field) if audit_matches else None
        locator = {"run_id": run_id, "audit_field": field}
        if attempt is not None:
            locator["attempt"] = attempt
        items.append(
            _item(
                "execution",
                target_type,
                run_id,
                locator,
                hash=value,
                unavailable_reason=None if value else f"最终尝试未持久化{label}",
                suffix=field,
            )
        )

    reports = (
        admin.table("reproduction_reports")
        .select("*")
        .eq("run_id", run_id)
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    report = reports[0] if reports else None
    report_attempt = report.get("execution_attempt", report.get("attempt")) if report else None
    report_matches = final is not None and report is not None and report_attempt == attempt
    if report:
        markdown = report.get("markdown")
        markdown_hash = (
            hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            if isinstance(markdown, str)
            else None
        )
        unavailable_reason = None
        if not report_matches:
            unavailable_reason = f"最新报告属于尝试 {report_attempt}，不是最终尝试 {attempt}"
        elif markdown_hash is None:
            unavailable_reason = "最新报告缺少 Markdown 内容，无法计算 SHA-256"
        items.append(
            _item(
                "review_report",
                "report",
                report["id"],
                {"report_id": report["id"], "run_id": run_id, "version": report["version"]},
                hash=markdown_hash,
                unavailable_reason=unavailable_reason,
            )
        )
    else:
        items.append(
            _missing("review_report", "report", run_id, "运行尚未生成不可变报告", attempt=attempt)
        )

    review_trace_start = len(items)
    if report_matches and experiment.get("paper_id") and experiment.get("spec_id"):
        sources = _review_event_sources(
            admin,
            "reproduction_specs",
            "paper_id",
            experiment["paper_id"],
            experiment["spec_id"],
        )
        if plan_id:
            sources.update(
                _review_event_sources(
                    admin,
                    "experiment_plans",
                    "experiment_id",
                    experiment_id,
                    plan_id,
                )
            )
        decisions = collect_review_decisions(
            admin,
            paper_id=str(experiment["paper_id"]),
            experiment_id=str(experiment_id),
            spec_id=str(experiment["spec_id"]),
            plan_id=str(plan_id),
        )
        for decision in decisions:
            source = sources.get(str(decision.id))
            if not source:
                continue
            items.append(
                _item(
                    "review_report",
                    "review_trace",
                    decision.id,
                    {
                        "trace_id": str(decision.id),
                        "source": source,
                        "report_id": report["id"],
                        "version": report["version"],
                        "plan_id": str(plan_id),
                    },
                    suffix=source,
                )
            )
    if report_matches:
        trace_items = items[review_trace_start:]
        for source, label in (("card", "Card"), ("plan", "Plan")):
            if any(
                item.target_type == "review_trace"
                and not item.unavailable_reason
                and item.locator.get("source") == source
                for item in trace_items
            ):
                continue
            items.append(
                _item(
                    "review_report",
                    "review_trace",
                    report["id"],
                    {
                        "source": source,
                        "run_id": run_id,
                        "report_id": report["id"],
                        "version": report["version"],
                        "attempt": attempt,
                    },
                    unavailable_reason=f"最终报告没有持久化的 {label} review trace",
                    suffix=f"{source}-unavailable",
                )
            )

    return EvidenceIndex(run_id=run_id, items=items)
