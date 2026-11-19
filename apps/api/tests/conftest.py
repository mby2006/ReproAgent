import sys
from pathlib import Path

# importlib import mode does not add the test directory to sys.path; expose it so
# test modules can share helpers (e.g. ``from test_runs import FakeAdmin``).
sys.path.insert(0, str(Path(__file__).parent))
