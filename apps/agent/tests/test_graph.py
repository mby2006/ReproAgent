import pytest
from reproagent_agent.graph.build import NODE_ORDER, build_graph
from reproagent_agent.graph.nodes import (
    compare_results,
    generate_code,
    run_experiment,
    write_report,
)


def test_graph_compiles_with_expected_nodes():
    graph = build_graph()
    node_names = set(graph.get_graph().nodes) - {"__start__", "__end__"}
    assert node_names == set(NODE_ORDER)


async def test_understand_paper_requires_pdf():
    graph = build_graph()
    with pytest.raises(ValueError, match="pdf_b64"):
        await graph.ainvoke({"job_id": "j", "user_id": "u", "paper_title": "LeNet-5"})


@pytest.mark.parametrize("node", [generate_code, run_experiment, compare_results, write_report])
async def test_later_phase_nodes_still_stubs(node):
    with pytest.raises(NotImplementedError):
        await node({})
