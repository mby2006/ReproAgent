import pytest
from reproagent_agent.graph import build
from reproagent_agent.graph.build import build_planning_graph, build_understanding_graph
from reproagent_schemas import PlanResponse, ReproductionCard


class NullLLM:
    async def complete(self, messages, *, temperature=0.2, max_tokens=2048) -> str:
        raise AssertionError("missing PDF must fail before extraction")


def explicit(value):
    return {
        "value": value,
        "status": "paper_explicit",
        "evidence_quote": f"...{value}...",
        "source_page": 1,
    }


def test_understanding_graph_contains_only_active_nodes():
    graph = build_understanding_graph(NullLLM())
    node_names = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert node_names == {"understand_paper", "extract_spec"}


async def test_understanding_graph_requires_pdf_before_extraction():
    graph = build_understanding_graph(NullLLM())
    with pytest.raises(ValueError, match="pdf_b64"):
        await graph.ainvoke({"job_id": "j", "user_id": "u", "paper_title": "LeNet-5"})


async def test_planning_graph_is_deterministic_without_llm():
    graph = build_planning_graph()
    node_names = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert node_names == {"plan_experiment"}
    result = await graph.ainvoke(
        {
            "card": ReproductionCard(
                method=explicit("LeNet-5 gradient-based learning"),
                model_architecture=explicit("LeNet-5 convolutional network"),
                dataset=explicit("MNIST handwritten digits"),
                optimizer=explicit("SGD"),
                loss_function=explicit("cross-entropy"),
                hyperparameters={"learning_rate": explicit("0.001")},
                evaluation_metrics=[explicit("accuracy")],
            )
        }
    )
    assert PlanResponse(plan=result["plan"]).plan.model_cfg.architecture == "lenet-5"


def test_full_workflow_builder_and_placeholder_nodes_are_absent():
    assert not hasattr(build, "build_graph")
    assert not hasattr(build, "NODE_ORDER")
    assert not hasattr(build, "_resolve_llm")
    from reproagent_agent.graph import nodes

    for name in ("generate_code", "run_experiment", "compare_results", "write_report"):
        assert not hasattr(nodes, name)
