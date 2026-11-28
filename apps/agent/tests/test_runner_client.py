import sys

from reproagent_agent.main import create_app


def test_agent_startup_does_not_import_retired_runner_client():
    assert "reproagent_agent.clients.runner" not in sys.modules
    app = create_app()
    assert "reproagent_agent.clients.runner" not in sys.modules
    assert not hasattr(app.state, "graph")
