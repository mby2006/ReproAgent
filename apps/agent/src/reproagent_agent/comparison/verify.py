"""Pair verification and the only honest way to produce a difference.

Two rules are enforced here, not in the UI:

1. Accuracy is recomputed as ``correct_count / evaluated_count`` from the raw
   counts each run wrote. The training script's own ``metric_value`` is recorded
   for cross-checking but never becomes the reported number.
2. A difference is computed only when both runs succeeded AND every shared
   condition was observed to match (split, initial weights, sample order, budget,
   evaluation settings, code). A missing fingerprint is UNKNOWN, not "fine".
"""

from collections import defaultdict

from reproagent_schemas import (
    CheckStatus,
    ComparisonOutcome,
    ComparisonPlan,
    ComparisonStatus,
    GroupKey,
    PairCheckItem,
    PairItem,
    PairVerification,
    RunObservation,
    RunStatus,
    SeedComparison,
)

_DIRECTION_LABEL = {
    "a_higher": "A 组更高",
    "b_higher": "B 组更高",
    "equal": "两组相同",
}


def _code_hash(obs: RunObservation) -> str | None:
    """Template identity, not the aggregate code hash.

    The aggregate hash covers config.yaml, which intentionally differs between
    groups (that is the factor under test). What must be identical is the
    executable template itself.
    """
    if obs.fingerprint.template_sha256:
        return obs.fingerprint.template_sha256
    return None


def _recompute(obs: RunObservation | None) -> float | None:
    if obs is None or obs.correct_count is None or not obs.evaluated_count:
        return None
    if obs.evaluated_count <= 0:
        return None
    return obs.correct_count / obs.evaluated_count


def _check(
    item: PairItem,
    left: str | None,
    right: str | None,
    *,
    label: str,
) -> PairCheckItem:
    if not left or not right:
        return PairCheckItem(
            item=item,
            status=CheckStatus.UNKNOWN,
            group_a=left,
            group_b=right,
            note=f"{label}：至少一组没有记录可核验摘要",
        )
    if left == right:
        return PairCheckItem(
            item=item, status=CheckStatus.MATCHED, group_a=left, group_b=right,
            note=f"{label}：两组一致",
        )
    return PairCheckItem(
        item=item, status=CheckStatus.DEVIATED, group_a=left, group_b=right,
        note=f"{label}：两组不一致，配对条件被破坏",
    )


def _pair_checks(a: RunObservation, b: RunObservation) -> list[PairCheckItem]:
    fa, fb = a.fingerprint, b.fingerprint
    checks = [
        _check(PairItem.DATASET_SPLIT, fa.dataset_split_sha256, fb.dataset_split_sha256,
               label="数据划分"),
        _check(PairItem.INITIAL_WEIGHTS, fa.init_weights_sha256, fb.init_weights_sha256,
               label="初始权重"),
        _check(PairItem.SAMPLE_ORDER, fa.sample_order_sha256, fb.sample_order_sha256,
               label="样本顺序"),
        _check(PairItem.CODE, _code_hash(a), _code_hash(b), label="代码版本"),
    ]
    budget_pairs = (
        ("seed", fa.seed, fb.seed),
        ("batch_size", fa.batch_size, fb.batch_size),
        ("epochs", fa.epochs, fb.epochs),
        ("train_subset_size", fa.train_subset_size, fb.train_subset_size),
    )
    missing = [name for name, left, right in budget_pairs if left is None or right is None]
    mismatched = [
        f"{name}: {left!r} vs {right!r}" for name, left, right in budget_pairs if left != right
    ]
    checks.append(
        PairCheckItem(
            item=PairItem.TRAINING_BUDGET,
            status=(
                CheckStatus.UNKNOWN if missing
                else CheckStatus.DEVIATED if mismatched
                else CheckStatus.MATCHED
            ),
            group_a=str(fa.batch_size),
            group_b=str(fb.batch_size),
            note=(
                f"训练预算缺少可核验字段：{', '.join(missing)}" if missing
                else "训练预算：" + "；".join(mismatched) if mismatched
                else f"训练预算一致（seed {fa.seed}、batch {fa.batch_size}、epochs {fa.epochs}）"
            ),
        )
    )
    evaluation_mismatch = []
    if fa.test_subset_size is None or fb.test_subset_size is None:
        evaluation_mismatch.append("测试样本数缺失")
    elif fa.test_subset_size != fb.test_subset_size:
        evaluation_mismatch.append(
            f"测试样本数 {fa.test_subset_size} vs {fb.test_subset_size}"
        )
    if fa.loss_function is None or fb.loss_function is None:
        evaluation_mismatch.append("损失函数缺失")
    elif fa.loss_function != fb.loss_function:
        evaluation_mismatch.append(f"损失函数 {fa.loss_function} vs {fb.loss_function}")
    evaluation_unknown = any("缺失" in item for item in evaluation_mismatch)
    checks.append(
        PairCheckItem(
            item=PairItem.EVALUATION,
            status=(
                CheckStatus.UNKNOWN if evaluation_unknown
                else CheckStatus.DEVIATED if evaluation_mismatch
                else CheckStatus.MATCHED
            ),
            group_a=str(fa.test_subset_size),
            group_b=str(fb.test_subset_size),
            note=(
                "评估条件：" + "；".join(evaluation_mismatch) if evaluation_mismatch
                else f"评估条件一致（{fa.test_subset_size} 张测试图）"
            ),
        )
    )
    return checks


def _best_attempt(observations: list[RunObservation]) -> RunObservation | None:
    if not observations:
        return None
    succeeded = [o for o in observations if o.status is RunStatus.SUCCEEDED]
    pool = succeeded or observations
    return max(pool, key=lambda o: o.attempt)


def _seed_comparison(
    seed: int, a: RunObservation | None, b: RunObservation | None
) -> SeedComparison:
    blockers: list[str] = []
    for label, obs in (("A 组", a), ("B 组", b)):
        if obs is None:
            blockers.append(f"{label}没有运行记录")
        elif obs.status is not RunStatus.SUCCEEDED:
            detail = f"：{obs.error}" if obs.error else ""
            blockers.append(f"{label}运行未成功（{obs.status.value}）{detail}")
        elif obs.correct_count is None or not obs.evaluated_count:
            blockers.append(f"{label}缺少正确样本数或测试样本数，无法重算准确率")

    verification: PairVerification | None = None
    if a is not None and b is not None:
        checks = _pair_checks(a, b)
        failing = [
            c for c in checks if c.status is not CheckStatus.MATCHED
        ]
        verification = PairVerification(
            seed=seed,
            verified=not failing and not blockers,
            checks=checks,
            blocking=[c.note for c in failing],
        )
        blockers.extend(c.note for c in failing)

    accuracy_a = _recompute(a) if a is not None and a.status is RunStatus.SUCCEEDED else None
    accuracy_b = _recompute(b) if b is not None and b.status is RunStatus.SUCCEEDED else None
    delta = None
    direction = None
    complete = bool(
        verification and verification.verified
        and accuracy_a is not None and accuracy_b is not None
    )
    if complete:
        delta = accuracy_a - accuracy_b
        if abs(delta) < 1e-12:
            direction = "equal"
        else:
            direction = "a_higher" if delta > 0 else "b_higher"
    return SeedComparison(
        seed=seed,
        group_a=a,
        group_b=b,
        recomputed_accuracy_a=accuracy_a,
        recomputed_accuracy_b=accuracy_b,
        delta=delta,
        delta_percentage_points=delta * 100 if delta is not None else None,
        direction=direction,
        verification=verification,
        complete=complete,
        blockers=list(dict.fromkeys(blockers)),
    )


def verify_pairs(
    plan: ComparisonPlan,
    observations: list[RunObservation],
    requested_pairs: int,
) -> ComparisonOutcome:
    grouped: dict[tuple[int, GroupKey], list[RunObservation]] = defaultdict(list)
    for obs in observations:
        grouped[(obs.seed, obs.group_key)].append(obs)

    seeds = plan.seeds
    comparisons: list[SeedComparison] = []
    for seed in seeds:
        comparisons.append(
            _seed_comparison(
                seed,
                _best_attempt(grouped.get((seed, GroupKey.A), [])),
                _best_attempt(grouped.get((seed, GroupKey.B), [])),
            )
        )

    verified = [c for c in comparisons if c.complete]
    complete_a = [c.recomputed_accuracy_a for c in verified if c.recomputed_accuracy_a is not None]
    complete_b = [c.recomputed_accuracy_b for c in verified if c.recomputed_accuracy_b is not None]
    mean_a = sum(complete_a) / len(complete_a) if complete_a else None
    mean_b = sum(complete_b) / len(complete_b) if complete_b else None
    mean_delta = mean_a - mean_b if mean_a is not None and mean_b is not None else None
    direction = None
    if mean_delta is not None:
        direction = "equal" if abs(mean_delta) < 1e-12 else (
            "a_higher" if mean_delta > 0 else "b_higher"
        )

    any_success = any(
        obs.status is RunStatus.SUCCEEDED
        for observations in grouped.values()
        for obs in observations
    )
    if not observations:
        status = ComparisonStatus.FAILED
    elif len(verified) == len(seeds):
        status = ComparisonStatus.COMPLETED
    elif any_success:
        status = ComparisonStatus.INCOMPLETE
    else:
        status = ComparisonStatus.FAILED

    blockers: list[str] = []
    for comparison in comparisons:
        blockers.extend(comparison.blockers)
    if not observations:
        blockers.append("没有任何运行记录")

    return ComparisonOutcome(
        status=status,
        target_metric=plan.question.target_metric,
        seeds=comparisons,
        mean_a=mean_a,
        mean_b=mean_b,
        mean_delta=mean_delta,
        mean_delta_percentage_points=mean_delta * 100 if mean_delta is not None else None,
        direction=direction,
        completed_pairs=len(verified),
        requested_pairs=max(requested_pairs, len(seeds)),
        blockers=list(dict.fromkeys(blockers)),
        verifications=[c.verification for c in comparisons if c.verification is not None],
    )
