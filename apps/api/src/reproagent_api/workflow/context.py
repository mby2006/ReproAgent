"""Per-invocation dependencies shared by every workflow node.

The context owns the Supabase service client (all DB writes), the Agent and
Runner HTTP clients, and the budget configuration. It also centralizes workflow
row persistence and trace writing so individual nodes stay focused on compute.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from reproagent_schemas import BudgetConfig, LLMUsage, RunStatus, WorkflowStatus

from ..clients.agent import AgentClient
from ..clients.runner import RunnerClient
from ..config import ApiSettings
from ..services.runs import finalize_execution_run, load_run_row
from .harness import WORKFLOW_PERSIST_KEYS, redact


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class WorkflowContext:
    admin: object
    agent: AgentClient
    runner: RunnerClient
    settings: ApiSettings
    budget_config: BudgetConfig = field(default_factory=BudgetConfig)

    def set_status(self, state: dict, status: WorkflowStatus, node: str) -> dict:
        """Persist an awaiting/phase status immediately (used before interrupt())."""
        self.admin.table("workflows").update(
            {"status": status.value, "current_node": node, "updated_at": _now()}
        ).eq("id", state["workflow_id"]).execute()
        return {"status": status.value, "current_node": node}

    def persist_workflow(self, state: dict, updates: dict, node: str) -> None:
        row = {key: updates[key] for key in WORKFLOW_PERSIST_KEYS if key in updates}
        if row.get("error") is not None:
            row["error"] = redact(row["error"])
        row["current_node"] = node
        row["updated_at"] = _now()
        if "budget" in updates:
            row["budget_json"] = updates["budget"].model_dump(mode="json")
        self.admin.table("workflows").update(row).eq("id", state["workflow_id"]).execute()

    def write_trace(
        self,
        state: dict,
        node: str,
        input_json: dict,
        output_json: dict,
        duration_ms: int,
        model_name: str | None,
        usage: LLMUsage | None,
        error: str | None,
        retry_count: int,
    ) -> None:
        self.admin.table("agent_traces").insert(
            {
                "workflow_id": state["workflow_id"],
                "experiment_id": state.get("experiment_id"),
                "run_id": state.get("run_id"),
                "node_name": node,
                "input_json": redact(input_json),
                "output_json": redact(output_json),
                "duration_ms": duration_ms,
                "model_name": model_name,
                "token_usage": usage.model_dump(mode="json") if usage else None,
                "error": redact(error) if error else None,
                "retry_count": retry_count,
            }
        ).execute()

    def finalize_workflow(
        self, state: dict, status: WorkflowStatus, error: str | None = None
    ) -> None:
        row = {"status": status.value, "updated_at": _now()}
        if error is not None:
            row["error"] = redact(error)
        if "budget" in state:
            row["budget_json"] = state["budget"].model_dump(mode="json")
        self.admin.table("workflows").update(row).eq("id", state["workflow_id"]).in_(
            "status",
            [
                status.value
                for status in WorkflowStatus
                if status
                not in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED)
            ],
        ).execute()
        attempt = None
        if state.get("run_id"):
            # Static-validation repair can advance workflow state before any
            # execution attempt owns the row; finalize the row's actual attempt.
            run_row = load_run_row(self.admin, state["run_id"])
            attempt = run_row.get("repair_attempt", 0) if run_row else 0
        if status in (WorkflowStatus.FAILED, WorkflowStatus.CANCELLED) and attempt is not None:
            finalize_execution_run(
                self.admin,
                state["run_id"],
                RunStatus.CANCELLED if status is WorkflowStatus.CANCELLED else RunStatus.FAILED,
                error,
                attempt=attempt,
            )
