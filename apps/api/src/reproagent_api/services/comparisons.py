"""Persistence + orchestration for paired comparisons (API trust domain).

A comparison never re-implements training: each group run reuses the approved
single-group ExperimentPlan pipeline (deterministic codegen -> static validation
-> sandboxed Runner -> persisted metrics/audit). What this module adds is the
pairing discipline:

* one experiment + plan row per (seed, group), pinned to the same code template;
* ``comparison_runs`` records every attempt, so a retry is auditable;
* a failed group is retried alone with the *same* configuration;
* a difference is only produced by the Agent's verifier from observed runs.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from reproagent_schemas import (
    CodegenRequest,
    ComparisonOrigin,
    ComparisonPlan,
    ComparisonReferences,
    ComparisonReportRequest,
    ComparisonRerunRequest,
    ComparisonStatus,
    ErrorCategory,
    ExecImageStatus,
    ExperimentPlan,
    GroupKey,
    ParamSource,
    PendingDecision,
    PendingParam,
    PendingStatus,
    PlanVersionOrigin,
    ReproducibilityFingerprint,
    ReproductionCard,
    RunObservation,
    RunRequest,
    RunResult,
    RunStatus,
    StaticValidateRequest,
    VerifyPairRequest,
)

from .runs import (
    async_persist_result,
    build_run_request,
    execution_request_id,
    finalize_execution_run,
    insert_code_version,
    load_run_row,
    mark_attempt_running,
    persist_runner_run_id,
    pin_run_files,
)
from .versions import insert_versioned

POLL_INTERVAL_SECONDS = 1.0
TERMINAL_RUNS = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED}
TERMINAL_COMPARISONS = {
    ComparisonStatus.COMPLETED.value,
    ComparisonStatus.INCOMPLETE.value,
    ComparisonStatus.FAILED.value,
}
MAX_ATTEMPTS_PER_GROUP = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_plan_row(admin, plan_id: str) -> dict:
    rows = admin.table("comparison_plans").select("*").eq("id", plan_id).execute().data
    if not rows:
        raise ValueError("对照计划不存在")
    return rows[0]


def get_version_row(admin, version_id: str) -> dict:
    rows = (
        admin.table("comparison_plan_versions").select("*").eq("id", version_id).execute().data
    )
    if not rows:
        raise ValueError("对照计划版本不存在")
    return rows[0]


def latest_version(admin, plan_id: str) -> tuple[dict, ComparisonPlan]:
    rows = (
        admin.table("comparison_plan_versions")
        .select("*")
        .eq("comparison_plan_id", plan_id)
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise ValueError("对照计划没有版本")
    return rows[0], ComparisonPlan.model_validate(rows[0]["plan_json"])


def get_comparison(admin, comparison_id: str) -> dict:
    rows = admin.table("comparisons").select("*").eq("id", comparison_id).execute().data
    if not rows:
        raise ValueError("对照任务不存在")
    return rows[0]


def list_versions(admin, plan_id: str) -> list[dict]:
    return (
        admin.table("comparison_plan_versions")
        .select("id,version,origin,approved_at,change_reason,created_at")
        .eq("comparison_plan_id", plan_id)
        .order("version")
        .execute()
        .data
    )


def comparison_runs(admin, comparison_id: str) -> list[dict]:
    rows = (
        admin.table("comparison_runs")
        .select("*")
        .eq("comparison_id", comparison_id)
        .order("created_at")
        .execute()
        .data
    )
    return rows


# --------------------------------------------------------------------------
# Plan lifecycle
# --------------------------------------------------------------------------


def create_plan(
    admin,
    *,
    project_id: str,
    paper_id: str,
    spec_id: str,
    name: str,
    plan: ComparisonPlan,
) -> tuple[dict, dict]:
    plan_row = (
        admin.table("comparison_plans")
        .insert(
            {
                "project_id": project_id,
                "paper_id": paper_id,
                "spec_id": spec_id,
                "name": name[:200],
                "status": "awaiting_confirmation",
            }
        )
        .execute()
        .data[0]
    )
    version_row = insert_versioned(
        lambda: 1,
        lambda version: admin.table("comparison_plan_versions")
        .insert(
            {
                "comparison_plan_id": plan_row["id"],
                "version": version,
                "plan_json": plan.model_dump(mode="json"),
                "origin": PlanVersionOrigin.GENERATED.value,
                "change_reason": "initial comparison plan",
            }
        )
        .execute()
        .data[0],
    )
    return plan_row, version_row


def add_version(
    admin,
    plan_row: dict,
    plan: ComparisonPlan,
    *,
    origin: PlanVersionOrigin,
    change_reason: str,
    parent_version_id: str,
) -> dict:
    get_version_row(admin, parent_version_id)

    def next_version() -> int:
        row, _ = latest_version(admin, plan_row["id"])
        if row["id"] != parent_version_id:
            raise ValueError("对照计划已被更新，请基于最新版本修改")
        return row["version"] + 1

    def insert(version: int) -> dict:
        return (
            admin.table("comparison_plan_versions")
            .insert(
                {
                    "comparison_plan_id": plan_row["id"],
                    "version": version,
                    "plan_json": plan.model_dump(mode="json"),
                    "origin": origin.value,
                    "parent_version_id": parent_version_id,
                    "change_reason": change_reason,
                }
            )
            .execute()
            .data[0]
        )

    return insert_versioned(next_version, insert)


def approve_version(admin, plan_row: dict, version_row: dict, plan: ComparisonPlan) -> dict:
    if not plan.approval_ready():
        raise ValueError("仍有未确认的关键参数，不能批准该对照计划")
    updated = (
        admin.table("comparison_plan_versions")
        .update({"approved_at": _now()})
        .eq("id", version_row["id"])
        .execute()
        .data
    )
    admin.table("comparison_plans").update({"status": "approved"}).eq(
        "id", plan_row["id"]
    ).execute()
    return updated[0] if updated else {**version_row, "approved_at": _now()}


def _param(value, *, source_text: str, quote: str | None = None, page: int | None = None):
    return {
        "value": value,
        "source": ParamSource.HUMAN_CONFIRMED.value,
        "paper_value": None,
        "evidence_quote": quote,
        "source_page": page,
        "rationale": source_text,
        "requires_confirmation": False,
        "human_confirmed": True,
        "confirmed_by": "human",
        "confidence": 1.0,
    }


def _as_number(value, path: str, *, integer: bool) -> float | int:
    """User input is untrusted text: fail with a field-specific message, not a 500."""
    if isinstance(value, bool):
        raise ValueError(f"{path} 需要一个数值，收到布尔值")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{path} 需要一个数值，收到「{value}」") from None
    if number <= 0:
        raise ValueError(f"{path} 必须大于 0，收到「{value}」")
    if integer:
        if number != int(number):
            raise ValueError(f"{path} 需要整数，收到「{value}」")
        return int(number)
    return number


def _clamp_to_authorization(data: dict) -> list[str]:
    """Only ever lower values; never raise them to the ceiling."""
    auth = data["authorization"]
    dataset = data["base_plan"]["dataset_config"]
    training = data["base_plan"]["training_config"]
    budget = data["base_plan"]["runtime_budget"]
    clamped: list[str] = []
    if dataset["train_subset_size"] > auth["max_train_samples"]:
        dataset["train_subset_size"] = auth["max_train_samples"]
        clamped.append("train_subset_size")
    if dataset["test_subset_size"] > auth["max_test_samples"]:
        dataset["test_subset_size"] = auth["max_test_samples"]
        clamped.append("test_subset_size")
    if training["epochs"]["value"] > auth["max_epochs"]:
        training["epochs"]["value"] = auth["max_epochs"]
        clamped.append("epochs")
    if budget["max_seconds"] > auth["max_seconds"]:
        budget["max_seconds"] = auth["max_seconds"]
        clamped.append("max_seconds")
    return clamped


def _apply_resolved(data: dict, item: dict, optimizer_names: frozenset[str]) -> None:
    """Write a resolved pending value back into the concrete plan.

    This is replayed for every already-resolved item on each new version, so
    every branch must be idempotent (overwrite scalars, dedupe lists).
    """
    path = item["field_path"]
    value = item["resolved_value"]
    source_text = f"用户确认：{item['resolved_source']}"
    parts = path.split(".")

    if parts[0] == "groups":
        index = next(
            (i for i, group in enumerate(data["groups"]) if group["key"] == parts[1]), None
        )
        if index is None:
            raise ValueError(f"未知的组：{parts[1]}")
        group = data["groups"][index]
        field = parts[2]
        if field == "optimizer":
            optimizer = str(value).strip().lower()
            if optimizer not in optimizer_names:
                raise ValueError(f"不支持的优化器：{value}")
            group["optimizer"] = _param(optimizer, source_text=source_text)
        elif field == "learning_rate":
            group["learning_rate"] = _param(
                _as_number(value, path, integer=False), source_text=source_text
            )
        elif field == "references":
            reference = {
                "label": str(value).strip(),
                "quote": None,
                "source_page": None,
                "source_section": item["resolved_source"],
                "table_id": None,
                "metric": data["question"]["target_metric"],
                "value": None,
                "condition": None,
            }
            known = {
                (existing["label"], existing.get("source_section"))
                for existing in group["references"]
            }
            if (reference["label"], reference["source_section"]) not in known:
                group["references"] = group["references"] + [reference]
        else:
            raise ValueError(f"不支持的待确认字段：{path}")
        return

    if path in ("train_subset_size", "test_subset_size"):
        data["base_plan"]["dataset_config"][path] = _as_number(value, path, integer=True)
        return
    if path == "epochs":
        data["base_plan"]["training_config"]["epochs"]["value"] = _as_number(
            value, path, integer=True
        )
        return
    if path == "max_seconds":
        data["base_plan"]["runtime_budget"]["max_seconds"] = _as_number(
            value, path, integer=True
        )
        return
    if path == "scaling.authorization":
        # Legacy aggregate item: accepting the suggestion means "stay inside the
        # authority", so clamp every exceeded field down to its ceiling.
        _clamp_to_authorization(data)
        return
    raise ValueError(f"不支持的待确认字段：{path}")


def apply_pending_decisions(
    plan: ComparisonPlan, decisions: list[PendingDecision], optimizer_names: frozenset[str]
) -> ComparisonPlan:
    if not decisions:
        raise ValueError("没有待确认项需要处理")
    data = plan.model_dump(mode="json")
    by_id = {item["id"]: item for item in data["pending"]}
    seen: set[str] = set()
    for decision in decisions:
        if decision.pending_id in seen:
            raise ValueError(f"重复处理待确认项：{decision.pending_id}")
        seen.add(decision.pending_id)
        item = by_id.get(decision.pending_id)
        if item is None:
            raise ValueError(f"未知的待确认项：{decision.pending_id}")
        if item["status"] != PendingStatus.PENDING.value:
            raise ValueError(f"待确认项已处理：{decision.pending_id}")
        if decision.adopt_suggestion:
            if item.get("suggested_value") is None:
                raise ValueError(f"待确认项没有可采用的建议值：{decision.pending_id}")
            item["status"] = PendingStatus.ADOPTED.value
            item["resolved_value"] = item["suggested_value"]
            item["resolved_source"] = item.get("suggestion_source") or "系统建议"
        else:
            item["status"] = PendingStatus.PROVIDED.value
            item["resolved_value"] = decision.value
            item["resolved_source"] = decision.source

    for item in data["pending"]:
        if item["status"] != PendingStatus.PENDING.value:
            _apply_resolved(data, item, optimizer_names)
    return ComparisonPlan.model_validate(data)


def apply_scaling_change(
    plan: ComparisonPlan,
    *,
    train_subset_size: int | None,
    test_subset_size: int | None,
    epochs: int | None,
    batch_size: int | None,
) -> tuple[ComparisonPlan, bool]:
    """Apply scaling changes; each exceeded field becomes its own pending item.

    Per-field items matter: adopting the suggestion must write the ceiling back
    into that exact field, otherwise the plan keeps an out-of-range value while
    the decision log claims it was confirmed.
    """
    data = plan.model_dump(mode="json")
    auth = plan.authorization
    requested: dict[str, tuple[int, int]] = {}
    if train_subset_size is not None:
        requested["train_subset_size"] = (train_subset_size, auth.max_train_samples)
    if test_subset_size is not None:
        requested["test_subset_size"] = (test_subset_size, auth.max_test_samples)
    if epochs is not None:
        requested["epochs"] = (epochs, auth.max_epochs)

    for name, (value, _ceiling) in requested.items():
        if name in ("train_subset_size", "test_subset_size"):
            data["base_plan"]["dataset_config"][name] = value
        else:
            data["base_plan"]["training_config"]["epochs"]["value"] = value
    if batch_size is not None:
        data["base_plan"]["training_config"]["batch_size"]["value"] = batch_size

    beyond = False
    for name, (value, ceiling) in requested.items():
        if value <= ceiling:
            continue
        beyond = True
        # Re-requesting the same field must not accumulate duplicate items.
        data["pending"] = [
            item for item in data["pending"] if item["id"] != f"scaling.{name}"
        ]
        data["pending"].append(
            PendingParam(
                id=f"scaling.{name}",
                field_path=name,
                label=f"缩放参数 {name}",
                reason=f"请求值 {value} 超出授权上限 {ceiling}",
                impact="超出授权会改变原问题的可比条件，需先确认后才会执行",
                suggested_value=ceiling,
                suggestion_source="当前授权上限",
            ).model_dump(mode="json")
        )
    return ComparisonPlan.model_validate(data), beyond


def authorization_violations(plan: ComparisonPlan) -> list[str]:
    """Values that exceed the approved authority; never allowed to execute."""
    auth = plan.authorization
    dataset = plan.base_plan.dataset_config
    training = plan.base_plan.training_config
    violations: list[str] = []
    if dataset.train_subset_size > auth.max_train_samples:
        violations.append(
            f"训练样本 {dataset.train_subset_size} > 授权上限 {auth.max_train_samples}"
        )
    if dataset.test_subset_size > auth.max_test_samples:
        violations.append(
            f"测试样本 {dataset.test_subset_size} > 授权上限 {auth.max_test_samples}"
        )
    if training.epochs.value > auth.max_epochs:
        violations.append(f"训练轮数 {training.epochs.value} > 授权上限 {auth.max_epochs}")
    if plan.base_plan.runtime_budget.max_seconds > auth.max_seconds:
        violations.append(
            f"单次预算 {plan.base_plan.runtime_budget.max_seconds}s > 授权上限 {auth.max_seconds}s"
        )
    if len(plan.seeds) > auth.max_seeds:
        violations.append(f"种子数 {len(plan.seeds)} > 授权上限 {auth.max_seeds}")
    return violations


# --------------------------------------------------------------------------
# Comparison creation
# --------------------------------------------------------------------------


def create_comparison(
    admin,
    *,
    plan_row: dict,
    version_row: dict,
    plan: ComparisonPlan,
    origin: ComparisonOrigin,
    source_comparison_id: str | None,
    idempotency_key: str | None,
) -> dict:
    if idempotency_key:
        existing = _by_idempotency_key(admin, plan_row["id"], idempotency_key)
        if existing:
            return existing
    try:
        return (
            admin.table("comparisons")
            .insert(
                {
                    "project_id": plan_row["project_id"],
                    "plan_id": plan_row["id"],
                    "plan_version_id": version_row["id"],
                    "status": ComparisonStatus.QUEUED.value,
                    "origin": origin.value,
                    "source_comparison_id": source_comparison_id,
                    "idempotency_key": idempotency_key,
                    "requested_pairs": len(plan.seeds),
                    "question": plan.question.question,
                }
            )
            .execute()
            .data[0]
        )
    except Exception:
        # A concurrent identical start can lose the unique-index race; return
        # the winner instead of surfacing a 500.
        if idempotency_key:
            existing = _by_idempotency_key(admin, plan_row["id"], idempotency_key)
            if existing:
                return existing
        raise


def _by_idempotency_key(admin, plan_id: str, key: str) -> dict | None:
    rows = (
        admin.table("comparisons")
        .select("*")
        .eq("plan_id", plan_id)
        .eq("idempotency_key", key)
        .execute()
        .data
    )
    return rows[0] if rows else None


def supersede_open_comparisons(admin, plan_id: str, reason: str) -> None:
    """A changed question leaves the previous comparisons explicitly unfinished."""
    rows = (
        admin.table("comparisons")
        .select("id,status")
        .eq("plan_id", plan_id)
        .execute()
        .data
    )
    for row in rows:
        if row["status"] in TERMINAL_COMPARISONS:
            continue
        admin.table("comparisons").update(
            {"status": ComparisonStatus.INCOMPLETE.value, "error": reason}
        ).eq("id", row["id"]).execute()


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def group_plan(plan: ComparisonPlan, group, seed: int) -> ExperimentPlan:
    base = plan.base_plan
    data = base.model_dump(mode="json")
    data["training_config"]["optimizer"] = group.optimizer.model_dump(mode="json")
    data["training_config"]["learning_rate"] = group.learning_rate.model_dump(mode="json")
    if group.weight_decay is not None:
        data["training_config"]["weight_decay"] = group.weight_decay.model_dump(mode="json")
    data["training_config"]["seed"] = seed
    data["runtime_budget"]["seed"] = seed
    data["evaluation_config"]["target_metric"] = plan.question.target_metric
    data["evaluation_config"]["reported_value"] = (
        group.reported_result.value if group.reported_result else None
    )
    return ExperimentPlan.model_validate(data)


def _observation(
    row: dict, group_key: GroupKey, seed: int, attempt: int
) -> RunObservation:
    metrics = row.get("metrics_json") or {}
    repro = metrics.get("reproducibility") or {}
    audit = row.get("audit_json") or {}
    fingerprint = ReproducibilityFingerprint(
        dataset_split_sha256=repro.get("dataset_split_sha256"),
        init_weights_sha256=repro.get("init_weights_sha256"),
        sample_order_sha256=repro.get("sample_order_sha256"),
        template_sha256=repro.get("template_sha256"),
        seed=repro.get("seed", metrics.get("seed")),
        batch_size=repro.get("batch_size"),
        epochs=repro.get("epochs"),
        train_subset_size=repro.get("train_subset_size", metrics.get("train_samples")),
        test_subset_size=repro.get("test_subset_size", metrics.get("test_samples")),
        loss_function=repro.get("loss_function"),
        optimizer=repro.get("optimizer"),
        learning_rate=repro.get("learning_rate"),
        code_hash=row.get("code_hash") or audit.get("code_hash"),
        image_digest=audit.get("image_digest"),
        dataset_fingerprint=audit.get("dataset_fingerprint"),
    )
    return RunObservation(
        run_id=UUID(row["id"]),
        group_key=group_key,
        seed=seed,
        status=RunStatus(row["status"]),
        attempt=attempt,
        correct_count=metrics.get("correct_count"),
        evaluated_count=metrics.get("evaluated_count"),
        metric_value=metrics.get("metric_value"),
        accuracy=metrics.get("accuracy"),
        test_loss=metrics.get("test_loss"),
        fingerprint=fingerprint,
        error=row.get("error"),
    )


async def _poll(runner, runner_run_id: UUID) -> RunResult:
    for _ in range(700):
        result = await runner.get_run(runner_run_id)
        if result.status in TERMINAL_RUNS:
            return result
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    try:
        await runner.cancel_run(runner_run_id)
    except Exception:
        pass
    return RunResult(
        run_id=runner_run_id,
        status=RunStatus.TIMEOUT,
        error="Runner polling budget exhausted; execution was cancelled",
        error_category=ErrorCategory.TIMEOUT,
    )


def _result_from_row(run_id: UUID, row: dict) -> RunResult:
    return RunResult(
        run_id=run_id,
        status=RunStatus(row["status"]),
        error=row.get("error"),
    )


def _finalize_cancelled(admin, run_id: str, fallback: dict) -> None:
    current = load_run_row(admin, run_id) or fallback
    if current["status"] not in (
        RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value,
    ):
        _finalize_execution(
            admin,
            current,
            RunResult(run_id=UUID(run_id), status=RunStatus.CANCELLED),
        )


async def _cancel_runner(runner, identity: UUID | None) -> None:
    if identity is None:
        return
    try:
        await runner.cancel_run(identity)
    except Exception:
        pass


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


async def _execute_attempt(
    admin,
    agent,
    runner,
    *,
    project_id: str,
    paper_id: str,
    comparison_id: str,
    plan: ComparisonPlan,
    group,
    seed: int,
    attempt: int,
    card,
    spec_id: str,
) -> tuple[UUID, RunResult, str]:
    """One attempt of one group.

    The ``comparison_runs`` row is written *before* the Runner is asked to
    train, so a crash mid-run leaves an auditable attempt instead of an
    invisible one; any infrastructure error finalizes the run row so no
    execution is left stuck in ``running`` forever.
    """
    experiment_id = uuid4()
    admin.table("experiments").insert(
        {
            "id": str(experiment_id),
            "project_id": project_id,
            "paper_id": paper_id,
            "spec_id": spec_id,
            "name": f"对照 {comparison_id[:8]} · {group.label} · seed {seed}"[:200],
            "status": "running",
        }
    ).execute()
    plan_for_group = group_plan(plan, group, seed)
    plan_row = (
        admin.table("experiment_plans")
        .insert(
            {
                "experiment_id": str(experiment_id),
                "version": 1,
                "config_json": plan_for_group.model_dump(mode="json"),
                "budget_json": plan_for_group.runtime_budget.model_dump(mode="json"),
                "origin": "generated",
                "change_reason": f"comparison {comparison_id} seed {seed} group {group.key}",
                "approved_at": _now(),
            }
        )
        .execute()
        .data[0]
    )
    run_id = uuid4()
    admin.table("execution_runs").insert(
        {
            "id": str(run_id),
            "experiment_id": str(experiment_id),
            "plan_id": plan_row["id"],
            "code_version": 0,
            "plan_version": 1,
            "status": RunStatus.QUEUED.value,
            "repair_attempt": 0,
        }
    ).execute()
    admin.table("comparison_runs").insert(
        {
            "comparison_id": comparison_id,
            "seed": seed,
            "group_key": group.key.value,
            "run_id": str(run_id),
            "attempt": attempt,
        }
    ).execute()

    def fail(error: str) -> tuple[UUID, RunResult, str]:
        result = RunResult(run_id=run_id, status=RunStatus.FAILED, error=error)
        _finalize_execution(
            admin,
            {"id": str(run_id), "experiment_id": str(experiment_id)},
            result,
            error,
        )
        return run_id, result, str(experiment_id)

    request: RunRequest | None = None
    runner_run_id: UUID | None = None
    result: RunResult | None = None
    try:
        bundle = (await agent.codegen(CodegenRequest(card=card, plan=plan_for_group))).bundle
        validation = await agent.validate_code(StaticValidateRequest(bundle=bundle))
        if not validation.ok:
            issues = "; ".join(issue.message for issue in validation.issues)
            return fail(f"static validation failed: {issues}")

        code_version, file_ids = insert_code_version(
            admin,
            str(experiment_id),
            1,
            bundle,
            f"comparison seed {seed} group {group.key.value}",
        )
        pin_run_files(admin, str(run_id), file_ids, attempt=1)
        request = build_run_request(
            experiment_id,
            UUID(plan_row["id"]),
            bundle,
            plan_for_group,
            code_version,
            1,
            run_id,
            0,
        )
        claimed = mark_attempt_running(
            admin,
            load_run_row(admin, str(run_id)),
            attempt=0,
            code_version=code_version,
            code_hash=bundle.code_hash,
            plan_id=plan_row["id"],
            plan_version=1,
            request_json=request.model_dump(mode="json"),
        )
        if claimed is None:
            current = load_run_row(admin, str(run_id))
            if current and current["status"] in (
                RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value, RunStatus.TIMEOUT.value,
            ):
                return run_id, _result_from_row(run_id, current), str(experiment_id)
            return fail("Execution attempt ownership was lost")

        runner_run_id = UUID(claimed["runner_run_id"]) if claimed.get("runner_run_id") else None
        if runner_run_id is None:
            current = load_run_row(admin, str(run_id))
            if current["status"] in (
                RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value,
            ):
                return run_id, _result_from_row(run_id, current), str(experiment_id)
            accepted = await runner.start_run(request)
            runner_run_id = accepted.run_id
            if not persist_runner_run_id(admin, str(run_id), runner_run_id, 0):
                current = load_run_row(admin, str(run_id))
                if current["status"] != RunStatus.SUCCEEDED.value:
                    await _cancel_runner(runner, runner_run_id)
                return run_id, _result_from_row(run_id, current), str(experiment_id)
        result = await _poll(runner, runner_run_id)
        await async_persist_result(admin, str(run_id), runner_run_id, result, runner, 0)
        _finalize_execution(
            admin, {"id": str(run_id), "experiment_id": str(experiment_id)}, result
        )
        return run_id, result, str(experiment_id)
    except asyncio.CancelledError:
        await _cancel_runner(runner, runner_run_id or (request.request_id if request else None))
        _finalize_cancelled(
            admin,
            str(run_id),
            {"id": str(run_id), "experiment_id": str(experiment_id)},
        )
        raise
    except Exception as exc:  # noqa: BLE001 - never leave a run stuck in running
        await _cancel_runner(runner, runner_run_id or (request.request_id if request else None))
        current = load_run_row(admin, str(run_id))
        if current and current["status"] in (
            RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value,
        ):
            return run_id, _result_from_row(run_id, current), str(experiment_id)
        if result is not None and result.status in TERMINAL_RUNS:
            # The Runner's real terminal outcome remains authoritative even if
            # storing its audit data failed.
            _finalize_execution(
                admin,
                current or {"id": str(run_id), "experiment_id": str(experiment_id)},
                result,
            )
            return run_id, result, str(experiment_id)
        return fail(f"执行失败：{exc}")


def _finalize_execution(admin, run_row: dict, result: RunResult, error: str | None = None) -> None:
    # The RPC locks the row, guards its attempt and latest-run position, and
    # serializes the experiment mirror. Direct status writes can resurrect a
    # cancelled/succeeded run or overwrite a newer repair/rerun.
    finalize_execution_run(
        admin,
        run_row["id"],
        result.status,
        error if error is not None else result.error,
        attempt=run_row.get("repair_attempt", 0),
        require_latest=True,
    )


def _fetch_run(admin, run_id: str) -> dict | None:
    rows = (
        admin.table("execution_runs")
        .select("id,experiment_id,status,runner_run_id,repair_attempt,created_at")
        .eq("id", run_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


async def _resume_run(admin, runner, run_row: dict) -> RunResult | None:
    """Re-attach to the stable Runner identity left by a crashed driver.

    The API persists the deterministic request before dispatch. If the response
    was lost, probing that identity recovers the accepted container instead of
    generating code or starting a second run. Confirmed absence fails this
    attempt deliberately; uncertain transport failures cancel the identity
    before the caller is allowed to retry.
    """
    run_uuid = UUID(run_row["id"])
    current = load_run_row(admin, str(run_uuid))
    if current and current["status"] in (
        RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value,
    ):
        return _result_from_row(run_uuid, current)
    current = current or run_row
    attempt = current.get("repair_attempt", 0)
    runner_run_id = UUID(current["runner_run_id"]) if current.get("runner_run_id") else None
    discovered: RunResult | None = None
    if runner_run_id is None:
        deterministic_id = execution_request_id(run_uuid, attempt)
        try:
            discovered = await runner.get_run(deterministic_id)
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found(exc):
                await _cancel_runner(runner, deterministic_id)
            failed = RunResult(
                run_id=run_uuid,
                status=RunStatus.FAILED,
                error=f"无法恢复运行：缺少 Runner 运行标识（驱动在启动阶段中断）：{exc}",
            )
            _finalize_execution(admin, current, failed)
            return failed
        if not persist_runner_run_id(admin, str(run_uuid), deterministic_id, attempt):
            current = load_run_row(admin, str(run_uuid)) or current
            if current["status"] != RunStatus.SUCCEEDED.value:
                await _cancel_runner(runner, deterministic_id)
            return _result_from_row(run_uuid, current)
        runner_run_id = deterministic_id

    try:
        result = (
            discovered
            if discovered is not None and discovered.status in TERMINAL_RUNS
            else await _poll(runner, runner_run_id)
        )
    except asyncio.CancelledError:
        await _cancel_runner(runner, runner_run_id)
        _finalize_cancelled(admin, str(run_uuid), current)
        raise
    except Exception as exc:  # noqa: BLE001
        await _cancel_runner(runner, runner_run_id)
        current = load_run_row(admin, str(run_uuid)) or current
        if current["status"] in (
            RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value,
        ):
            return _result_from_row(run_uuid, current)
        failed = RunResult(
            run_id=run_uuid,
            status=RunStatus.FAILED,
            error=f"无法恢复运行（Runner 记录已失效）：{exc}",
        )
        _finalize_execution(admin, current, failed)
        return failed

    try:
        await async_persist_result(admin, str(run_uuid), runner_run_id, result, runner, attempt)
    except asyncio.CancelledError:
        await _cancel_runner(runner, runner_run_id)
        _finalize_cancelled(admin, str(run_uuid), current)
        raise
    except Exception:  # noqa: BLE001 - the polled terminal result is authoritative
        current = load_run_row(admin, str(run_uuid)) or current
        if current["status"] in (
            RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value,
        ):
            return _result_from_row(run_uuid, current)
    _finalize_execution(admin, current, result)
    return result


async def _run_group(
    admin,
    agent,
    runner,
    *,
    comparison: dict,
    paper_id: str,
    plan: ComparisonPlan,
    group,
    seed: int,
    existing: list[dict],
    card,
    spec_id: str,
) -> list[dict]:
    """Return the authoritative attempt rows for one (seed, group)."""
    prior = [
        row for row in existing if row["seed"] == seed and row["group_key"] == group.key.value
    ]
    for row in sorted(prior, key=lambda item: item["attempt"], reverse=True):
        run_row = _fetch_run(admin, row["run_id"])
        if run_row is None:
            continue
        if run_row["status"] in (RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value):
            return prior
        if run_row["status"] in (
            RunStatus.QUEUED.value, RunStatus.DISPATCHING.value, RunStatus.RUNNING.value,
        ):
            result = await _resume_run(admin, runner, run_row)
            if result is not None and result.status in (RunStatus.SUCCEEDED, RunStatus.CANCELLED):
                return prior

    attempt = max((row["attempt"] for row in prior), default=0) + 1
    created: list[dict] = []
    while attempt <= MAX_ATTEMPTS_PER_GROUP:
        run_id, result, experiment_id = await _execute_attempt(
            admin,
            agent,
            runner,
            project_id=comparison["project_id"],
            paper_id=paper_id,
            comparison_id=comparison["id"],
            plan=plan,
            group=group,
            seed=seed,
            attempt=attempt,
            card=card,
            spec_id=spec_id,
        )
        created.append(
            {
                "comparison_id": comparison["id"],
                "seed": seed,
                "group_key": group.key.value,
                "run_id": str(run_id),
                "attempt": attempt,
                "experiment_id": experiment_id,
            }
        )
        if result.status in (RunStatus.SUCCEEDED, RunStatus.CANCELLED):
            break
        attempt += 1
    return prior + created


async def execute_comparison(admin, agent, runner, comparison_id: str) -> None:
    comparison = get_comparison(admin, comparison_id)
    if comparison["status"] in TERMINAL_COMPARISONS:
        return
    if comparison["status"] not in (
        ComparisonStatus.QUEUED.value,
        ComparisonStatus.RUNNING.value,
    ):
        raise ValueError("对照任务尚未批准，不能执行")
    version_row = get_version_row(admin, comparison["plan_version_id"])
    if not version_row.get("approved_at"):
        raise ValueError("对照计划版本尚未批准")
    plan = ComparisonPlan.model_validate(version_row["plan_json"])
    violations = authorization_violations(plan)
    if violations:
        admin.table("comparisons").update(
            {
                "status": ComparisonStatus.FAILED.value,
                "error": "计划超出授权范围：" + "；".join(violations),
            }
        ).eq("id", comparison_id).in_(
            "status", [ComparisonStatus.QUEUED.value, ComparisonStatus.RUNNING.value]
        ).execute()
        return
    plan_row = get_plan_row(admin, comparison["plan_id"])
    spec_id = plan_row["spec_id"]
    _, card = load_pinned_card(
        admin, comparison["project_id"], plan_row["paper_id"], spec_id
    )

    # Preflight: never start a pair whose pinned image is gone -- the failure
    # would otherwise surface as an opaque container error after minutes.
    try:
        image = await runner.image_status()
    except Exception:
        image = None
    if image is not None and not image.available:
        admin.table("comparisons").update(
            {
                "status": ComparisonStatus.FAILED.value,
                "error": f"执行镜像不可用：{image.error or image.image}",
            }
        ).eq("id", comparison_id).in_(
            "status", [ComparisonStatus.QUEUED.value, ComparisonStatus.RUNNING.value]
        ).execute()
        return

    admin.table("comparisons").update({"status": ComparisonStatus.RUNNING.value}).eq(
        "id", comparison_id
    ).in_(
        "status", [ComparisonStatus.QUEUED.value, ComparisonStatus.RUNNING.value]
    ).execute()

    current = get_comparison(admin, comparison_id)
    if current["status"] not in (
        ComparisonStatus.QUEUED.value, ComparisonStatus.RUNNING.value,
    ):
        return

    existing = comparison_runs(admin, comparison_id)
    try:
        active = True
        for seed in plan.seeds:
            for group in plan.groups:
                updated = await _run_group(
                    admin,
                    agent,
                    runner,
                    comparison=comparison,
                    paper_id=plan_row["paper_id"],
                    plan=plan,
                    group=group,
                    seed=seed,
                    existing=existing,
                    card=card,
                    spec_id=spec_id,
                )
                kept = [
                    row
                    for row in existing
                    if not (
                        row["seed"] == seed and row["group_key"] == group.key.value
                    )
                ]
                existing = kept + updated
                current = get_comparison(admin, comparison_id)
                active = current["status"] in (
                    ComparisonStatus.QUEUED.value, ComparisonStatus.RUNNING.value,
                )
                if not active:
                    break
            if not active:
                break
    except Exception as exc:  # infrastructure failure: never fake a result
        admin.table("comparisons").update(
            {"status": ComparisonStatus.FAILED.value, "error": str(exc)[:1000]}
        ).eq("id", comparison_id).in_(
            "status", [ComparisonStatus.QUEUED.value, ComparisonStatus.RUNNING.value]
        ).execute()
        current = get_comparison(admin, comparison_id)
        if current["status"] != ComparisonStatus.INCOMPLETE.value:
            return

    observations = []
    for row in existing:
        runs = (
            admin.table("execution_runs")
            .select("id,status,metrics_json,audit_json,code_hash,error")
            .eq("id", row["run_id"])
            .execute()
            .data
        )
        if not runs:
            continue
        observations.append(
            _observation(runs[0], GroupKey(row["group_key"]), row["seed"], row["attempt"])
        )

    outcome = (
        await agent.comparison_verify(
            VerifyPairRequest(
                plan=plan, observations=observations, requested_pairs=len(plan.seeds)
            )
        )
    ).outcome

    run_ids: list[UUID] = []
    code_hashes: set[str] = set()
    image_digest = None
    for row in existing:
        run_ids.append(UUID(row["run_id"]))
        runs = (
            admin.table("execution_runs")
            .select("code_hash,audit_json")
            .eq("id", row["run_id"])
            .execute()
            .data
        )
        if not runs:
            continue
        if runs[0].get("code_hash"):
            code_hashes.add(runs[0]["code_hash"])
        digest = (runs[0].get("audit_json") or {}).get("image_digest")
        image_digest = image_digest or digest

    references = ComparisonReferences(
        comparison_id=UUID(comparison_id),
        plan_id=UUID(plan_row["id"]),
        plan_version_id=UUID(version_row["id"]),
        plan_version=version_row["version"],
        paper_id=UUID(plan_row["paper_id"]),
        spec_version_id=UUID(spec_id),
        spec_version=_spec_version(admin, spec_id),
        run_ids=run_ids,
        code_hashes=sorted(code_hashes),
        image_digest=image_digest,
    )
    paper_title = _paper_title(admin, plan_row["paper_id"])

    # A question change while this pair was in flight must not be overwritten:
    # the old question stays explicitly unfinished, with its report kept.
    current = get_comparison(admin, comparison_id)
    superseded = current["status"] == ComparisonStatus.INCOMPLETE.value
    notes = [current["error"]] if superseded and current.get("error") else []

    report = await agent.comparison_report(
        ComparisonReportRequest(
            references=references,
            plan=plan,
            outcome=outcome,
            paper_title=paper_title,
            review_notes=notes,
        )
    )
    final_status = report.report.final_status
    _upsert_report(
        admin, comparison_id, report.report.model_dump(mode="json"), report.markdown, final_status
    )
    summary = outcome.model_dump(mode="json")
    payload: dict = {
        "completed_pairs": outcome.completed_pairs,
        "requested_pairs": max(outcome.requested_pairs, len(plan.seeds)),
        "summary_json": summary,
    }
    if not superseded:
        payload["status"] = final_status.value
        payload["error"] = None
    query = admin.table("comparisons").update(payload).eq("id", comparison_id)
    if superseded:
        query = query.eq("status", ComparisonStatus.INCOMPLETE.value)
    else:
        query = query.in_(
            "status", [ComparisonStatus.QUEUED.value, ComparisonStatus.RUNNING.value]
        )
    query.execute()


def _comparison_image_digest(admin, comparison_id: str) -> str | None:
    """The digest actually observed by the original runs, not a stored claim."""
    for row in comparison_runs(admin, comparison_id):
        runs = (
            admin.table("execution_runs")
            .select("audit_json")
            .eq("id", row["run_id"])
            .execute()
            .data
        )
        if not runs:
            continue
        digest = (runs[0].get("audit_json") or {}).get("image_digest")
        if digest:
            return digest
    return None


def _spec_version(admin, spec_id: str) -> int:
    rows = (
        admin.table("reproduction_specs").select("version").eq("id", spec_id).execute().data
    )
    return rows[0]["version"] if rows else 1


def _paper_title(admin, paper_id: str) -> str | None:
    rows = admin.table("papers").select("title").eq("id", paper_id).execute().data
    return rows[0]["title"] if rows else None


def load_pinned_card(admin, project_id: str, paper_id: str, spec_id: str):
    """Load the Card version a comparison was approved against.

    A newer confirmed Card version supersedes the older one, but historical
    comparisons and reruns must keep using the version they pinned -- otherwise
    a rerun would silently answer a different question.
    """
    papers = (
        admin.table("papers")
        .select("id")
        .eq("id", paper_id)
        .eq("project_id", project_id)
        .execute()
        .data
    )
    if not papers:
        raise ValueError("论文不属于该项目")
    rows = (
        admin.table("reproduction_specs")
        .select("*")
        .eq("id", spec_id)
        .eq("paper_id", paper_id)
        .execute()
        .data
    )
    if not rows:
        raise ValueError("对照计划固定的 Card 版本不存在")
    row = rows[0]
    if row.get("status") not in ("confirmed", "superseded"):
        raise ValueError("对照计划固定的 Card 版本尚未确认")
    return row, ReproductionCard.model_validate(row["spec_json"])


def _upsert_report(admin, comparison_id: str, report_json: dict, markdown: str, status) -> None:
    existing = (
        admin.table("comparison_reports")
        .select("id")
        .eq("comparison_id", comparison_id)
        .execute()
        .data
    )
    payload = {
        "comparison_id": comparison_id,
        "report_json": report_json,
        "markdown": markdown,
        "status": "complete" if status is ComparisonStatus.COMPLETED else "incomplete",
    }
    if existing:
        admin.table("comparison_reports").update(payload).eq(
            "comparison_id", comparison_id
        ).execute()
    else:
        admin.table("comparison_reports").insert(payload).execute()


async def rerun_comparison(
    admin,
    runner,
    comparison_id: str,
    request: ComparisonRerunRequest,
) -> tuple[dict, ExecImageStatus, list[str]]:
    """Create a new comparison on the same approved version (or a new env)."""
    comparison = get_comparison(admin, comparison_id)
    plan_row = get_plan_row(admin, comparison["plan_id"])
    version_row = get_version_row(admin, comparison["plan_version_id"])
    plan = ComparisonPlan.model_validate(version_row["plan_json"])
    missing: list[str] = []
    status = await runner.image_status()
    previous_digest = _comparison_image_digest(admin, comparison_id)
    if not status.available:
        missing.append(f"执行镜像 {status.image} 不可用")
    elif previous_digest and status.digest and status.digest != previous_digest:
        missing.append(
            f"镜像摘要已变化（原 {previous_digest[:12]} → 现 {status.digest[:12]}）"
        )
    if missing and not request.use_current_environment:
        raise ValueError("原环境不可用：" + "；".join(missing) + "；可另建使用当前环境的对照")
    origin = (
        ComparisonOrigin.RERUN_CURRENT_ENV
        if request.use_current_environment
        else ComparisonOrigin.RERUN
    )
    if request.use_current_environment and missing:
        version_row = add_version(
            admin,
            plan_row,
            plan,
            origin=PlanVersionOrigin.RERUN,
            change_reason=request.reason,
            parent_version_id=version_row["id"],
        )
        admin.table("comparison_plan_versions").update({"approved_at": _now()}).eq(
            "id", version_row["id"]
        ).execute()
    new_row = create_comparison(
        admin,
        plan_row=plan_row,
        version_row=version_row,
        plan=plan,
        origin=origin,
        source_comparison_id=comparison_id,
        idempotency_key=None,
    )
    return new_row, status, missing
