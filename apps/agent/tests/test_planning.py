from uuid import uuid4

import pytest
from reproagent_agent.graph.build import build_planning_graph
from reproagent_agent.planning import ScalingConfig, UnsupportedPaperError, build_plan
from reproagent_agent.planning.assumptions import (
    as_float,
    as_int,
    normalize_loss,
    normalize_optimizer,
)
from reproagent_schemas import (
    AssumptionKind,
    ConfirmationSource,
    EvidenceField,
    EvidenceStatus,
    ParamSource,
    ReproductionCard,
    ReviewMode,
    ReviewRequest,
    RiskTier,
    utcnow,
)


def explicit(value, page=8):
    return EvidenceField(
        value=value,
        status=EvidenceStatus.PAPER_EXPLICIT,
        evidence_quote=f"...{value}...",
        source_page=page,
    )


def make_card(**overrides) -> ReproductionCard:
    base = dict(
        method=explicit("LeNet-5 gradient-based learning"),
        model_architecture=explicit("LeNet-5 convolutional network"),
        dataset=explicit("MNIST handwritten digits"),
        optimizer=explicit("SGD"),
        loss_function=explicit("cross-entropy"),
        hyperparameters={
            "learning_rate": explicit("0.001"),
            "batch_size": explicit(128, page=9),
            "epochs": explicit(20, page=9),
        },
        evaluation_metrics=[explicit("accuracy")],
    )
    base.update(overrides)
    return ReproductionCard(**base)


class TestHelpers:
    def test_as_float_extracts_number(self):
        assert as_float("lr = 0.001") == 0.001
        assert as_float(3) == 3.0
        assert as_float(True) is None
        assert as_float("none") is None

    def test_as_int(self):
        assert as_int("batch 128") == 128
        assert as_int(None) is None

    def test_normalize_optimizer(self):
        assert normalize_optimizer("Stochastic Gradient Descent") == "sgd"
        assert normalize_optimizer("Adam") == "adam"
        assert normalize_optimizer("diagonal Levenberg-Marquardt") is None
        assert normalize_optimizer(None) is None

    def test_normalize_loss(self):
        assert normalize_loss("cross-entropy") == "cross_entropy"
        assert normalize_loss("Softmax Loss") == "cross_entropy"
        assert normalize_loss("discriminative forward loss function") is None
        assert normalize_loss(None) is None


class TestGuards:
    def test_rejects_non_mnist(self):
        with pytest.raises(UnsupportedPaperError, match="MNIST"):
            build_plan(make_card(dataset=explicit("CIFAR-10 images")))

    def test_rejects_missing_dataset(self):
        with pytest.raises(UnsupportedPaperError):
            build_plan(make_card(dataset=EvidenceField(status=EvidenceStatus.MISSING)))

    def test_rejects_non_lenet(self):
        with pytest.raises(UnsupportedPaperError, match="LeNet"):
            build_plan(
                make_card(
                    model_architecture=explicit("ResNet-50"), method=explicit("residual learning")
                )
            )


class TestProvenance:
    def test_paper_explicit_lr_preserved(self):
        plan = build_plan(make_card())
        lr = plan.training_config.learning_rate
        assert lr.value == 0.001
        assert lr.source is ParamSource.PAPER_EXPLICIT
        assert lr.source_page == 8
        assert lr.evidence_quote

    def test_missing_loss_is_flagged_proposal(self):
        plan = build_plan(make_card(loss_function=EvidenceField(status=EvidenceStatus.MISSING)))
        loss = plan.training_config.loss_function
        assert loss.value == "cross_entropy"
        assert loss.source is ParamSource.INFERRED
        assert loss.requires_confirmation is True
        kinds = {a.field_path: a.kind for a in plan.assumptions}
        assert kinds["training_config.loss_function"] is AssumptionKind.MISSING_PROPOSAL
        assert any(
            a.field_path == "training_config.loss_function" for a in plan.unconfirmed_required()
        )

    def test_nonstandard_optimizer_proposes_sgd(self):
        plan = build_plan(make_card(optimizer=explicit("stochastic diagonal Levenberg-Marquardt")))
        opt = plan.training_config.optimizer
        assert opt.value == "sgd"
        assert opt.source is ParamSource.INFERRED
        assert opt.paper_value == "stochastic diagonal Levenberg-Marquardt"
        assert opt.requires_confirmation is True
        assert any(a.kind is AssumptionKind.NONSTANDARD_CHOICE for a in plan.assumptions)

    def test_inferred_nonstandard_optimizer_proposes_sgd(self):
        inferred = EvidenceField(
            value="stochastic diagonal Levenberg-Marquardt procedure",
            status=EvidenceStatus.INFERRED,
            confidence=0.6,
            evidence_quote="...diagonal Levenberg-Marquardt...",
            source_page=8,
        )
        plan = build_plan(make_card(optimizer=inferred))
        opt = plan.training_config.optimizer
        assert opt.value == "sgd"
        assert opt.requires_confirmation is True
        assert opt.paper_value == "stochastic diagonal Levenberg-Marquardt procedure"
        assumption = next(
            a for a in plan.assumptions if a.field_path == "training_config.optimizer"
        )
        assert assumption.kind is AssumptionKind.NONSTANDARD_CHOICE
        assert assumption.value == "sgd"

    def test_standard_optimizer_normalized(self):
        plan = build_plan(make_card(optimizer=explicit("Adam")))
        assert plan.training_config.optimizer.value == "adam"
        assert plan.training_config.optimizer.source is ParamSource.PAPER_EXPLICIT

    def test_standard_loss_normalized(self):
        plan = build_plan(make_card(loss_function=explicit("cross-entropy")))
        loss = plan.training_config.loss_function
        assert loss.value == "cross_entropy"
        assert loss.source is ParamSource.PAPER_EXPLICIT
        assert loss.paper_value == "cross-entropy"
        assert plan.unconfirmed_required() == []

    def test_nonstandard_loss_proposes_cross_entropy(self):
        plan = build_plan(make_card(loss_function=explicit("discriminative forward loss function")))
        loss = plan.training_config.loss_function
        assert loss.value == "cross_entropy"
        assert loss.source is ParamSource.INFERRED
        assert loss.paper_value == "discriminative forward loss function"
        assert loss.requires_confirmation is True
        assumption = next(
            a for a in plan.assumptions if a.field_path == "training_config.loss_function"
        )
        assert assumption.kind is AssumptionKind.NONSTANDARD_CHOICE
        assert assumption.value == "cross_entropy"
        assert assumption.risk_tier is RiskTier.CRITICAL
        assert assumption in plan.unconfirmed_critical()

    def test_inferred_nonstandard_loss_proposes_cross_entropy(self):
        inferred = EvidenceField(
            value="discriminative forward loss",
            status=EvidenceStatus.INFERRED,
            confidence=0.6,
            evidence_quote="...forward loss...",
            source_page=8,
        )
        plan = build_plan(make_card(loss_function=inferred))
        loss = plan.training_config.loss_function
        assert loss.value == "cross_entropy"
        assert loss.requires_confirmation is True
        assert loss.paper_value == "discriminative forward loss"
        assumption = next(
            a for a in plan.assumptions if a.field_path == "training_config.loss_function"
        )
        assert assumption.kind is AssumptionKind.NONSTANDARD_CHOICE
        assert assumption.risk_tier is RiskTier.CRITICAL


class TestAssistedReviewPhase1:
    def test_prose_and_quoted_inferences_keep_distinct_provenance(self):
        card = make_card(
            data_preprocessing={
                "centering": EvidenceField(
                    value="centered by bounding box",
                    status=EvidenceStatus.PAPER_DESCRIBED,
                    evidence_quote="images are centered",
                    source_page=4,
                ),
                "size_normalization": EvidenceField(
                    value="size normalized to 20x20",
                    status=EvidenceStatus.INFERRED,
                    confidence=0.7,
                    evidence_quote="size normalization",
                    source_page=4,
                ),
            }
        )
        plan = build_plan(card)
        assert plan.preprocessing_config.steps[0].source is ParamSource.PAPER_EXPLICIT
        assert plan.preprocessing_config.steps[1].source is ParamSource.INFERRED
        inferred = [a for a in plan.assumptions if a.field_path.startswith("preprocessing_config.")]
        assert len(inferred) == 1
        assert inferred[0].field_path == "preprocessing_config.size_normalization"
        assert inferred[0].requires_confirmation

    def test_true_inferred_preprocessing_is_routine(self):
        card = make_card(
            data_preprocessing={
                "background_extension": EvidenceField(
                    value="background extended",
                    status=EvidenceStatus.INFERRED,
                    confidence=0.5,
                ),
            }
        )
        plan = build_plan(card)
        a = next(
            a
            for a in plan.assumptions
            if a.field_path == "preprocessing_config.background_extension"
        )
        assert a.risk_tier is RiskTier.ROUTINE
        assert a.auto_acceptable is True
        assert a.requires_confirmation is True
        assert a not in plan.unconfirmed_critical()
        assert a in plan.unconfirmed_required()

    def test_weight_decay_missing_is_routine(self):
        card = make_card(
            hyperparameters={
                "learning_rate": explicit("0.001"),
                "weight_decay": EvidenceField(status=EvidenceStatus.MISSING),
            }
        )
        plan = build_plan(card)
        wd = next(a for a in plan.assumptions if a.field_path == "training_config.weight_decay")
        assert wd.risk_tier is RiskTier.ROUTINE
        assert wd.auto_acceptable is True
        assert wd not in plan.unconfirmed_critical()

    def test_missing_loss_is_critical(self):
        plan = build_plan(make_card(loss_function=EvidenceField(status=EvidenceStatus.MISSING)))
        loss = next(a for a in plan.assumptions if a.field_path == "training_config.loss_function")
        assert loss.risk_tier is RiskTier.CRITICAL
        assert loss in plan.unconfirmed_critical()


class TestAssistedReviewPhase2:
    @pytest.mark.parametrize(
        "confirmed,confirmed_by,source",
        [
            (False, None, ParamSource.INFERRED),
            (True, None, ParamSource.HUMAN_CONFIRMED),  # legacy expert confirmation
            (True, ConfirmationSource.HUMAN, ParamSource.HUMAN_CONFIRMED),
            (True, ConfirmationSource.AI_ASSISTED, ParamSource.INFERRED),
        ],
    )
    def test_inferred_training_provenance(self, confirmed, confirmed_by, source):
        def inferred(value):
            return EvidenceField(
                value=value,
                status=EvidenceStatus.INFERRED,
                confidence=0.96,
                human_confirmed=confirmed,
                confirmed_by=confirmed_by,
                evidence_quote="context only, not a stated parameter",
                source_page=4,
            )

        card = make_card(
            optimizer=inferred("sgd"),
            loss_function=inferred("cross_entropy"),
            hyperparameters={"learning_rate": inferred(0.01), "weight_decay": inferred(0.0)},
        )
        original = card.model_dump()
        plan = build_plan(card)
        expected_by = confirmed_by or (ConfirmationSource.HUMAN if confirmed else None)
        for name in ("optimizer", "loss_function", "learning_rate", "weight_decay"):
            param = getattr(plan.training_config, name)
            assumption = next(
                a for a in plan.assumptions if a.field_path == f"training_config.{name}"
            )
            assert param.source is source
            assert assumption.kind is AssumptionKind.INFERRED
            for item in (param, assumption):
                assert item.confidence == 0.96
                assert item.confirmed_by is expected_by
                assert item.human_confirmed is confirmed
                assert item.requires_confirmation is not confirmed
                assert item.source_page == 4
                assert item.evidence_quote == "context only, not a stated parameter"
        assert card.model_dump() == original

    @pytest.mark.parametrize("has_quote", [False, True])
    def test_assisted_preprocessing_never_promoted_by_prose_grounding(self, has_quote):
        field = EvidenceField(
            value="center images",
            status=EvidenceStatus.INFERRED,
            confidence=0.97,
            human_confirmed=True,
            confirmed_by=ConfirmationSource.AI_ASSISTED,
            evidence_quote="images were centered" if has_quote else None,
            source_page=4 if has_quote else None,
        )
        card = make_card(data_preprocessing={"centering": field})
        original = card.model_dump()
        plan = build_plan(card)
        assert plan.preprocessing_config.steps[0].source is ParamSource.INFERRED
        assumption = next(
            a for a in plan.assumptions if a.field_path == "preprocessing_config.centering"
        )
        assert assumption.kind is AssumptionKind.INFERRED
        assert assumption.card_status is EvidenceStatus.INFERRED
        assert assumption.confirmed_by is ConfirmationSource.AI_ASSISTED
        assert assumption.confidence == 0.97
        assert assumption.human_confirmed
        assert not assumption.requires_confirmation
        assert assumption.evidence_quote == field.evidence_quote
        assert assumption.source_page == field.source_page
        assert assumption not in plan.unconfirmed_required()
        assert card.model_dump() == original

    @pytest.mark.parametrize("confirmed", [False, True])
    def test_pending_and_manual_preprocessing_confidence(self, confirmed):
        card = make_card(
            data_preprocessing={
                "normalize": EvidenceField(
                    value="normalize inputs",
                    status=EvidenceStatus.INFERRED,
                    confidence=0.93,
                    human_confirmed=confirmed,
                )
            }
        )
        plan = build_plan(card)
        expected_source = ParamSource.HUMAN_CONFIRMED if confirmed else ParamSource.INFERRED
        assert plan.preprocessing_config.steps[0].source is expected_source
        assumption = next(
            a for a in plan.assumptions if a.field_path == "preprocessing_config.normalize"
        )
        assert assumption.confidence == 0.93
        assert assumption.human_confirmed is confirmed
        assert assumption.requires_confirmation is not confirmed
        expected_by = ConfirmationSource.HUMAN if confirmed else None
        assert assumption.confirmed_by is expected_by

    @pytest.mark.parametrize("mode", [ReviewMode.EXPERT, ReviewMode.ASSISTED])
    def test_review_mode_propagates(self, mode):
        assert build_plan(make_card(review_mode=mode)).review_mode is mode

    def test_replacement_proposals_do_not_inherit_confirmation_or_confidence(self):
        def inferred(value):
            return EvidenceField(
                value=value,
                status=EvidenceStatus.INFERRED,
                confidence=0.99,
                human_confirmed=True,
                confirmed_by=ConfirmationSource.AI_ASSISTED,
            )

        plan = build_plan(
            make_card(
                optimizer=inferred("unsupported optimizer"),
                hyperparameters={"learning_rate": inferred("unknown")},
            )
        )
        for name in ("optimizer", "learning_rate"):
            param = getattr(plan.training_config, name)
            assumption = next(
                a for a in plan.assumptions if a.field_path == f"training_config.{name}"
            )
            for item in (param, assumption):
                assert item.confidence == 0
                assert item.confirmed_by is None
                assert not item.human_confirmed
                assert item.requires_confirmation


class TestDelegatedReview:
    def test_card_authority_carries_over_but_auto_requires_fresh_plan_recommendations(self):
        def auto(value):
            return EvidenceField(
                value=value, status="inferred", confirmed_by="auto", confidence=0.98,
                evidence_quote="context only", source_page=4,
            )

        actor, at = uuid4(), utcnow()
        card = make_card(
            review_mode="delegated", delegated_by=actor, delegated_at=at,
            optimizer=auto("sgd"), loss_function=auto("cross_entropy"),
            hyperparameters={"learning_rate": auto(0.01), "weight_decay": auto(0.0)},
            data_preprocessing={"normalize": auto("normalize inputs")},
        )
        before = card.model_dump(mode="json")
        plan = build_plan(card)
        assert plan.review_mode is ReviewMode.DELEGATED
        assert plan.delegated_at == at and plan.delegated_by == actor
        assert not plan.approval_ready(actor)
        assert not plan.approval_ready(uuid4())
        expected = {"training_config.optimizer", "training_config.loss_function",
                    "training_config.learning_rate", "training_config.weight_decay",
                    "preprocessing_config.normalize"}
        pending = ReviewRequest(card=card, plan=plan, scope="actor").review_fields()
        assert {f.field_path for f in pending} == expected
        for assumption in plan.unconfirmed_required():
            assert assumption.confirmed_by is None
            assert not assumption.human_confirmed
            assert assumption.evidence_quote == "context only"
        assert plan.preprocessing_config.steps[0].source is ParamSource.INFERRED
        for name in ("optimizer", "loss_function", "learning_rate", "weight_decay"):
            param = getattr(plan.training_config, name)
            assert param.confirmed_by is None and not param.human_confirmed
            assert param.requires_confirmation and param.source is ParamSource.INFERRED
        assert card.model_dump(mode="json") == before

    def test_auto_missing_still_generates_an_unaccepted_proposal(self):
        actor = uuid4()
        card = make_card(
            review_mode="delegated", delegated_by=actor, delegated_at=utcnow(),
            loss_function=EvidenceField(status="missing", confirmed_by="auto"),
        )
        plan = build_plan(card)
        loss = plan.training_config.loss_function
        assert loss.value == "cross_entropy"
        assert loss.confirmed_by is None and not loss.human_confirmed
        assert loss.requires_confirmation
        assert loss.evidence_quote is None and loss.paper_value is None
        assert not plan.approval_ready(actor)


class TestScaling:
    def test_mvp_defaults_tagged_and_deviations_recorded(self):
        plan = build_plan(make_card())
        assert plan.training_config.batch_size.value == 64
        assert plan.training_config.batch_size.source is ParamSource.MVP_SCALING
        assert plan.training_config.epochs.value == 3
        paths = {d.field_path for d in plan.deviations_from_paper}
        assert "training_config.batch_size" in paths
        assert "training_config.epochs" in paths
        assert "dataset_config.train_subset_size" in paths

    def test_mvp_scaling_does_not_block_approval(self):
        plan = build_plan(make_card())
        # all paper-explicit card -> no required confirmations from scaling
        assert plan.unconfirmed_required() == []

    def test_overrides_change_profile(self):
        scaling = ScalingConfig(train_subset_size=5000, epochs=5, batch_size=32, max_seconds=300)
        plan = build_plan(make_card(), scaling)
        assert plan.dataset_config.train_subset_size == 5000
        assert plan.training_config.epochs.value == 5
        assert plan.training_config.batch_size.value == 32
        assert plan.runtime_budget.max_seconds == 300
        assert plan.expected_runtime.estimated_seconds <= 300

    def test_preprocessing_fallback_when_absent(self):
        plan = build_plan(make_card())
        names = {s.name for s in plan.preprocessing_config.steps}
        assert {"to_tensor", "normalize"} <= names
        assert all(s.source is ParamSource.MVP_SCALING for s in plan.preprocessing_config.steps)


class TestDeterminism:
    def test_same_card_same_plan(self):
        card = make_card()
        assert build_plan(card).model_dump() == build_plan(card).model_dump()

    def test_runtime_within_budget(self):
        plan = build_plan(make_card())
        assert plan.expected_runtime.estimated_seconds <= plan.runtime_budget.max_seconds
        assert plan.runtime_budget.device == "cpu"
        assert plan.training_config.seed == 42


async def test_planning_graph_produces_plan():
    graph = build_planning_graph()
    result = await graph.ainvoke({"card": make_card(), "scaling": ScalingConfig()})
    assert result["plan"].model_cfg.architecture == "lenet-5"


async def test_planning_graph_requires_card():
    graph = build_planning_graph()
    with pytest.raises(ValueError, match="ReproductionCard"):
        await graph.ainvoke({})
