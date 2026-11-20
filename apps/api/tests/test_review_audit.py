"""Public audit/report service tests: immutable ancestry, stable IDs and redaction."""

from copy import deepcopy
from datetime import datetime
from uuid import UUID, uuid4

from reproagent_api.services.reports import build_evaluate_request
from reproagent_api.services.review_audit import collect_review_decisions, decision_traces
from reproagent_schemas.common import ReviewDecision
from test_runs import (
    EXPERIMENT_ID,
    PAPER_ID,
    PLAN_ID,
    SPEC_ID,
    FakeAdmin,
    seed_tables,
)

STAMP = "2026-01-01T12:00:00+00:00"


def event(field="optimizer", **kwargs):
    return {
        "id": str(uuid4()), "field_path": field, "before": "sgd", "after": "adam",
        "confirmed_by": "ai_assisted", "review_mode": "assisted",
        "actor": str(uuid4()), "timestamp": STAMP, **kwargs,
    }


def audit_tree():
    tables = seed_tables()
    card = tables["reproduction_specs"][0]
    plan = tables["experiment_plans"][0]
    card["spec_json"]["review_decisions"] = [event("optimizer")]
    plan["changes_json"] = [event("training_config.optimizer")]
    # Siblings must not enter either pinned report. Latest mode has its own ancestry.
    for table, root, key in (
        ("reproduction_specs", card, "spec_json"),
        ("experiment_plans", plan, "config_json"),
    ):
        child = deepcopy(root)
        child.update(id=str(uuid4()), version=2, parent_version_id=root["id"])
        later = deepcopy(root)
        later.update(id=str(uuid4()), version=3, parent_version_id=root["id"])
        if key == "spec_json":
            child[key]["review_decisions"] = [event("card.child")]
            later[key]["review_decisions"] = [event("card.sibling")]
        else:
            child["changes_json"] = [event("plan.child")]
            later["changes_json"] = [event("plan.sibling")]
        tables[table].extend([child, later])
    return FakeAdmin(tables, {})


def fields(decisions):
    return {d.field_path for d in decisions}


def test_selected_ancestry_excludes_sibling_future_and_foreign_versions():
    admin = audit_tree()
    cards = admin.tables["reproduction_specs"]
    plans = admin.tables["experiment_plans"]
    result = collect_review_decisions(
        admin, paper_id=str(PAPER_ID), experiment_id=str(EXPERIMENT_ID),
        spec_id=cards[1]["id"], plan_id=plans[1]["id"],
    )
    assert fields(result) == {"optimizer", "training_config.optimizer", "card.child", "plan.child"}
    latest = collect_review_decisions(
        admin, paper_id=str(PAPER_ID), experiment_id=str(EXPERIMENT_ID),
    )
    assert fields(latest) == {
        "optimizer", "training_config.optimizer", "card.sibling", "plan.sibling",
    }
    # Corrupt ancestry crossing ownership must stop, not leak the foreign parent.
    cards[0]["paper_id"] = str(uuid4())
    plans[0]["experiment_id"] = str(uuid4())
    result = collect_review_decisions(
        admin, paper_id=str(PAPER_ID), experiment_id=str(EXPERIMENT_ID),
        spec_id=cards[1]["id"], plan_id=plans[1]["id"],
    )
    assert fields(result) == {"card.child", "plan.child"}


def test_cross_paper_experiment_is_rejected_even_with_explicit_plan():
    admin = audit_tree()
    assert collect_review_decisions(
        admin, paper_id=str(uuid4()), experiment_id=str(EXPERIMENT_ID),
    ) == []
    assert collect_review_decisions(
        admin, paper_id=str(uuid4()), plan_id=str(PLAN_ID),
    ) == []
    assert fields(collect_review_decisions(
        admin, paper_id=str(PAPER_ID), spec_id=str(SPEC_ID), plan_id=str(PLAN_ID),
    )) == {"optimizer", "training_config.optimizer"}


def test_missing_pins_do_not_fall_back_to_latest_or_invent_events():
    admin = audit_tree()
    assert collect_review_decisions(
        admin, paper_id=str(PAPER_ID), experiment_id=str(EXPERIMENT_ID),
        spec_id=str(uuid4()), plan_id=str(uuid4()),
    ) == []
    admin.tables["reproduction_specs"][0]["parent_version_id"] = str(uuid4())
    assert fields(collect_review_decisions(
        admin, paper_id=str(PAPER_ID), spec_id=str(SPEC_ID),
    )) == {"optimizer"}


def test_old_event_ids_are_stable_read_only_and_duplicates_are_removed():
    admin = audit_tree()
    card = admin.tables["reproduction_specs"][0]
    old = event("old")
    del old["id"]
    del old["timestamp"]
    card["created_at"] = STAMP
    card["spec_json"]["review_decisions"] = [old, deepcopy(old)]
    plan = admin.tables["experiment_plans"][0]
    duplicate = event("same")
    plan["changes_json"] = [duplicate, deepcopy(duplicate)]
    before = deepcopy(admin.tables)
    kwargs = dict(paper_id=str(PAPER_ID), experiment_id=str(EXPERIMENT_ID),
                  spec_id=str(SPEC_ID), plan_id=str(PLAN_ID))
    first = collect_review_decisions(admin, **kwargs)
    second = collect_review_decisions(admin, **kwargs)
    assert [d.id for d in first] == [d.id for d in second]
    assert len(first) == 3
    assert len({d.id for d in first}) == 3
    assert all(d.id.version == 5 for d in first if d.field_path == "old")
    assert all(d.timestamp == datetime.fromisoformat(STAMP) for d in first)
    assert admin.tables == before
    assert "agent_traces" not in admin.tables


def test_cycles_terminate_and_missing_history_is_not_fabricated():
    admin = audit_tree()
    cards = admin.tables["reproduction_specs"]
    cards[0]["parent_version_id"] = cards[1]["id"]
    assert fields(collect_review_decisions(
        admin, paper_id=str(PAPER_ID), spec_id=cards[1]["id"],
    )) == {"optimizer", "card.child"}
    cards[0]["spec_json"]["review_decisions"] = []
    cards[0]["parent_version_id"] = None
    assert collect_review_decisions(admin, paper_id=str(PAPER_ID), spec_id=str(SPEC_ID)) == []


def test_projection_redacts_both_values_and_recommendation_and_preserves_identity():
    secret = "sk-" + "x" * 28
    decision = ReviewDecision.model_validate(event(
        before={"api_key": "not-pattern-shaped", "nested": [secret]},
        after={"value": secret, "authorization": "Bearer hidden"},
        recommendation_reasoning=f"use {secret}", recommendation_confidence=0.9,
        recommendation_id="rec-1", recommendation_risk="routine", preselection_threshold=0.8,
    ))
    traces = decision_traces([decision, decision], experiment_id=str(EXPERIMENT_ID))
    assert len(traces) == 1
    trace = traces[0]
    assert trace.id == decision.id
    assert trace.node_name == "review_decision"
    assert trace.workflow_id is None
    assert trace.experiment_id == EXPERIMENT_ID
    assert trace.created_at == decision.timestamp
    assert trace.input_json["before"] == {"nested": ["[REDACTED]"]}
    assert trace.output_json["after"] == {"value": "[REDACTED]"}
    assert trace.output_json["recommendation_reasoning"] == "use [REDACTED]"
    assert trace.output_json["recommendation_confidence"] == 0.9
    assert secret not in trace.model_dump_json()
    assert decision.before["api_key"] == "not-pattern-shaped"  # no mutation


def test_collector_redacts_for_reports_and_scalar_sensitive_fields():
    admin = audit_tree()
    row = admin.tables["reproduction_specs"][0]
    row["spec_json"]["review_decisions"] = [event(
        "provider.api_key", before="old-credential", after="new-credential",
        recommendation_reasoning="adopt new-credential",
    )]
    result = collect_review_decisions(admin, paper_id=str(PAPER_ID), spec_id=str(SPEC_ID))
    assert result[0].before == result[0].after == "[REDACTED]"
    assert result[0].recommendation_reasoning == "[REDACTED]"


def test_evaluate_request_pins_real_versions_and_review_references():
    admin = audit_tree()
    run_id = str(uuid4())
    admin.tables["execution_runs"] = [{
        "id": run_id, "experiment_id": str(EXPERIMENT_ID), "plan_id": str(PLAN_ID),
        "code_version": 1, "status": "succeeded", "metrics_json": {},
    }]
    request = build_evaluate_request(admin, run_id)
    assert fields(request.review_decisions) == {"optimizer", "training_config.optimizer"}
    assert request.references.review_trace_ids == [d.id for d in request.review_decisions]
    assert request.references.spec_version_id == SPEC_ID
    assert request.references.plan_id == PLAN_ID
    assert request.references.run_id == UUID(run_id)
