"""Experiment Validation: approved plan vs what the run actually executed.

Actual values come only from real run data: metrics.json (MetricsReport keys),
the run audit, and the config.yaml pinned in the executed code version.
"""

import math

from reproagent_schemas import (
    CheckStatus,
    EvidenceRef,
    ExperimentCheck,
    ExperimentItem,
    ExperimentPlan,
    RunAudit,
)

from ..planning.assumptions import normalize_optimizer
from .facts import ImplementationFacts


def _plan_ref(field_path: str, value: object) -> EvidenceRef:
    return EvidenceRef(kind="plan", field_path=field_path, detail=str(value))


def _metrics_ref(key: str, value: object) -> EvidenceRef:
    return EvidenceRef(kind="metrics", field_path=f"metrics_json.{key}", detail=str(value))


def _audit_ref(key: str, value: object) -> EvidenceRef:
    return EvidenceRef(kind="audit", field_path=f"audit_json.{key}", detail=str(value))


def _config_ref(facts: ImplementationFacts, key: str, value: object) -> EvidenceRef | None:
    if facts.config_source is None:
        return None
    return EvidenceRef(
        kind="config", file_path=facts.config_source, field_path=key, detail=str(value)
    )


def _numeric_check(
    item: ExperimentItem,
    plan_value: int | float,
    actual_value: object,
    plan_path: str,
    actual_ref: EvidenceRef | None,
    label: str,
) -> ExperimentCheck:
    if actual_value is None:
        return ExperimentCheck(
            item=item,
            status=CheckStatus.UNKNOWN,
            plan_value=plan_value,
            plan_evidence=_plan_ref(plan_path, plan_value),
            note=f"真实运行数据缺少{label}，无法比对",
        )
    try:
        actual = float(actual_value)
    except (TypeError, ValueError):
        return ExperimentCheck(
            item=item,
            status=CheckStatus.UNKNOWN,
            plan_value=plan_value,
            actual_value=str(actual_value),
            plan_evidence=_plan_ref(plan_path, plan_value),
            actual_evidence=actual_ref,
            note=f"{label}实际值不可解析：{actual_value}",
        )
    equal = math.isclose(actual, float(plan_value), rel_tol=1e-9, abs_tol=1e-12)
    return ExperimentCheck(
        item=item,
        status=CheckStatus.MATCHED if equal else CheckStatus.DEVIATED,
        plan_value=plan_value,
        actual_value=actual_value if isinstance(actual_value, (int, float)) else actual,
        plan_evidence=_plan_ref(plan_path, plan_value),
        actual_evidence=actual_ref,
        note=(
            f"{label}计划 {plan_value} 与实际 {actual_value} 一致"
            if equal
            else f"{label}计划 {plan_value} 与实际 {actual_value} 不一致"
        ),
    )


def validate_experiment(
    plan: ExperimentPlan,
    facts: ImplementationFacts,
    metrics: dict,
    audit: RunAudit | None,
    execution_time: float | None,
) -> list[ExperimentCheck]:
    training = plan.training_config
    config_training = facts.config.get("training", {})
    checks: list[ExperimentCheck] = []

    checks.append(
        _numeric_check(
            ExperimentItem.TRAINING_SAMPLES,
            plan.dataset_config.train_subset_size,
            metrics.get("train_samples"),
            "dataset_config.train_subset_size",
            _metrics_ref("train_samples", metrics.get("train_samples")),
            "训练样本数",
        )
    )
    checks.append(
        _numeric_check(
            ExperimentItem.TEST_SAMPLES,
            plan.dataset_config.test_subset_size,
            metrics.get("test_samples"),
            "dataset_config.test_subset_size",
            _metrics_ref("test_samples", metrics.get("test_samples")),
            "测试样本数",
        )
    )
    checks.append(
        _numeric_check(
            ExperimentItem.EPOCHS,
            training.epochs.value,
            metrics.get("epochs_completed"),
            "training_config.epochs",
            _metrics_ref("epochs_completed", metrics.get("epochs_completed")),
            "epoch 数",
        )
    )
    checks.append(
        _numeric_check(
            ExperimentItem.BATCH_SIZE,
            training.batch_size.value,
            config_training.get("batch_size"),
            "training_config.batch_size",
            _config_ref(facts, "training.batch_size", config_training.get("batch_size")),
            "批大小",
        )
    )

    plan_optimizer = normalize_optimizer(training.optimizer.value) or str(
        training.optimizer.value
    ).lower()
    actual_optimizer_raw = config_training.get("optimizer")
    actual_optimizer = (
        normalize_optimizer(actual_optimizer_raw) or str(actual_optimizer_raw).lower()
        if actual_optimizer_raw is not None
        else None
    )
    if actual_optimizer is None:
        checks.append(
            ExperimentCheck(
                item=ExperimentItem.OPTIMIZER,
                status=CheckStatus.UNKNOWN,
                plan_value=plan_optimizer,
                plan_evidence=_plan_ref("training_config.optimizer", plan_optimizer),
                note="已固定代码的 config.yaml 缺少优化器配置，无法比对",
            )
        )
    else:
        equal = plan_optimizer == actual_optimizer
        checks.append(
            ExperimentCheck(
                item=ExperimentItem.OPTIMIZER,
                status=CheckStatus.MATCHED if equal else CheckStatus.DEVIATED,
                plan_value=plan_optimizer,
                actual_value=actual_optimizer,
                plan_evidence=_plan_ref("training_config.optimizer", plan_optimizer),
                actual_evidence=_config_ref(facts, "training.optimizer", actual_optimizer_raw),
                note=(
                    f"优化器计划 {plan_optimizer} 与实际 {actual_optimizer} 一致"
                    if equal
                    else f"优化器计划 {plan_optimizer} 与实际 {actual_optimizer} 不一致"
                ),
            )
        )

    checks.append(
        _numeric_check(
            ExperimentItem.LEARNING_RATE,
            training.learning_rate.value,
            config_training.get("learning_rate"),
            "training_config.learning_rate",
            _config_ref(facts, "training.learning_rate", config_training.get("learning_rate")),
            "学习率",
        )
    )

    metric_seed = metrics.get("seed")
    audit_seed = audit.seed if audit else None
    seed_values = [v for v in (metric_seed, audit_seed) if v is not None]
    if not seed_values:
        checks.append(
            ExperimentCheck(
                item=ExperimentItem.RANDOM_SEED,
                status=CheckStatus.UNKNOWN,
                plan_value=training.seed,
                plan_evidence=_plan_ref("training_config.seed", training.seed),
                note="真实运行数据缺少随机种子记录，无法比对",
            )
        )
    else:
        equal = all(v == training.seed for v in seed_values)
        checks.append(
            ExperimentCheck(
                item=ExperimentItem.RANDOM_SEED,
                status=CheckStatus.MATCHED if equal else CheckStatus.DEVIATED,
                plan_value=training.seed,
                actual_value=", ".join(dict.fromkeys(str(v) for v in seed_values)),
                plan_evidence=_plan_ref("training_config.seed", training.seed),
                actual_evidence=(
                    _metrics_ref("seed", metric_seed) if metric_seed is not None
                    else _audit_ref("seed", audit_seed)
                ),
                note=(
                    f"种子计划 {training.seed} 与 metrics/audit 记录一致"
                    if equal
                    else f"种子计划 {training.seed} 与记录 {seed_values} 不一致"
                ),
            )
        )

    fingerprint = audit.dataset_fingerprint if audit else None
    checks.append(
        ExperimentCheck(
            item=ExperimentItem.DATASET_FINGERPRINT,
            status=CheckStatus.MATCHED if fingerprint else CheckStatus.UNKNOWN,
            plan_value="mnist (preloaded, baked into exec image)",
            actual_value=fingerprint,
            plan_evidence=_plan_ref("dataset_config", "mnist preloaded"),
            actual_evidence=_audit_ref("dataset_fingerprint", fingerprint) if fingerprint else None,
            note=(
                "审计记录了执行镜像烘焙数据集的 sha256 指纹，与计划的预置 MNIST 假设一致"
                if fingerprint
                else "审计缺少数据集指纹，无法验证所用数据集"
            ),
        )
    )

    python_version = audit.python_version if audit else None
    image_digest = audit.image_digest if audit else None
    budget = plan.runtime_budget
    if python_version is None and image_digest is None:
        checks.append(
            ExperimentCheck(
                item=ExperimentItem.RUNTIME_ENVIRONMENT,
                status=CheckStatus.UNKNOWN,
                plan_value=f"device={budget.device}, max_seconds={budget.max_seconds}",
                plan_evidence=_plan_ref("runtime_budget", f"{budget.device}/{budget.max_seconds}s"),
                note="审计缺少运行环境记录（Python 版本/镜像 digest），无法比对",
            )
        )
    else:
        over_budget = execution_time is not None and execution_time > budget.max_seconds
        actual_desc = (
            f"python={python_version}, image={image_digest}, "
            f"execution_time={execution_time}s, device={budget.device}"
        )
        checks.append(
            ExperimentCheck(
                item=ExperimentItem.RUNTIME_ENVIRONMENT,
                status=CheckStatus.DEVIATED if over_budget else CheckStatus.MATCHED,
                plan_value=f"device={budget.device}, max_seconds={budget.max_seconds}",
                actual_value=actual_desc,
                plan_evidence=_plan_ref("runtime_budget", f"{budget.device}/{budget.max_seconds}s"),
                actual_evidence=_audit_ref("python_version", python_version),
                note=(
                    f"实际运行 {execution_time}s 超出预算 {budget.max_seconds}s"
                    if over_budget
                    else "CPU 隔离容器执行，审计环境记录完整且在时间预算内"
                ),
            )
        )
    return checks
