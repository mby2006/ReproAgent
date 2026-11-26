"""Graph assembly: 15 nodes, conditional repair/replan routing, human interrupts.

Every node is wrapped with tracing (credential-redacted input/output, duration,
model/token usage, error, retry count) and workflow-row persistence. The graph is
compiled with a checkpointer keyed by thread_id == workflow_id, so human gates
(interrupt) and crashes resume from the last completed node.
"""

import asyncio
import logging
import time
from uuid import UUID

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from reproagent_schemas import ErrorCategory, RunStatus, WorkflowStatus

from ..services.runs import execution_request_id, load_run_row
from .context import WorkflowContext
from .harness import BudgetExceeded, redact, trace_input, trace_output
from .nodes import (
    build_reproduction_spec,
    classify_error,
    evaluate_result,
    execute_code,
    extract_paper,
    generate_code,
    generate_report,
    human_approve_plan,
    human_review,
    ingest_paper,
    persist_results,
    plan_experiment,
    repair_code,
    validate_code,
    validate_evidence,
)
from .state import WorkflowState

logger = logging.getLogger(__name__)

NODE_ORDER = [
    ("ingest_paper", ingest_paper),
    ("extract_paper", extract_paper),
    ("build_reproduction_spec", build_reproduction_spec),
    ("validate_evidence", validate_evidence),
    ("human_review", human_review),
    ("plan_experiment", plan_experiment),
    ("human_approve_plan", human_approve_plan),
    ("generate_code", generate_code),
    ("validate_code", validate_code),
    ("execute_code", execute_code),
    ("classify_error", classify_error),
    ("repair_code", repair_code),
    ("evaluate_result", evaluate_result),
    ("generate_report", generate_report),
    ("persist_results", persist_results),
]


def _traced(name: str, ctx: WorkflowContext, fn):
    async def wrapped(state: WorkflowState) -> dict:
        ctx.admin.table("workflows").update({"current_node": name}).eq(
            "id", state["workflow_id"]
        ).execute()
        started = time.monotonic()
        captured_input = trace_input(state)
        try:
            updates = await fn(state, ctx)
        except GraphInterrupt:
            raise
        except Exception as exc:
            duration = int((time.monotonic() - started) * 1000)
            ctx.write_trace(
                state,
                name,
                captured_input,
                {},
                duration,
                None,
                None,
                str(exc),
                state.get("repair_attempt", 0),
            )
            raise
        duration = int((time.monotonic() - started) * 1000)
        usages = updates.get("usages") or []
        usage = usages[-1] if usages else None
        ctx.write_trace(
            state,
            name,
            captured_input,
            trace_output(updates),
            duration,
            usage.model_name if usage else None,
            usage,
            None,
            updates.get("repair_attempt", state.get("repair_attempt", 0)),
        )
        ctx.persist_workflow(state, updates, name)
        return updates

    return wrapped


def route_after_validate(state: WorkflowState) -> str:
    if state.get("validation_ok"):
        return "execute_code"
    classification = state.get("classification")
    config = state["budget_config"]
    if (
        classification is not None
        and classification.repairable
        and state.get("repair_attempt", 0) < config.max_repairs
    ):
        return "repair_code"
    return "persist_results"


def route_after_execute(state: WorkflowState) -> str:
    if state.get("budget_exhausted"):
        return "evaluate_result"
    result = state.get("execution_result")
    if result is None:
        return "persist_results"
    if result.status is RunStatus.SUCCEEDED:
        return "evaluate_result"
    if result.status is RunStatus.CANCELLED:
        return "persist_results"
    return "classify_error"


def route_after_classify(state: WorkflowState) -> str:
    classification = state["classification"]
    config = state["budget_config"]
    if not classification.repairable:
        return "evaluate_result"
    if state.get("repair_attempt", 0) >= config.max_repairs:
        return "evaluate_result"
    if (
        classification.category is ErrorCategory.OUT_OF_MEMORY
        and state.get("replan_count", 0) >= config.max_replans
    ):
        return "evaluate_result"
    return "repair_code"


def route_after_extract(state: WorkflowState) -> str:
    if state.get("error"):
        return "persist_results"
    return "build_reproduction_spec"


def route_after_spec(state: WorkflowState) -> str:
    if state.get("error"):
        return "persist_results"
    return "validate_evidence"


def build_workflow_graph(ctx: WorkflowContext, checkpointer):
    graph = StateGraph(WorkflowState)
    for name, fn in NODE_ORDER:
        graph.add_node(name, _traced(name, ctx, fn))
    graph.add_edge(START, "ingest_paper")
    graph.add_edge("ingest_paper", "extract_paper")
    graph.add_conditional_edges(
        "extract_paper",
        route_after_extract,
        {
            "build_reproduction_spec": "build_reproduction_spec",
            "persist_results": "persist_results",
        },
    )
    graph.add_conditional_edges(
        "build_reproduction_spec",
        route_after_spec,
        {
            "validate_evidence": "validate_evidence",
            "persist_results": "persist_results",
        },
    )
    graph.add_edge("validate_evidence", "human_review")
    graph.add_edge("human_review", "plan_experiment")
    graph.add_edge("plan_experiment", "human_approve_plan")
    graph.add_edge("human_approve_plan", "generate_code")
    graph.add_edge("generate_code", "validate_code")
    graph.add_conditional_edges(
        "validate_code",
        route_after_validate,
        {
            "execute_code": "execute_code",
            "repair_code": "repair_code",
            "persist_results": "persist_results",
        },
    )
    graph.add_conditional_edges(
        "execute_code",
        route_after_execute,
        {
            "evaluate_result": "evaluate_result",
            "classify_error": "classify_error",
            "persist_results": "persist_results",
        },
    )
    graph.add_conditional_edges(
        "classify_error",
        route_after_classify,
        {"repair_code": "repair_code", "evaluate_result": "evaluate_result"},
    )
    graph.add_edge("repair_code", "validate_code")
    graph.add_edge("evaluate_result", "generate_report")
    graph.add_edge("generate_report", "persist_results")
    graph.add_edge("persist_results", END)
    return graph.compile(checkpointer=checkpointer)


async def _invoke(graph, payload, config: dict, ctx: WorkflowContext, fallback: dict) -> None:
    try:
        await graph.ainvoke(payload, config)
    except asyncio.CancelledError:
        # Cleanup is best effort: database/checkpoint failures must never replace
        # the task's cancellation exception. The deterministic request id covers
        # acceptance before the accepted Runner id could be persisted.
        try:
            current = (await graph.aget_state(config)).values or fallback
        except Exception:
            current = fallback
        runner_run_id = current.get("runner_run_id") or fallback.get("runner_run_id")
        try:
            if not runner_run_id and current.get("run_id"):
                run_row = load_run_row(ctx.admin, current["run_id"])
                runner_run_id = run_row.get("runner_run_id") if run_row else None
                if not runner_run_id and current.get("code_version"):
                    runner_run_id = execution_request_id(
                        UUID(current["run_id"]), current.get("repair_attempt", 0)
                    )
        except Exception:
            logger.warning("workflow %s cancellation lookup failed", current.get("workflow_id"))
        if runner_run_id:
            try:
                await ctx.runner.cancel_run(UUID(str(runner_run_id)))
            except Exception:
                logger.warning(
                    "workflow %s Runner cancellation failed",
                    current.get("workflow_id"),
                )
        try:
            ctx.finalize_workflow(current, WorkflowStatus.CANCELLED, "workflow cancelled")
        except Exception:
            logger.warning(
                "workflow %s cancellation persistence failed", current.get("workflow_id")
            )
        raise
    except BudgetExceeded as exc:
        current = (await graph.aget_state(config)).values or fallback
        ctx.finalize_workflow(current, WorkflowStatus.FAILED, str(exc))
    except Exception as exc:
        current = (await graph.aget_state(config)).values or fallback
        ctx.finalize_workflow(current, WorkflowStatus.FAILED, str(exc))
        logger.error("workflow %s failed: %s", fallback.get("workflow_id"), redact(str(exc)))


async def start_workflow(ctx: WorkflowContext, checkpointer, state: WorkflowState) -> None:
    graph = build_workflow_graph(ctx, checkpointer)
    config = {"configurable": {"thread_id": state["thread_id"]}}
    await _invoke(graph, state, config, ctx, state)


async def resume_workflow(
    ctx: WorkflowContext, checkpointer, thread_id: str, payload: dict, fallback: dict
) -> None:
    graph = build_workflow_graph(ctx, checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    await _invoke(graph, Command(resume=payload), config, ctx, fallback)


async def continue_workflow(
    ctx: WorkflowContext, checkpointer, thread_id: str, fallback: dict
) -> None:
    """Resume from the last checkpoint after a crash/restart (no human input)."""
    graph = build_workflow_graph(ctx, checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    await _invoke(graph, None, config, ctx, fallback)


async def drive_from_execution(ctx: WorkflowContext, checkpointer, state: WorkflowState) -> None:
    """Enter the unified graph at generate_code for an already-approved plan.

    Seeds the checkpoint as if human_approve_plan just completed, so the legacy
    runs-router entry shares the exact same execution/repair/evaluate pipeline.
    """
    graph = build_workflow_graph(ctx, checkpointer)
    config = {"configurable": {"thread_id": state["thread_id"]}}
    await graph.aupdate_state(config, state, as_node="human_approve_plan")
    await _invoke(graph, None, config, ctx, state)
