from pathlib import Path

import pytest

INTEGRATION_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.is_relative_to(INTEGRATION_DIR):
            item.add_marker(pytest.mark.integration)
