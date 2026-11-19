from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..config import get_settings
from ..llm.base import LLMAdapter
from ..llm.factory import get_llm_adapter
from . import nodes
from .state import AgentState

NODE_ORDER = [
    "understand_paper",
    "extract_spec",
    "plan_experiment",
    "generate_code",
    "run_experiment",
    "compare_results",
    "write_report",
]


def _resolve_llm(llm: LLMAdapter | None) -> LLMAdapter:
    return llm if llm is not None else get_llm_adapter(get_settings())


def build_graph(llm: LLMAdapter | None = None) -> CompiledStateGraph:
    builder = StateGraph(AgentState)
    builder.add_node("understand_paper", nodes.understand_paper)
    builder.add_node("extract_spec", nodes.make_extract_spec(_resolve_llm(llm)))
    for name in NODE_ORDER[2:]:
        builder.add_node(name, getattr(nodes, name))
    builder.add_edge(START, NODE_ORDER[0])
    for prev, nxt in zip(NODE_ORDER, NODE_ORDER[1:], strict=False):
        builder.add_edge(prev, nxt)
    builder.add_edge(NODE_ORDER[-1], END)
    return builder.compile()


def build_understanding_graph(llm: LLMAdapter) -> CompiledStateGraph:
    builder = StateGraph(AgentState)
    builder.add_node("understand_paper", nodes.understand_paper)
    builder.add_node("extract_spec", nodes.make_extract_spec(llm))
    builder.add_edge(START, "understand_paper")
    builder.add_edge("understand_paper", "extract_spec")
    builder.add_edge("extract_spec", END)
    return builder.compile()


def build_planning_graph() -> CompiledStateGraph:
    """Deterministic card -> plan; no LLM required."""
    builder = StateGraph(AgentState)
    builder.add_node("plan_experiment", nodes.plan_experiment)
    builder.add_edge(START, "plan_experiment")
    builder.add_edge("plan_experiment", END)
    return builder.compile()
