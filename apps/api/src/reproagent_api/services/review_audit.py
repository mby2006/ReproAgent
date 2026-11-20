"""Project immutable version ancestry into audit traces without duplicate writes."""

import logging
from datetime import UTC
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from reproagent_schemas.common import ReviewDecision
from reproagent_schemas.workflow import NodeTrace

from reproagent_api.workflow.harness import redact

logger = logging.getLogger(__name__)


def _ancestry(admin, table: str, owner_key: str, owner: str, version_id: str | None):
    query = admin.table(table).select("*").eq(owner_key, owner)
    if version_id is not None:
        query = query.eq("id", version_id)
    else:
        query = query.order("version", desc=True)
    rows = query.limit(1).execute().data or []
    seen: set[str] = set()
    chain = []
    while rows:
        row = rows[0]
        key = str(row["id"])
        if key in seen:
            break
        seen.add(key)
        chain.append(row)
        parent = row.get("parent_version_id")
        if not parent:
            break
        rows = (
            admin.table(table).select("*").eq(owner_key, owner)
            .eq("id", str(parent)).limit(1).execute().data or []
        )
    # The parent's events precede the child's, including duplicate legacy payloads.
    return reversed(chain)


def _safe_decision(decision: ReviewDecision) -> ReviewDecision:
    payload = redact(decision.model_dump(mode="json"))
    # Scalar values may reveal sensitive keys only through their field_path.
    path_keys = decision.field_path.replace("[", ".").replace("]", "").split(".")
    if any(not redact({key: True}) for key in path_keys):
        payload["before"] = "[REDACTED]"
        payload["after"] = "[REDACTED]"
        if payload.get("recommendation_reasoning"):
            payload["recommendation_reasoning"] = "[REDACTED]"
    return ReviewDecision.model_validate(payload)


def collect_review_decisions(
    admin,
    *,
    paper_id: str,
    experiment_id: str | None = None,
    spec_id: str | None = None,
    plan_id: str | None = None,
) -> list[ReviewDecision]:
    """Collect redacted ancestor events with stable IDs, never fabricating missing history."""
    if plan_id is not None and experiment_id is None:
        rows = (
            admin.table("experiment_plans").select("experiment_id")
            .eq("id", plan_id).limit(1).execute().data or []
        )
        if not rows:
            return []
        experiment_id = str(rows[0]["experiment_id"])
    if experiment_id is not None:
        experiment = (
            admin.table("experiments").select("id")
            .eq("id", experiment_id).eq("paper_id", paper_id).limit(1).execute().data
        )
        if not experiment:
            return []

    chains = [
        ("reproduction_specs", _ancestry(
            admin, "reproduction_specs", "paper_id", paper_id, spec_id
        )),
    ]
    if experiment_id is not None:
        chains.append(("experiment_plans", _ancestry(
            admin, "experiment_plans", "experiment_id", experiment_id, plan_id
        )))
    decisions: dict = {}
    for table, chain in chains:
        for row in chain:
            events = (
                (row.get("spec_json") or {}).get("review_decisions", [])
                if table == "reproduction_specs" else row.get("changes_json") or []
            )
            for index, event in enumerate(events):
                if not isinstance(event, dict):
                    continue
                payload = dict(event)
                if not payload.get("id"):
                    payload["id"] = uuid5(
                        NAMESPACE_URL, f"reproagent:review:{table}:{row['id']}:{index}"
                    )
                if not payload.get("timestamp"):
                    # Use persisted version time, never the current read time.
                    payload["timestamp"] = row.get("created_at")
                try:
                    decision = _safe_decision(ReviewDecision.model_validate(payload))
                except ValidationError:
                    logger.warning("Invalid review event in %s version %s", table, row["id"])
                    continue
                decisions.setdefault(decision.id, decision)
    return sorted(decisions.values(), key=lambda d: (
        d.timestamp.replace(tzinfo=UTC) if d.timestamp.tzinfo is None
        else d.timestamp.astimezone(UTC), str(d.id)
    ))


def decision_traces(
    decisions: list[ReviewDecision],
    *,
    workflow_id: str | None = None,
    experiment_id: str | None = None,
) -> list[NodeTrace]:
    """Project persisted review events into the existing node-trace wire format."""
    traces = []
    seen = set()
    for decision in decisions:
        if decision.id in seen:
            continue
        seen.add(decision.id)
        safe = _safe_decision(decision)
        payload = safe.model_dump(mode="json")
        traces.append(NodeTrace(
            id=safe.id,
            node_name="review_decision",
            workflow_id=workflow_id,
            experiment_id=experiment_id,
            input_json={"field_path": payload["field_path"], "before": payload["before"]},
            output_json={
                key: value for key, value in payload.items()
                if key not in {"id", "field_path", "before"}
            },
            created_at=safe.timestamp,
        ))
    return traces
