from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..llm.base import LLMAdapter
from . import nodes
from .state import AgentState


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
