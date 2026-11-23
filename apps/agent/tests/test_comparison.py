"""Deterministic comparison logic: candidates, planning, verification, report."""

from uuid import uuid4

import pytest
from reproagent_agent.comparison import (
    UnsupportedComparisonError,
    build_comparison_plan,
    build_comparison_report,
    find_candidates,
    verify_pairs,
)
from reproagent_schemas import (
    CandidateRequest,
    CheckStatus,
    ComparisonGroupSpec,
    ComparisonPlanRequest,
    ComparisonReferences,
    ComparisonReportRequest,
    ComparisonStatus,
    EvidenceField,
    EvidenceStatus,
    GroupKey,
    PairItem,
    PaperReference,
    ParamSource,
    ParsedDocument,
    ReportedResult,
    ReproducibilityFingerprint,
    ReproductionCard,
    RunObservation,
    RunStatus,
    TableData,
    TextBlock,
)


def explicit(value, page=8):
    return EvidenceField(
        value=value,
        status=EvidenceStatus.PAPER_EXPLICIT,
        evidence_quote=f"...{value}...",
        source_page=page,
    )


def card(optimizer="SGD", learning_rate="0.01") -> ReproductionCard:
    return ReproductionCard(
        method=explicit("LeNet-5 gradient-based learning"),
        model_architecture=explicit("LeNet-5 convolutional network"),
        dataset=explicit("MNIST handwritten digits"),
        optimizer=explicit(optimizer),
        loss_function=explicit("cross entropy"),
        hyperparameters={"learning_rate": explicit(learning_rate)},
        evaluation_metrics=[explicit("accuracy")],
        reported_results=[
            ReportedResult(
                metric="accuracy",
                value=0.99,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...test accuracy 99%...",
                source_page=10,
            )
        ],
    )


def table_doc() -> ParsedDocument:
    return ParsedDocument(
        num_pages=10,
        blocks=[
            TextBlock(
                id="p8-b1",
                text="We compare SGD and Adam on MNIST. Adam reaches 99% accuracy.",
                page=8,
            )
        ],
        tables=[
            TableData(
                id="t3",
                page=7,
                caption="Table 3: optimizer comparison, test accuracy",
                rows=[
                    ["Optimizer", "Learning rate", "Accuracy"],
                    ["SGD", "0.01", "90.5%"],
                    ["Adam", "0.001", "93.2%"],
                ],
            ),
            TableData(
                id="t4",
                page=9,
                caption="Table 4: ablation under a different training budget, test accuracy",
                rows=[
                    ["Optimizer", "Learning rate", "Accuracy"],
                    ["SGD", "0.05", "88.0%"],
                    ["Adam", "0.0005", "91.0%"],
                ],
            ),
        ],
    )


def references() -> list[PaperReference]:
    return [
        PaperReference(
            label="表3 · SGD", quote="SGD 0.01 90.5%", source_page=7, table_id="t3",
            metric="accuracy", value=0.905,
        ),
        PaperReference(
            label="表3 · Adam", quote="Adam 0.001 93.2%", source_page=7, table_id="t3",
            metric="accuracy", value=0.932,
        ),
    ]


def plan_request(**overrides) -> ComparisonPlanRequest:
    refs = references()
    payload = {
        "card": card(),
        "question": "在相同条件下，Adam 是否比 SGD 准确率更高？",
        "group_specs": [
            ComparisonGroupSpec(
                key=GroupKey.A, label="SGD 组", optimizer="sgd", learning_rate=0.01,
                references=[refs[0]],
            ),
            ComparisonGroupSpec(
                key=GroupKey.B, label="Adam 组", optimizer="adam", learning_rate=0.001,
                references=[refs[1]],
            ),
        ],
    }
    payload.update(overrides)
    return ComparisonPlanRequest(**payload)


def observation(
    key: GroupKey, correct: int, total: int, *, seed=42, split="split"
) -> RunObservation:
    fingerprint = ReproducibilityFingerprint(
        dataset_split_sha256=split,
        init_weights_sha256="init",
        sample_order_sha256="order",
        template_sha256="template",
        seed=seed,
        batch_size=64,
        epochs=2,
        train_subset_size=4096,
        test_subset_size=1024,
        loss_function="cross_entropy",
        optimizer="sgd" if key is GroupKey.A else "adam",
        learning_rate=0.01 if key is GroupKey.A else 0.001,
        code_hash="code-v1",
        image_digest="img-v1",
    )
    return RunObservation(
        run_id=uuid4(),
        group_key=key,
        seed=seed,
        status=RunStatus.SUCCEEDED,
        correct_count=correct,
        evaluated_count=total,
        metric_value=0.5,  # deliberately wrong: the verifier must not trust it
        fingerprint=fingerprint,
    )


# --- candidates -----------------------------------------------------------


def test_candidates_bind_each_result_to_its_own_table_row():
    response = find_candidates(CandidateRequest(card=card(), document=table_doc()))
    assert response.support.ok
    assert [candidate.label for candidate in response.candidates] == ["ADAM vs SGD"]
    candidate = response.candidates[0]
    assert candidate.comparable
    # Both references come from table 3 (the first table with a value); the
    # same-named accuracy row in table 4 must not leak into either group.
    assert {ref.table_id for ref in candidate.references} == {"t3"}
    sgd = next(ref for ref in candidate.references if "SGD" in ref.label.upper())
    adam = next(ref for ref in candidate.references if "ADAM" in ref.label.upper())
    assert sgd.value == pytest.approx(0.905)
    assert adam.value == pytest.approx(0.932)
    assert candidate.note and "同名指标" in candidate.note


def test_candidates_flag_single_optimizer_as_not_comparable():
    only_sgd = ParsedDocument(
        num_pages=3,
        blocks=[TextBlock(id="p2-b1", text="We train with SGD and reach 98% accuracy.", page=2)],
        tables=[],
    )
    plain = card()
    plain.reported_results = []
    plain.hyperparameters = {}
    response = find_candidates(CandidateRequest(card=plain, document=only_sgd))
    assert response.candidates[0].comparable is False
    assert response.candidates[0].blocking


# --- planning -------------------------------------------------------------


def test_missing_learning_rate_becomes_pending_with_suggestion():
    request = plan_request()
    request.group_specs[1].learning_rate = None
    request.card.hyperparameters = {}
    response = build_comparison_plan(request)
    pending = {item.id: item for item in response.plan.pending}
    assert "groups.b.learning_rate.value" in pending
    item = pending["groups.b.learning_rate.value"]
    assert item.suggested_value == 0.001
    assert "学习率" in item.impact
    assert not response.plan.approval_ready()


def test_plan_rejects_unsupported_dataset_with_concrete_gap():
    broken = card()
    broken.dataset = explicit("CIFAR-10 images")
    with pytest.raises(UnsupportedComparisonError) as exc:
        build_comparison_plan(plan_request(card=broken))
    assert any("CIFAR-10" in gap for gap in exc.value.gaps)


def test_plan_requires_a_reference_for_each_group():
    request = plan_request()
    request.group_specs[1].references = []
    response = build_comparison_plan(request)
    assert "groups.b.references" in {item.id for item in response.plan.pending}


def test_pending_state_survives_serialization():
    request = plan_request()
    request.group_specs[1].learning_rate = None
    response = build_comparison_plan(request)
    restored = type(response.plan).model_validate_json(response.plan.model_dump_json())
    assert [item.id for item in restored.open_pending] == [
        item.id for item in response.plan.open_pending
    ]


# --- verification ---------------------------------------------------------


def test_accuracy_is_recomputed_not_trusted():
    plan = build_comparison_plan(plan_request()).plan
    outcome = verify_pairs(
        plan, [observation(GroupKey.A, 880, 1024), observation(GroupKey.B, 921, 1024)], 1
    )
    assert outcome.status is ComparisonStatus.COMPLETED
    assert outcome.seeds[0].recomputed_accuracy_a == pytest.approx(880 / 1024)
    assert outcome.seeds[0].recomputed_accuracy_b == pytest.approx(921 / 1024)
    assert outcome.mean_delta == pytest.approx((880 - 921) / 1024)
    assert outcome.mean_delta_percentage_points == pytest.approx((880 - 921) / 1024 * 100)
    assert outcome.direction == "b_higher"


def test_broken_common_condition_blocks_the_difference():
    plan = build_comparison_plan(plan_request()).plan
    outcome = verify_pairs(
        plan,
        [
            observation(GroupKey.A, 880, 1024, split="split-a"),
            observation(GroupKey.B, 921, 1024, split="split-b"),
        ],
        1,
    )
    assert outcome.status is ComparisonStatus.INCOMPLETE
    assert outcome.mean_delta is None
    split_check = next(
        check for check in outcome.seeds[0].verification.checks
        if check.item is PairItem.DATASET_SPLIT
    )
    assert split_check.status is CheckStatus.DEVIATED
    assert any("数据划分" in blocker for blocker in outcome.blockers)


def test_missing_fingerprint_is_unknown_not_matched():
    plan = build_comparison_plan(plan_request()).plan
    left = observation(GroupKey.A, 880, 1024)
    right = observation(GroupKey.B, 921, 1024)
    right.fingerprint.init_weights_sha256 = None
    outcome = verify_pairs(plan, [left, right], 1)
    assert outcome.status is ComparisonStatus.INCOMPLETE
    weights = next(
        check for check in outcome.seeds[0].verification.checks
        if check.item is PairItem.INITIAL_WEIGHTS
    )
    assert weights.status is CheckStatus.UNKNOWN


def test_one_failed_group_leaves_the_comparison_unfinished():
    plan = build_comparison_plan(plan_request()).plan
    failed = observation(GroupKey.B, 0, 1024)
    failed.status = RunStatus.FAILED
    failed.error = "CUDA out of memory"
    failed.correct_count = None
    outcome = verify_pairs(plan, [observation(GroupKey.A, 880, 1024), failed], 1)
    assert outcome.status is ComparisonStatus.INCOMPLETE
    assert outcome.mean_delta is None
    assert any("B 组运行未成功" in blocker for blocker in outcome.blockers)


def test_no_runs_is_failed_not_completed():
    plan = build_comparison_plan(plan_request()).plan
    outcome = verify_pairs(plan, [], 1)
    assert outcome.status is ComparisonStatus.FAILED


def test_mean_over_seeds_requires_all_pairs():
    request = plan_request(question="SGD 与 Adam 在多次运行中的平均准确率是否有差异？")
    request.seeds = [42, 43]
    plan = build_comparison_plan(request).plan
    # A pinned seed list cannot make a "mean" question answerable by one pair.
    assert plan.seeds == [42, 43, 44]
    assert plan.question.requires_mean
    assert "3 个相同种子" in (plan.question.mean_basis or "")
    assert any("补齐为 3 个" in item for item in plan.assumptions)
    observations = [
        observation(GroupKey.A, 880, 1024, seed=42),
        observation(GroupKey.B, 921, 1024, seed=42),
        observation(GroupKey.A, 900, 1024, seed=43),
        observation(GroupKey.B, 910, 1024, seed=43),
        observation(GroupKey.A, 890, 1024, seed=44),
        observation(GroupKey.B, 905, 1024, seed=44),
    ]
    outcome = verify_pairs(plan, observations, 3)
    assert outcome.status is ComparisonStatus.COMPLETED
    assert outcome.completed_pairs == 3
    expected = ((880 + 900 + 890) / 3 - (921 + 910 + 905) / 3) / 1024
    assert outcome.mean_delta == pytest.approx(expected)


# --- report ---------------------------------------------------------------


def _references(plan) -> ComparisonReferences:
    return ComparisonReferences(
        comparison_id=uuid4(),
        plan_id=uuid4(),
        plan_version_id=uuid4(),
        plan_version=1,
        paper_id=uuid4(),
        spec_version_id=uuid4(),
        spec_version=1,
        run_ids=[uuid4(), uuid4()],
        code_hashes=["code-v1"],
        image_digest="img-v1",
    )


def test_report_states_the_difference_and_its_limits():
    plan = build_comparison_plan(plan_request()).plan
    outcome = verify_pairs(
        plan, [observation(GroupKey.A, 880, 1024), observation(GroupKey.B, 921, 1024)], 1
    )
    response = build_comparison_report(
        ComparisonReportRequest(
            references=_references(plan), plan=plan, outcome=outcome, paper_title="LeNet-5"
        )
    )
    markdown = response.markdown
    assert "对照完成" in markdown
    assert "85.94%" in markdown and "89.94%" in markdown
    assert "4.00 个百分点" in markdown
    assert "不构成论文的完整复现" in markdown or "不构成论文完整复现" in markdown
    assert "## 8. 配对核验" in markdown
    assert "重算准确率" in markdown


def test_incomplete_report_shows_blockers_and_no_delta():
    plan = build_comparison_plan(plan_request()).plan
    failed = observation(GroupKey.B, 0, 1024)
    failed.status = RunStatus.TIMEOUT
    failed.correct_count = None
    outcome = verify_pairs(plan, [observation(GroupKey.A, 880, 1024), failed], 1)
    response = build_comparison_report(
        ComparisonReportRequest(
            references=_references(plan), plan=plan, outcome=outcome, paper_title="LeNet-5"
        )
    )
    assert "对照未完成" in response.markdown
    assert "不给出对照结论" in response.report.summary
    assert outcome.mean_delta is None


def test_duplicate_pinned_seeds_are_deduplicated():
    request = plan_request()
    request.seeds = [42, 42, 43]
    plan = build_comparison_plan(request).plan
    assert plan.seeds == [42, 43]
    assert plan.question.requires_mean
    assert not any("补齐" in item for item in plan.assumptions)


def test_user_supplied_reference_counts_as_evidence():
    request = plan_request()
    request.group_specs[1].references = [
        PaperReference(
            label="附录 B 表 5",
            quote=None,
            source_page=None,
            source_section="用户补充依据",
            metric="accuracy",
        )
    ]
    response = build_comparison_plan(request)
    pending_ids = {item.id for item in response.plan.pending}
    assert "groups.b.optimizer.source" not in pending_ids
    group_b = response.plan.group(GroupKey.B)
    assert group_b.optimizer.source is ParamSource.HUMAN_CONFIRMED
    assert group_b.optimizer.human_confirmed


def test_report_marks_a_withdrawn_comparison_as_unfinished():
    plan = build_comparison_plan(plan_request()).plan
    outcome = verify_pairs(
        plan, [observation(GroupKey.A, 880, 1024), observation(GroupKey.B, 921, 1024)], 1
    )
    response = build_comparison_report(
        ComparisonReportRequest(
            references=_references(plan),
            plan=plan,
            outcome=outcome,
            paper_title="LeNet-5",
            review_notes=["问题已变更：原问题保留为未完成"],
        )
    )
    assert response.report.final_status is ComparisonStatus.INCOMPLETE
    assert "对照未完成" in response.markdown
    assert "问题已变更" in response.markdown
    assert "作废" in response.report.summary
    assert "高出" not in response.report.summary
