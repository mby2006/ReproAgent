import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def audit_module():
    path = Path(__file__).with_name("audit_legacy_consumers.py")
    if not path.exists():
        pytest.fail("legacy consumer audit script is unavailable")
    spec = importlib.util.spec_from_file_location("task14_audit_legacy_consumers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION_NAMES = {
    "20261126090000_attempt_history.sql",
    "20261126091000_run_authorization.sql",
    "20261126092000_report_versions.sql",
    "20261126103000_service_role_authorization.sql",
    "20261126104000_atomic_result_persistence.sql",
}


def write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    marker = root / "executed.txt"
    write(
        root,
        "apps/agent/src/reproagent_agent/main.py",
        """from reproagent_agent.clients import runner
from reproagent_agent.routers import reproductions
from reproagent_agent.graph.build import build_graph

app = object()
app.include_router(reproductions.router)
app.state.graph = build_graph()
client.post("/v1/jobs/legacy")
""",
    )
    write(
        root,
        "apps/agent/src/reproagent_agent/clients/runner.py",
        f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n',
    )
    write(
        root,
        "apps/agent/src/reproagent_agent/routers/reproductions.py",
        """from fastapi import APIRouter
router = APIRouter(prefix="/v1")
@router.post("/reproductions")
async def start():
    return None
@router.get("/jobs/{job_id}")
async def job():
    return None
""",
    )
    write(
        root,
        "apps/agent/src/reproagent_agent/graph/build.py",
        """def build_graph():
    return None
def build_understanding_graph(llm):
    return None
def build_planning_graph():
    return None
""",
    )
    write(root, "apps/web/src/api.ts", 'const jobUrl = "/v1/jobs";\nfetch(jobUrl);\n')
    write(
        root,
        "tests/test_legacy.py",
        'import reproagent_agent.clients.runner\nclient.get("/v1/jobs/1")\n',
    )
    write(
        root,
        "README.md",
        "Legacy `reproagent_agent.clients.runner`, `/v1/reproductions`, and `/v1/jobs`.\n",
    )
    write(root, "docker-compose.yml", "command: python -m reproagent_agent.clients.runner\n")
    canonical = root / "infra/supabase/migrations"
    deploy = root / "deploy/server/supabase/migrations"
    for name in sorted(MIGRATION_NAMES):
        content = f"-- canonical migration {name}\nselect 1;\n"
        write(canonical, name, content)
        write(deploy, name, content)
    return root


def classifications(report, target):
    return {consumer["classification"] for consumer in report["targets"][target]["consumers"]}


def test_audit_classifies_python_compose_frontend_and_docs_consumers(repository, audit_module):
    report = audit_module.audit_repository(repository)
    runner = report["targets"]["reproagent_agent.clients.runner"]
    assert classifications(report, "reproagent_agent.clients.runner") == {
        "production",
        "test",
        "docs-only",
    }
    assert {consumer["kind"] for consumer in runner["consumers"]} >= {
        "python-import",
        "compose-command",
        "documentation-reference",
    }
    assert classifications(report, "/v1/jobs") == {"production", "test", "docs-only"}
    assert any(c["kind"] == "frontend-api-call" for c in report["targets"]["/v1/jobs"]["consumers"])
    assert report["ok"] is False
    assert any(c["blocking"] for c in runner["consumers"])
    assert all(
        not c["blocking"] for c in runner["consumers"] if c["classification"] != "production"
    )


def test_audit_distinguishes_live_registration_from_dormant_retained_files(
    repository, audit_module
):
    report = audit_module.audit_repository(repository)
    target = report["targets"]["reproagent_agent.routers.reproductions"]
    assert target["status"] == "live-registered"
    assert target["live_routes"] == [
        {
            "path": "/v1/jobs/{job_id}",
            "methods": ["GET"],
            "registered_in": "apps/agent/src/reproagent_agent/main.py",
            "router_module": "reproagent_agent.routers.reproductions",
        },
        {
            "path": "/v1/reproductions",
            "methods": ["POST"],
            "registered_in": "apps/agent/src/reproagent_agent/main.py",
            "router_module": "reproagent_agent.routers.reproductions",
        },
    ]
    assert target["retained_files"] == ["apps/agent/src/reproagent_agent/routers/reproductions.py"]
    assert report["targets"]["reproagent_agent.graph"]["status"] == "retained-active"


def test_nonregistered_targets_allow_test_and_docs_references(repository, audit_module):
    write(
        repository,
        "apps/agent/src/reproagent_agent/main.py",
        """from reproagent_agent.graph.build import build_understanding_graph, build_planning_graph
app = object()
app.understanding_graph = build_understanding_graph(None)
app.planning_graph = build_planning_graph()
""",
    )
    write(repository, "apps/web/src/api.ts", 'const workflowUrl = "/v1/workflows";\n')
    report = audit_module.audit_repository(repository)
    for target in (
        "reproagent_agent.clients.runner",
        "reproagent_agent.routers.reproductions",
        "/v1/jobs",
        "/v1/reproductions",
    ):
        assert report["targets"][target]["status"] == "retired-by-nonregistration"
        assert report["targets"][target]["live_routes"] == []
    assert not any(c["blocking"] for c in report["targets"]["/v1/jobs"]["consumers"])
    assert report["targets"]["/v1/jobs"]["consumers"]


def test_production_consumer_blocks_retirement(repository, audit_module):
    report = audit_module.audit_repository(repository)
    blocking = [
        consumer
        for consumer in report["targets"]["reproagent_agent.clients.runner"]["consumers"]
        if consumer["blocking"]
    ]
    assert blocking
    assert all(c["classification"] == "production" for c in blocking)
    assert report["ok"] is False


def test_matching_migrations_have_exact_hashes(repository, audit_module):
    result = audit_module.audit_repository(repository)["migrations"]
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["extra"] == []
    assert result["mismatched"] == []
    assert set(result["canonical_sha256"]) == MIGRATION_NAMES
    for name in MIGRATION_NAMES:
        expected = hashlib.sha256(
            (repository / "infra/supabase/migrations" / name).read_bytes()
        ).hexdigest()
        assert result["canonical_sha256"][name] == expected
        assert result["deploy_sha256"][name] == expected


def test_missing_extra_and_divergent_migrations_fail(repository, audit_module):
    deploy = repository / "deploy/server/supabase/migrations"
    missing = sorted(MIGRATION_NAMES)[0]
    (deploy / missing).unlink()
    (deploy / "20261127000000_extra.sql").write_text("select 1;\n")
    divergent = sorted(MIGRATION_NAMES)[1]
    (deploy / divergent).write_text("-- changed\n")
    result = audit_module.audit_repository(repository)["migrations"]
    assert result["ok"] is False
    assert result["missing"] == [missing]
    assert result["extra"] == ["20261127000000_extra.sql"]
    assert result["mismatched"] == [divergent]


def test_audit_never_executes_discovered_python_or_shell(repository, audit_module):
    assert audit_module.audit_repository(repository)["ok"] is False
    assert not (repository / "executed.txt").exists()


def test_cli_writes_requested_json_and_human_output(tmp_path, repository, audit_module):
    json_output = tmp_path / "acceptance/legacy-consumers.json"
    result = audit_module.main(
        ["--root", str(repository), "--format", "json", "--output", str(json_output)]
    )
    assert result == 1
    payload = json.loads(json_output.read_text())
    assert payload == audit_module.audit_repository(repository)

    human_output = tmp_path / "acceptance/legacy-consumers.txt"
    result = audit_module.main(
        ["--root", str(repository), "--format", "human", "--output", str(human_output)]
    )
    assert result == 1
    text = human_output.read_text()
    assert "live-registered" in text
    assert "reproagent_agent.clients.runner" in text
    assert "Migrations: PASS" in text


def test_migration_only_gate_returns_nonzero_for_mismatch(repository):
    (repository / "deploy/server/supabase/migrations" / sorted(MIGRATION_NAMES)[0]).write_text(
        "bad"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("audit_legacy_consumers.py")),
            "--root",
            str(repository),
            "--check-migrations",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "migration" in result.stdout.lower()


def test_repository_audit_is_clean_and_safe(audit_module):
    root = Path(__file__).resolve().parents[1]
    report = audit_module.audit_repository(root)
    assert report["ok"] is True, report
    assert report["targets"]["reproagent_agent.graph"]["status"] == "retained-active"
    assert report["migrations"]["ok"] is True
