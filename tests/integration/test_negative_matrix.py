"""G-03/G-04 negative matrix against disposable real Postgres.

Workflow nodes, authorization SQL, RLS, repair/OOM planning, attempt pinning and
report versioning are real. LLM/queue/checkpointer/Runner I/O are deterministic
fixtures; no generated program executes in this process and no shared database is
used. Run with REPRO_TEST_DATABASE_TEMPLATE=reproagent_task8_template.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb
from reproagent_agent.codegen import (
    classify_error,
    generate_from_plan,
    repair_bundle,
    validate_bundle,
)
from reproagent_agent.evaluation import evaluate_run
from reproagent_agent.planning import build_plan
from reproagent_api.clients.agent import AgentClient
from reproagent_api.clients.runner import RunnerClient
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.services.runs import create_oom_plan
from reproagent_api.workflow import (
    WorkflowContext,
    initial_state,
    make_memory_checkpointer,
)
from reproagent_api.workflow.graph import build_workflow_graph
from reproagent_schemas import (
    ClassifyRequest,
    CodeBundle,
    EvaluateRequest,
    ExperimentPlan,
    RepairRequest,
    RunAccepted,
    RunResult,
    RunStatus,
    WorkflowStatus,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_execution import make_card  # noqa: E402

pytestmark = pytest.mark.integration


class Result:
    def __init__(self, data, *, single=False, scalar=None):
        if scalar is not None:
            self.data = scalar
            return
        rows = [
            {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()}
            for row in data
        ]
        self.data = (rows[0] if rows else None) if single else rows


def adapt(value):
    return Jsonb(value) if isinstance(value, (dict, list)) else value


class Query:
    def __init__(self, admin, table):
        self.admin, self.table = admin, table
        self.columns = "*"
        self.filters = []
        self.order_by = None
        self.limit_count = None
        self.operation = "select"
        self.payload = None
        self.on_conflict = None
        self.ignore_duplicates = False
        self.want_single = False
        self.not_ = self

    def select(self, columns):
        self.columns = columns
        return self

    def eq(self, key, value):
        self.filters.append((key, "=", value))
        return self

    def neq(self, key, value):
        self.filters.append((key, "!=", value))
        return self

    def in_(self, key, values):
        self.filters.append((key, "in", list(values)))
        return self

    def lte(self, key, value):
        self.filters.append((key, "<=", value))
        return self

    def order(self, key, *, desc=False):
        self.order_by = (key, desc)
        return self

    def limit(self, count):
        self.limit_count = count
        return self

    def insert(self, payload):
        self.operation, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.operation, self.payload = "update", payload
        return self

    def upsert(self, payload, *, on_conflict=None, ignore_duplicates=False):
        self.operation, self.payload = "upsert", payload
        self.on_conflict, self.ignore_duplicates = on_conflict, ignore_duplicates
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def single(self):
        self.want_single = True
        return self

    def execute(self):
        cursor, table = self.admin.cursor, sql.Identifier(self.table)
        where, params = where_clause(self.filters)
        if self.operation == "select":
            relation = ownership_join(self.table, self.columns)
            joined = relation is not None
            where, params = where_clause(self.filters, joined)
            statement = sql.SQL("select {} from {}{} {}").format(
                sql.SQL("t.*" if joined else self.columns),
                sql.Identifier(self.table),
                relation or sql.SQL(""),
                where,
            )
            if self.order_by:
                key = self.order_by[0]
                if joined:
                    key = joined_key(key)
                statement += sql.SQL(" order by {} {}").format(
                    identifier(key),
                    sql.SQL("desc" if self.order_by[1] else "asc"),
                )
            if self.limit_count is not None:
                statement += sql.SQL(" limit {}").format(sql.Literal(self.limit_count))
            rows = cursor.execute(statement, params).fetchall()
        elif self.operation in {"insert", "upsert"}:
            records = self.payload if isinstance(self.payload, list) else [self.payload]
            if not records:
                rows = []
                statement = sql.SQL("select * from {} where false").format(table)
            else:
                values = list(records[0])
                if any(list(record) != values for record in records):
                    raise ValueError("bulk insert columns must match")
                statement = sql.SQL("insert into {} ({}) values {}").format(
                    table,
                    sql.SQL(", ").join(map(sql.Identifier, values)),
                    sql.SQL(", ").join(
                        sql.SQL("({})").format(
                            sql.SQL(", ").join(sql.Placeholder() for _ in values)
                        )
                        for _ in records
                    ),
                )
                if self.operation == "upsert":
                    conflict = self.on_conflict or "id"
                    action = (
                        "do nothing"
                        if self.ignore_duplicates
                        else (
                            "do update set "
                            + ", ".join(
                                f"{key} = excluded.{key}"
                                for key in values
                                if key not in conflict.split(",")
                            )
                        )
                    )
                    statement += sql.SQL(" on conflict ({}) {}").format(
                        sql.SQL(conflict), sql.SQL(action)
                    )
                statement += sql.SQL(" returning *")
            rows = cursor.execute(
                statement,
                [adapt(record[key]) for record in records for key in values],
            ).fetchall()
        elif self.operation == "update":
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(key), sql.Placeholder())
                for key in self.payload
            )
            rows = cursor.execute(
                sql.SQL("update {} set {} {} returning *").format(table, assignments, where),
                [adapt(value) for value in self.payload.values()] + params,
            ).fetchall()
        else:
            rows = cursor.execute(
                sql.SQL("delete from {} {} returning *").format(table, where), params
            ).fetchall()
        return Result(rows, single=self.want_single)


def identifier(key):
    return sql.SQL(".").join(map(sql.Identifier, key.split(".")))


def ownership_join(table, columns):
    """Translate the narrow PostgREST ownership joins used by API read guards."""
    if "projects!inner" not in columns:
        return None
    prefix = sql.SQL(" as t")
    if table in {"experiment_plans", "generated_files"}:
        prefix += sql.SQL(" join public.experiments e on e.id = t.experiment_id")
    elif table == "execution_runs" and "experiments!inner" in columns:
        prefix += sql.SQL(" join public.experiments e on e.id = t.experiment_id")
    elif table == "reproduction_specs":
        prefix += sql.SQL(" join public.papers pa on pa.id = t.paper_id")
        prefix += sql.SQL(" join public.projects p on p.id = pa.project_id")
        return prefix
    if table in {"experiments", "papers"}:
        return prefix + sql.SQL(" join public.projects p on p.id = t.project_id")
    if table in {"experiment_plans", "generated_files"} or table == "execution_runs":
        return prefix + sql.SQL(" join public.projects p on p.id = e.project_id")
    return prefix + sql.SQL(" join public.projects p on p.id = t.project_id")


def joined_key(key):
    if key.endswith("projects.user_id"):
        return "p.user_id"
    if key.startswith("projects."):
        return "p." + key.removeprefix("projects.")
    return "t." + key


def where_clause(filters, joined=False):
    clauses, params = [], []
    for key, operator, value in filters:
        if joined:
            key = joined_key(key)
        if operator == "in":
            clauses.append(sql.SQL("{} = any({})").format(sql.Identifier(key), sql.Placeholder()))
            params.append(value)
        else:
            clauses.append(
                sql.SQL("{} {} {}").format(
                    identifier(key), sql.SQL(operator), sql.Placeholder()
                )
            )
            params.append(value)
    return (
        sql.SQL("where ").format() + sql.SQL(" and ").join(clauses) if clauses else sql.SQL(""),
        params,
    )


class Rpc:
    def __init__(self, admin, name, params):
        self.admin, self.name, self.params = admin, name, params

    def execute(self):
        cursor = self.admin.cursor.execute(
            sql.SQL("select * from public.{}({})").format(
                sql.Identifier(self.name),
                sql.SQL(", ").join(
                    sql.SQL("{} := {}::{}").format(
                        identifier(key),
                        sql.Placeholder(),
                        sql.SQL(
                            {
                                "p_run_id": "uuid",
                                "p_experiment_id": "uuid",
                                "p_plan_id": "uuid",
                                "p_plan_version": "integer",
                                "p_actor_id": "uuid",
                                "p_file_ids": "uuid[]",
                                "p_report_json": "jsonb",
                                "p_artifact_rows": "jsonb",
                                "p_metrics_json": "jsonb",
                                "p_audit_json": "jsonb",
                                "p_attempt": "integer",
                                "p_generation_ms": "numeric",
                                "p_execution_time": "numeric",
                                "p_exit_code": "integer",
                                "p_repair_attempt": "integer",
                                "p_require_latest": "boolean",
                            }.get(key, "text")
                        ),
                    )
                    for key in self.params
                ),
            ),
            [
                Jsonb(value)
                if isinstance(value, dict)
                or (isinstance(value, list) and key == "p_artifact_rows")
                else value
                for key, value in self.params.items()
            ],
        )
        if cursor.description and len(cursor.description) == 1:
            value = next(iter(cursor.fetchone().values()))
            return Result([], scalar=value if value is not None else False)
        return Result(cursor.fetchall())


class StorageBucket:
    def __init__(self, admin, name):
        self.admin, self.name = admin, name

    def upload(self, path, data, options=None):
        self.admin.cursor.execute(
            "delete from storage.objects where bucket_id=%s and name=%s",
            (self.name, path),
        )
        self.admin.cursor.execute(
            "insert into storage.objects (id, bucket_id, name) values (%s, %s, %s)",
            (uuid4(), self.name, path),
        )
        self.admin.files[path] = data
        return Result([])

    def download(self, path):
        if path not in self.admin.files:
            raise FileNotFoundError(path)
        return self.admin.files[path]


class Storage:
    def __init__(self, admin):
        self.admin = admin

    def from_(self, name):
        return StorageBucket(self.admin, name)

    def create_bucket(self, name, options=None):
        self.admin.cursor.execute(
            "insert into storage.buckets (id, name, public) values (%s, %s, %s) "
            "on conflict (id) do nothing",
            (name, name, False),
        )


class DbAdmin:
    """Narrow PostgREST-shaped adapter executing every operation in real SQL."""

    def __init__(self, cursor):
        self.cursor, self.files = cursor, {}
        self.storage = Storage(self)

    def table(self, name):
        return Query(self, name)

    def rpc(self, name, params):
        return Rpc(self, name, params)


class RunnerFixture:
    def __init__(self, outcomes, *, on_accept=None, on_poll=None):
        self.outcomes, self.results = list(outcomes), {}
        self.requests, self.cancelled = [], []
        self.on_accept, self.on_poll = on_accept, on_poll

    def transport(self):
        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/cancel"):
                run_id = UUID(request.url.path.rstrip("/").rsplit("/", 2)[-2])
                self.cancelled.append(run_id)
                result = self.results[run_id].model_copy(update={"status": RunStatus.CANCELLED})
                self.results[run_id] = result
                return httpx.Response(200, json=result.model_dump(mode="json"))
            if request.method == "POST":
                body = json.loads(request.content)
                run_id = UUID(body["request_id"])
                self.requests.append(body)
                self.results[run_id] = self.outcomes.pop(0).model_copy(update={"run_id": run_id})
                if self.on_accept is not None:
                    self.on_accept(run_id)
                accepted = RunAccepted(run_id=run_id, status=RunStatus.QUEUED)
                return httpx.Response(202, json=accepted.model_dump(mode="json"))
            if request.url.path.endswith("/artifacts/metrics.json"):
                return self.artifact(UUID(request.url.path.split("/")[2]), "metrics.json")
            run_id = UUID(request.url.path.rsplit("/", 1)[-1])
            if self.on_poll is not None:
                self.on_poll(run_id)
            result = self.results[run_id]
            return httpx.Response(200, json=result.model_dump(mode="json"))

        return httpx.MockTransport(handler)

    def artifact(self, run_id, name):
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "schema_version": "1.0",
                    "target_metric": "accuracy",
                    "metric_value": 0.9,
                    "accuracy": 0.9,
                }
            ).encode(),
        )


def agent_transport():
    def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/codegen/validate":
            payload = validate_bundle(CodeBundle.model_validate(body["bundle"])).model_dump(
                mode="json"
            )
        elif path == "/v1/codegen/classify":
            payload = classify_error(ClassifyRequest.model_validate(body)).model_dump(mode="json")
        elif path == "/v1/codegen/repair":
            payload = repair_bundle(RepairRequest.model_validate(body)).model_dump(mode="json")
        elif path == "/v1/codegen":
            plan = ExperimentPlan.model_validate(body["plan"])
            payload = {"bundle": generate_from_plan(plan).model_dump(mode="json")}
        elif path == "/v1/plan":
            payload = {"plan": build_plan(body["card"]).model_dump(mode="json")}
        elif path == "/v1/evaluate":
            payload = evaluate_run(EvaluateRequest.model_validate(body)).model_dump(mode="json")
        else:
            return httpx.Response(404, json={"detail": path})
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


class Harness:
    def __init__(self, db, outcomes):
        self.db, self.admin = db, DbAdmin(db)
        self.card, self.plan = make_card(), None
        self.plan = build_plan(self.card)
        self.ids = {
            name: uuid4()
            for name in (
                "owner",
                "other",
                "project",
                "other_project",
                "paper",
                "spec",
                "experiment",
                "plan",
                "workflow",
            )
        }
        self.runner = RunnerFixture(outcomes)
        self.settings = ApiSettings(
            supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
        )
        self.ctx = WorkflowContext(
            admin=self.admin,
            agent=AgentClient(self.settings, transport=agent_transport()),
            runner=RunnerClient(self.settings, transport=self.runner.transport()),
            settings=self.settings,
        )
        self.checkpointer = make_memory_checkpointer()
        self.seed()

    def seed(self):
        c, ids = self.db, self.ids
        c.execute(
            "insert into auth.users (id,email) values (%s,%s),(%s,%s)",
            (
                str(ids["owner"]),
                f"{ids['owner']}@example.test",
                str(ids["other"]),
                f"{ids['other']}@example.test",
            ),
        )
        c.execute("set local role service_role")
        role = c.execute(
            "select current_user, rolsuper, rolbypassrls from pg_roles where rolname=current_user"
        ).fetchone()
        assert role == {"current_user": "service_role", "rolsuper": False, "rolbypassrls": True}
        c.execute(
            "insert into public.projects (id,user_id,name) values (%s,%s,'matrix'),(%s,%s,'other')",
            (str(ids["project"]), str(ids["owner"]), str(ids["other_project"]), str(ids["other"])),
        )
        c.execute(
            "insert into public.papers (id,project_id,title,pdf_path) values (%s,%s,'matrix',%s)",
            (str(ids["paper"]), str(ids["project"]), "matrix/paper.pdf"),
        )
        c.execute(
            "insert into public.reproduction_specs "
            "(id,paper_id,version,spec_json,status,origin) "
            "values (%s,%s,1,%s,'confirmed','extraction')",
            (str(ids["spec"]), str(ids["paper"]), Jsonb(self.card.model_dump(mode="json"))),
        )
        c.execute(
            "insert into public.experiments (id,project_id,paper_id,spec_id,name,status) "
            "values (%s,%s,%s,%s,'matrix','running')",
            (str(ids["experiment"]), str(ids["project"]), str(ids["paper"]), str(ids["spec"])),
        )
        c.execute(
            "insert into public.experiment_plans "
            "(id,experiment_id,version,config_json,approved_at) "
            "values (%s,%s,1,%s,%s)",
            (
                str(ids["plan"]),
                str(ids["experiment"]),
                Jsonb(self.plan.model_dump(mode="json")),
                datetime.now(UTC),
            ),
        )
        c.execute(
            "insert into public.workflows (id,thread_id,project_id,paper_id,experiment_id,"
            "spec_version_id,plan_id,status) values (%s,%s,%s,%s,%s,%s,%s,'executing')",
            (
                str(ids["workflow"]),
                str(ids["workflow"]),
                str(ids["project"]),
                str(ids["paper"]),
                str(ids["experiment"]),
                str(ids["spec"]),
                str(ids["plan"]),
            ),
        )

    async def drive(self):
        workflow = (
            self.admin.table("workflows")
            .select("*")
            .eq("id", str(self.ids["workflow"]))
            .execute()
            .data[0]
        )
        state = initial_state(workflow, self.ctx.budget_config)
        state.update(
            card=self.card,
            plan=self.plan,
            experiment_id=str(self.ids["experiment"]),
            plan_id=str(self.ids["plan"]),
            plan_version=1,
            spec_version_id=str(self.ids["spec"]),
            spec_version=1,
            status=WorkflowStatus.EXECUTING.value,
        )
        graph = build_workflow_graph(self.ctx, self.checkpointer)
        config = {"configurable": {"thread_id": state["thread_id"]}}
        await graph.aupdate_state(config, state, as_node="human_approve_plan")
        await graph.ainvoke(None, config)
        snapshot = await self.checkpointer.aget(
            {"configurable": {"thread_id": state["thread_id"]}}
        )
        return snapshot["channel_values"]

    def row(self, table, key):
        return self.db.execute(
            sql.SQL("select * from {} where id = %s").format(sql.Identifier(table)),
            (str(key),),
        ).fetchone()

    def run(self):
        return self.db.execute(
            "select * from public.execution_runs where experiment_id=%s order by created_at",
            (str(self.ids["experiment"]),),
        ).fetchone()

    def inject_cancel(self, _runner_request_id):
        """Persist the same cancellation intent as the API's workflow endpoint."""
        run_id = self.run()["id"]
        self.db.execute(
            "update public.workflows set status='cancelled',error=%s where id=%s",
            ("cancelled: workflow cancelled by user", str(self.ids["workflow"])),
        )
        self.db.execute(
            "update public.execution_runs set status='cancelled',error=%s where id=%s",
            ("cancelled: workflow cancelled by user", str(run_id)),
        )

    def workflow(self):
        return self.row("workflows", self.ids["workflow"])

    def close(self):
        asyncio.run(self.ctx.agent.aclose())
        asyncio.run(self.ctx.runner.aclose())


@pytest.fixture
def harness(db):
    value = Harness(db, [])
    try:
        yield value
    finally:
        value.close()


def outcome(status, stderr="", exit_code=1):
    return RunResult(run_id=uuid4(), status=status, stderr=stderr, exit_code=exit_code)


async def recover(harness):
    from reproagent_api.worker import _recover_existing_workflow

    await _recover_existing_workflow(
        {"settings": harness.settings, "redis": None},
        harness.admin,
        harness.run(),
        harness.workflow(),
    )


@pytest.mark.parametrize(
    ("case", "expected_run", "expected_workflow", "error"),
    [
        ("unrecoverable_exit", RunStatus.FAILED, WorkflowStatus.FAILED, "unclassified crash"),
        ("hard_timeout", RunStatus.TIMEOUT, WorkflowStatus.FAILED, "deadline exceeded"),
        ("user_cancel", RunStatus.CANCELLED, WorkflowStatus.CANCELLED, "cancelled"),
    ],
)
async def test_terminal_matrix_survives_recovery(
    harness, case, expected_run, expected_workflow, error
):
    harness.runner.outcomes = [outcome(expected_run, error)]
    if case == "user_cancel":
        harness.runner.on_accept = harness.inject_cancel
    state = await harness.drive()
    assert state["execution_result"].status is expected_run
    assert harness.run()["status"] == expected_run.value
    assert harness.workflow()["status"] == expected_workflow.value
    attempt = harness.db.execute(
        "select status from public.execution_attempts where run_id=%s",
        (harness.run()["id"],),
    ).fetchone()
    if case == "user_cancel":
        assert attempt is None or attempt["status"] == "cancelled"
    else:
        assert attempt["status"] == expected_run.value
    await recover(harness)
    assert harness.run()["status"] == expected_run.value
    assert harness.workflow()["status"] == expected_workflow.value


async def test_cancel_after_acceptance_before_result_persistence(harness):
    harness.runner.outcomes = [outcome(RunStatus.SUCCEEDED)]

    def cancel_before_persistence(run_id):
        harness.inject_cancel(run_id)
        harness.runner.cancelled.append(run_id)
        harness.runner.results[run_id] = harness.runner.results[run_id].model_copy(
            update={"status": RunStatus.CANCELLED}
        )

    harness.runner.on_poll = cancel_before_persistence

    state = await harness.drive()

    assert harness.runner.cancelled
    assert harness.run()["status"] == RunStatus.CANCELLED.value
    assert harness.workflow()["status"] == WorkflowStatus.CANCELLED.value
    assert state["status"] == WorkflowStatus.CANCELLED.value
    await recover(harness)
    assert harness.run()["status"] == RunStatus.CANCELLED.value
    assert harness.workflow()["status"] == WorkflowStatus.CANCELLED.value


@pytest.mark.parametrize(
    ("persisted_status", "late_runner_status", "expected_workflow"),
    [
        (RunStatus.FAILED, RunStatus.SUCCEEDED, WorkflowStatus.FAILED),
        (RunStatus.TIMEOUT, RunStatus.SUCCEEDED, WorkflowStatus.FAILED),
        (RunStatus.SUCCEEDED, RunStatus.FAILED, WorkflowStatus.COMPLETED),
    ],
)
async def test_persisted_terminal_truth_wins_over_late_runner_result(
    harness, persisted_status, late_runner_status, expected_workflow
):
    """A stale Runner response must never reopen or overwrite any terminal Run."""
    harness.runner.outcomes = [outcome(late_runner_status)]

    def persist_terminal_before_runner_response(_runner_request_id):
        harness.db.execute(
            "update public.execution_runs set status=%s,error=%s where id=%s",
            (persisted_status.value, "persisted terminal truth", harness.run()["id"]),
        )

    harness.runner.on_poll = persist_terminal_before_runner_response
    state = await harness.drive()

    assert state["execution_result"].status is persisted_status
    assert harness.run()["status"] == persisted_status.value
    assert harness.workflow()["status"] == expected_workflow.value
    assert len(harness.runner.requests) == 1
    await recover(harness)
    assert harness.run()["status"] == persisted_status.value
    assert harness.workflow()["status"] == expected_workflow.value


async def test_persisted_timeout_survives_runner_transport_failure(harness):
    harness.runner.outcomes = [outcome(RunStatus.SUCCEEDED)]

    def persist_timeout_then_fail(_runner_request_id):
        harness.db.execute(
            "update public.execution_runs set status='timeout',error='hard deadline' where id=%s",
            (harness.run()["id"],),
        )
        raise RuntimeError("Runner response lost after timeout was persisted")

    harness.runner.on_poll = persist_timeout_then_fail
    state = await harness.drive()

    assert state["execution_result"].status is RunStatus.TIMEOUT
    assert harness.run()["status"] == RunStatus.TIMEOUT.value
    assert harness.workflow()["status"] == WorkflowStatus.FAILED.value
    assert len(harness.runner.requests) == 1


@pytest.mark.parametrize(
    ("terminal", "expected_workflow"),
    [
        ("cancelled", WorkflowStatus.CANCELLED),
        ("failed", WorkflowStatus.FAILED),
        ("timeout", WorkflowStatus.FAILED),
        ("succeeded", WorkflowStatus.COMPLETED),
    ],
)
async def test_terminal_write_after_result_claim_preserves_artifacts_and_audit(
    harness, monkeypatch, terminal, expected_workflow
):
    """Real SQL RPC protects evidence sealed after claim but before replacement."""
    from reproagent_api.services import runs

    harness.runner.outcomes = [
        outcome(RunStatus.SUCCEEDED).model_copy(update={"artifacts": ["late.json"]})
    ]
    original_upload = runs._upload
    protected = {}
    terminal_snapshot = {}

    def seal_after_claim(admin, path, data):
        nonlocal protected, terminal_snapshot
        original_upload(admin, path, data)
        if not path.endswith("/stdout.txt") or protected:
            return
        run = harness.run()
        run_id = run["id"]
        protected_id = uuid4()
        harness.db.execute(
            "insert into public.execution_artifacts "
            "(id,run_id,attempt,artifact_type,storage_path,metadata_json) "
            "values (%s,%s,1,'protected.json',%s,%s)",
            (
                protected_id,
                run_id,
                f"{run_id}/attempt-1/artifacts/protected.json",
                Jsonb({"attempt": 1}),
            ),
        )
        error_category = "TIMEOUT" if terminal == "timeout" else "UNKNOWN_ERROR"
        harness.db.execute(
            "update public.execution_runs set status=%s,error=%s,error_category=%s,"
            "metrics_json=%s,audit_json=%s,stdout_path=%s,stderr_path=%s where id=%s",
            (
                terminal,
                "persisted terminal truth",
                error_category,
                Jsonb({"protected": True}),
                Jsonb({"seed": 42}),
                "protected/stdout.txt",
                "protected/stderr.txt",
                run_id,
            ),
        )
        harness.db.execute(
            "update public.execution_attempts set status=%s,finished_at=now(),"
            "error_message='persisted terminal truth' where run_id=%s and attempt=1",
            (terminal, run_id),
        )
        if terminal == "cancelled":
            harness.db.execute(
                "update public.workflows set status='cancelled',error=%s where id=%s",
                ("cancelled: workflow cancelled by user", harness.ids["workflow"]),
            )
        protected = harness.db.execute(
            "select * from public.execution_artifacts where id=%s", (protected_id,)
        ).fetchone()
        terminal_snapshot = harness.db.execute(
            "select * from public.execution_runs where id=%s", (run_id,)
        ).fetchone()

    monkeypatch.setattr(runs, "_upload", seal_after_claim)
    state = await harness.drive()

    assert protected
    assert state["execution_result"].status.value == terminal
    assert harness.run()["status"] == terminal
    assert harness.workflow()["status"] == expected_workflow.value
    artifacts = harness.db.execute(
        "select * from public.execution_artifacts where run_id=%s", (harness.run()["id"],)
    ).fetchall()
    assert artifacts == [protected]
    persisted = harness.run()
    for field in (
        "error", "error_category", "metrics_json", "audit_json", "stdout_path",
        "stderr_path", "finished_at",
    ):
        assert persisted[field] == terminal_snapshot[field], field
    attempt = harness.db.execute(
        "select * from public.execution_attempts where run_id=%s and attempt=1",
        (harness.run()["id"],),
    ).fetchone()
    assert attempt["status"] == terminal
    assert attempt["error_message"] == "persisted terminal truth"
    await recover(harness)
    assert harness.run()["status"] == terminal
    assert harness.workflow()["status"] == expected_workflow.value


def test_result_persistence_rolls_back_delete_and_audit_on_insert_failure(harness):
    """A malformed artifact cannot partially erase current evidence or update audit."""
    run_id = harness.db.execute(
        "insert into public.execution_runs "
        "(id,experiment_id,plan_id,code_version,plan_version,status,repair_attempt) "
        "values (%s,%s,%s,1,1,'running',0) returning id",
        (uuid4(), str(harness.ids["experiment"]), str(harness.ids["plan"])),
    ).fetchone()["id"]
    harness.db.execute(
        "insert into public.execution_attempts "
        "(run_id,attempt,plan_id,plan_version,code_version,status) "
        "values (%s,1,%s,1,1,'running')",
        (run_id, str(harness.ids["plan"])),
    )
    protected_id = uuid4()
    harness.db.execute(
        "insert into public.execution_artifacts "
        "(id,run_id,attempt,artifact_type,storage_path,metadata_json) "
        "values (%s,%s,1,'protected.json',%s,'{}'::jsonb)",
        (protected_id, run_id, f"{run_id}/attempt-1/artifacts/protected.json"),
    )
    before_artifacts = harness.db.execute(
        "select * from public.execution_artifacts where run_id=%s", (run_id,)
    ).fetchall()
    before_run = harness.row("execution_runs", run_id)
    harness.db.execute("savepoint result_insert_failure")
    try:
        with pytest.raises(psycopg.errors.NotNullViolation):
            harness.admin.rpc(
                "persist_execution_result",
                {
                    "p_run_id": run_id,
                    "p_repair_attempt": 0,
                    "p_artifact_rows": [{"artifact_type": "late.json", "metadata_json": {}}],
                    "p_exit_code": 0,
                    "p_metrics_json": {"late": True},
                    "p_execution_time": 2.5,
                    "p_audit_json": {"late": True},
                    "p_stdout_path": "late/stdout.txt",
                    "p_stderr_path": "late/stderr.txt",
                    "p_error_category": None,
                    "p_error": None,
                },
            ).execute()
    finally:
        harness.db.execute("rollback to savepoint result_insert_failure")
        harness.db.execute("release savepoint result_insert_failure")
    assert harness.db.execute(
        "select * from public.execution_artifacts where run_id=%s", (run_id,)
    ).fetchall() == before_artifacts
    assert harness.row("execution_runs", run_id) == before_run


async def test_fourth_ordinary_repair_is_refused_and_versions_form_parent_chain(harness):
    harness.runner.outcomes = [outcome(RunStatus.FAILED, "SyntaxError: broken")] * 4
    state = await harness.drive()
    run, workflow = harness.run(), harness.workflow()
    assert state["repair_attempt"] == 3
    assert run["status"] == RunStatus.FAILED.value
    assert workflow["status"] == WorkflowStatus.FAILED.value
    attempts = harness.db.execute(
        "select attempt,code_version,plan_id,status from public.execution_attempts "
        "where run_id=%s order by attempt",
        (run["id"],),
    ).fetchall()
    assert [a["attempt"] for a in attempts] == [1, 2, 3, 4]
    assert [a["status"] for a in attempts] == ["failed"] * 4
    assert len(harness.runner.requests) == 4
    versions = harness.db.execute(
        "select id,version,path,sha256 from public.generated_files where experiment_id=%s "
        "order by version",
        (str(harness.ids["experiment"]),),
    ).fetchall()
    assert {v["version"] for v in versions} == {1, 2, 3, 4}
    # Canonical repair rendering may intentionally keep the same bytes; immutable
    # attempt-specific file identities and pins prove historical versions survive.
    # Five generated source files are pinned at each immutable code version.
    assert len(versions) == 20
    assert len({v["id"] for v in versions}) == 20
    assert {
        version: len({v["id"] for v in versions if v["version"] == version})
        for version in range(1, 5)
    } == {version: 5 for version in range(1, 5)}
    pins = harness.db.execute(
        "select p.attempt,p.generated_file_id,g.version from public.execution_attempt_files p "
        "join public.generated_files g on g.id=p.generated_file_id "
        "where p.run_id=%s order by p.attempt",
        (run["id"],),
    ).fetchall()
    assert sorted({p["attempt"] for p in pins}) == [1, 2, 3, 4]
    assert {
        attempt: {p["version"] for p in pins if p["attempt"] == attempt}
        for attempt in range(1, 5)
    } == {attempt: {attempt} for attempt in range(1, 5)}
    assert all(sum(p["attempt"] == attempt for p in pins) == 5 for attempt in range(1, 5))
    assert all(p["generated_file_id"] for p in pins)
    assert {a["code_version"] for a in attempts} == {1, 2, 3, 4}
    assert {str(a["plan_id"]) for a in attempts} == {str(harness.ids["plan"])}
    projection = harness.db.execute(
        "select generated_file_id from public.execution_run_files where run_id=%s",
        (run["id"],),
    ).fetchall()
    assert {p["generated_file_id"] for p in projection} == {
        p["generated_file_id"] for p in pins if p["attempt"] == 4
    }


async def test_third_oom_replan_is_refused_and_report_uses_final_attempt(harness):
    assert harness.db.execute(
        "select current_user, rolsuper, rolbypassrls from pg_roles where rolname=current_user"
    ).fetchone() == {
        "current_user": "service_role", "rolsuper": False, "rolbypassrls": True
    }
    harness.runner.outcomes = [outcome(RunStatus.FAILED, "CUDA out of memory", 137)] * 3
    state = await harness.drive()
    run, workflow = harness.run(), harness.workflow()
    assert run["status"] == RunStatus.FAILED.value
    assert workflow["status"] == WorkflowStatus.FAILED.value
    assert state["budget"].replan_count == 2
    plans = harness.db.execute(
        "select * from public.experiment_plans where experiment_id=%s order by version",
        (str(harness.ids["experiment"]),),
    ).fetchall()
    assert len(plans) == 3
    oom_plans = harness.db.execute(
        "select count(*) as count from public.experiment_plans "
        "where experiment_id=%s and origin='oom_retry' and related_run_id=%s",
        (str(harness.ids["experiment"]), run["id"]),
    ).fetchone()["count"]
    assert oom_plans == 2
    assert plans[0]["parent_version_id"] is None
    assert plans[1]["parent_version_id"] == plans[0]["id"]
    assert plans[2]["parent_version_id"] == plans[1]["id"]
    assert [p["config_json"]["training_config"]["batch_size"]["value"] for p in plans] == [
        64,
        32,
        16,
    ]
    critical = [a for a in plans[0]["config_json"]["assumptions"] if a["risk_tier"] == "critical"]
    assert all(a in p["config_json"]["assumptions"] for p in plans for a in critical)
    for before, after in zip(plans[:-1], plans[1:], strict=True):
        old_config = {k: v for k, v in before["config_json"].items() if k != "assumptions"}
        new_config = {k: v for k, v in after["config_json"].items() if k != "assumptions"}
        old_config["training_config"] = {
            k: v for k, v in old_config["training_config"].items() if k != "batch_size"
        }
        new_config["training_config"] = {
            k: v for k, v in new_config["training_config"].items() if k != "batch_size"
        }
        assert old_config == new_config
        assert [
            a for a in before["config_json"]["assumptions"]
            if a["field_path"] != "training_config.batch_size"
        ] == [
            a for a in after["config_json"]["assumptions"]
            if a["field_path"] != "training_config.batch_size"
        ]
    attempts = harness.db.execute(
        "select attempt,plan_id,plan_version from public.execution_attempts "
        "where run_id=%s order by attempt",
        (run["id"],),
    ).fetchall()
    assert [a["attempt"] for a in attempts] == [1, 2, 3]
    assert [a["plan_version"] for a in attempts] == [1, 2, 3]
    reports = harness.db.execute(
        "select * from public.reproduction_reports where run_id=%s",
        (run["id"],),
    ).fetchall()
    assert len(reports) == 1
    assert reports[0]["attempt"] == 3
    refs = reports[0]["report_json"]["references"]
    assert refs["execution_attempt"] == 3
    assert refs["plan_id"] == str(plans[2]["id"])
    assert refs["plan_version"] == 3


async def test_critical_assumption_change_stops_oom_retry(harness):
    plan = harness.plan.model_dump(mode="json")
    plan["assumptions"].append(
        {
            "field_path": "training_config.batch_size",
            "kind": "mvp_scaling",
            "value": 64,
            "rationale": "human critical decision",
            "requires_confirmation": True,
            "human_confirmed": True,
            "risk_tier": "critical",
        }
    )
    harness.db.execute(
        "insert into public.experiment_plans "
        "(id,experiment_id,version,parent_version_id,config_json,approved_at) "
        "values (%s,%s,2,%s,%s,%s)",
        (
            uuid4(),
            str(harness.ids["experiment"]),
            str(harness.ids["plan"]),
            Jsonb(plan),
            datetime.now(UTC),
        ),
    )
    critical_plan = harness.db.execute(
        "select * from public.experiment_plans where experiment_id=%s and version=2",
        (str(harness.ids["experiment"]),),
    ).fetchone()
    harness.plan = type(harness.plan).model_validate(plan)
    run_id = harness.db.execute(
        "insert into public.execution_runs "
        "(id,experiment_id,plan_id,code_version,plan_version,status) "
        "values (%s,%s,%s,1,2,'running') returning id",
        (uuid4(), str(harness.ids["experiment"]), critical_plan["id"]),
    ).fetchone()["id"]
    latest = (
        harness.admin.table("experiment_plans").select("*")
        .eq("experiment_id", str(harness.ids["experiment"]))
        .order("version", desc=True).limit(1).execute().data
    )
    assert [str(row["id"]) for row in latest] == [str(critical_plan["id"])]
    with pytest.raises(ValueError, match="Critical batch size changes"):
        create_oom_plan(
            harness.admin,
            str(harness.ids["experiment"]),
            {**critical_plan, "id": str(critical_plan["id"])},
            run_id,
            "OOM",
        )
    assert (
        harness.db.execute(
            "select count(*) as count from public.experiment_plans where experiment_id=%s",
            (str(harness.ids["experiment"]),),
        ).fetchone()["count"]
        == 2
    )


def test_service_role_grants_are_scoped_to_production_writes(harness):
    expected = {
        "projects": {"select", "insert"},
        "papers": {"select", "insert", "update", "delete"},
        "reproduction_specs": {"select", "insert"},
        "experiments": {"select", "insert", "update"},
        "experiment_plans": {"select", "insert", "update"},
        "generated_files": {"select", "insert"},
        "execution_runs": {"select", "insert", "update"},
        "execution_attempts": {"select", "insert", "update"},
        "execution_attempt_files": {"select", "insert"},
        "execution_run_files": {"select", "insert", "delete"},
        "reproduction_reports": {"select", "insert"},
        "execution_artifacts": {"select", "insert", "delete"},
        "workflows": {"select", "insert", "update"},
        "agent_traces": {"select", "insert"},
        "job_outbox": {"select", "insert", "update"},
        "execution_run_idempotency": {"select", "insert"},
    }
    privileges = ("select", "insert", "update", "delete", "truncate", "references", "trigger")
    for table, allowed in expected.items():
        relation = table if "." in table else f"public.{table}"
        actual = harness.db.execute(
            sql.SQL("select {} from pg_roles where rolname='service_role'").format(
                sql.SQL(", ").join(
                    sql.SQL(
                        "has_table_privilege('service_role'::name, {}::text, {}::text) as {}"
                    ).format(
                        sql.Literal(relation),
                        sql.Literal(privilege),
                        sql.Identifier(privilege),
                    )
                    for privilege in privileges
                )
            )
        ).fetchone()
        assert {key for key, value in actual.items() if value} == allowed, table
    execute_acl = harness.db.execute(
        "select "
        "has_function_privilege('anon', 'public.persist_execution_result("
        "uuid,integer,jsonb,integer,jsonb,numeric,jsonb,text,text,text,text)', 'execute') "
        "as anon, "
        "has_function_privilege('authenticated', 'public.persist_execution_result("
        "uuid,integer,jsonb,integer,jsonb,numeric,jsonb,text,text,text,text)', 'execute') "
        "as authenticated, "
        "has_function_privilege('service_role', 'public.persist_execution_result("
        "uuid,integer,jsonb,integer,jsonb,numeric,jsonb,text,text,text,text)', 'execute') "
        "as service_role"
    ).fetchone()
    assert execute_acl == {"anon": False, "authenticated": False, "service_role": True}


def test_two_account_denied_reads_and_writes_under_rls(harness):
    c, ids = harness.db, harness.ids
    run_id = c.execute(
        "insert into public.execution_runs "
        "(id,experiment_id,plan_id,code_version,plan_version,status) "
        "values (%s,%s,%s,1,1,'succeeded') returning id",
        (uuid4(), str(ids["experiment"]), str(ids["plan"])),
    ).fetchone()["id"]
    report_id = c.execute(
        "select * from public.append_report_version(%s,'{\"secret\":true}'::jsonb,'secret report')",
        (run_id,),
    ).fetchone()["id"]
    artifact_id = uuid4()
    c.execute(
        "insert into public.execution_artifacts (id,run_id,artifact_type,storage_path) "
        "values (%s,%s,'metrics.json',%s)",
        (artifact_id, run_id, f"{run_id}/metrics.json"),
    )
    c.execute(
        "insert into storage.buckets (id,name,public) values ('runs','runs',false) "
        "on conflict (id) do nothing"
    )
    c.execute(
        "insert into storage.objects (id,bucket_id,name) values (%s,'runs',%s)",
        (uuid4(), f"{run_id}/attempt-1/artifacts/metrics.json"),
    )
    c.execute("savepoint ownership_matrix")
    try:
        c.execute("set local role authenticated")
        c.execute("select set_config('request.jwt.claim.sub', %s, true)", (str(ids["other"]),))
        c.execute("select set_config('request.jwt.claim.role', 'authenticated', true)")
        for table, row_id in (
            ("projects", ids["project"]),
            ("papers", ids["paper"]),
            ("reproduction_specs", ids["spec"]),
            ("experiments", ids["experiment"]),
            ("experiment_plans", ids["plan"]),
            ("workflows", ids["workflow"]),
            ("execution_runs", run_id),
            ("reproduction_reports", report_id),
            ("execution_artifacts", artifact_id),
        ):
            assert (
                c.execute(
                    sql.SQL("select * from public.{} where id=%s").format(sql.Identifier(table)),
                    (row_id,),
                ).fetchall()
                == []
            ), table
        assert (
            c.execute(
                "select * from public.execution_attempts where run_id=%s", (run_id,)
            ).fetchall()
            == []
        )
        assert c.execute("select * from storage.objects where bucket_id='runs'").fetchall() == []
        assert (
            c.execute(
                "update public.papers set title='stolen' where id=%s", (ids["paper"],)
            ).rowcount
            == 0
        )
        assert c.execute("delete from public.projects where id=%s", (ids["project"],)).rowcount == 0
        for statement, row_id in (
            ("update public.projects set name='stolen' where id=%s", ids["project"]),
            ("update public.experiments set name='stolen' where id=%s", ids["experiment"]),
            ("update public.workflows set status='failed' where id=%s", ids["workflow"]),
            ("update public.execution_runs set status='failed' where id=%s", run_id),
            (
                "update public.execution_artifacts set artifact_type='stolen' where id=%s",
                artifact_id,
            ),
        ):
            assert c.execute(statement, (row_id,)).rowcount == 0, statement
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
            c.execute(
                "insert into public.papers (project_id,title,pdf_path) values (%s,'foreign','x')",
                (str(ids["project"]),),
            )
    finally:
        c.execute("rollback to savepoint ownership_matrix")
        c.execute("release savepoint ownership_matrix")


def test_service_role_owner_positive_workflow_writes(harness, monkeypatch):
    """Normal owner writes run as service_role, with RLS bypass made explicit."""
    from reproagent_api.auth import get_current_user_id
    from reproagent_api.routers import experiments, runs

    c, ids = harness.db, harness.ids
    for module in (experiments, runs):
        monkeypatch.setattr(module, "make_service_client", lambda settings: harness.admin)
    app = create_app(harness.settings)
    app.dependency_overrides[get_current_user_id] = lambda: ids["owner"]
    client = TestClient(app)

    edited = client.post(
        f"/v1/experiments/{ids['experiment']}/plans",
        json={
            "base_version": 1,
            "changes": [{"field_path": "training_config.batch_size", "value": 32}],
            "change_reason": "service-role positive control",
        },
    )
    assert edited.status_code == 201, edited.text
    assert edited.json()["version"] == 2
    approved = client.post(f"/v1/experiments/{ids['experiment']}/plans/2/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_at"] is not None

    headers = {"Idempotency-Key": "service-role-positive-run"}
    created = client.post(f"/v1/experiments/{ids['experiment']}/runs", headers=headers)
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    replay = client.post(f"/v1/experiments/{ids['experiment']}/runs", headers=headers)
    assert replay.status_code == 202, replay.text
    assert replay.json()["run_id"] == run_id
    assert c.execute(
        "select count(*) as count from public.job_outbox where entity_id=%s", (run_id,)
    ).fetchone()["count"] == 1
    assert c.execute(
        "select count(*) as count from public.execution_run_idempotency where run_id=%s", (run_id,)
    ).fetchone()["count"] == 1
    appended = harness.admin.rpc(
        "append_report_version",
        {"p_run_id": run_id, "p_report_json": {"owner": True}, "p_markdown": "owner report"},
    ).execute().data
    assert appended[0]["version"] == 1
    cancelled = client.post(f"/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert c.execute("select current_user").fetchone()["current_user"] == "service_role"


async def test_service_role_owner_positive_execution_projection_and_report(harness):
    """Generated code, attempts, projection, artifacts, traces and workflow are writable."""
    harness.runner.outcomes = [
        outcome(RunStatus.SUCCEEDED).model_copy(update={"artifacts": ["metrics.json"]})
    ]
    state = await harness.drive()
    run = harness.run()
    assert state["status"] == WorkflowStatus.COMPLETED.value
    assert run["status"] == "succeeded"
    assert harness.workflow()["status"] == "completed"
    assert harness.db.execute("select current_user").fetchone()["current_user"] == "service_role"
    for table, expected in (
        ("generated_files", 5),
        ("execution_attempts", 1),
        ("execution_attempt_files", 5),
        ("execution_run_files", 5),
        ("execution_artifacts", 1),
        ("reproduction_reports", 1),
    ):
        key = "experiment_id" if table == "generated_files" else "run_id"
        value = harness.ids["experiment"] if table == "generated_files" else run["id"]
        count = harness.db.execute(
            sql.SQL("select count(*) as count from public.{} where {}=%s").format(
                sql.Identifier(table), sql.Identifier(key)
            ),
            (value,),
        ).fetchone()["count"]
        assert count == expected, table
    assert harness.db.execute(
        "select count(*) as count from public.agent_traces where workflow_id=%s",
        (harness.ids["workflow"],),
    ).fetchone()["count"] > 0


def test_service_role_api_denies_foreign_resource_matrix_and_guessed_evidence(
    harness, monkeypatch
):
    """Exercise production API ownership guards with service-role SQL visibility.

    RLS is intentionally bypassed for this path; every router must still scope by
    the authenticated actor. The real API app and real SQL execute every denial.
    """
    from reproagent_api.auth import get_current_user_id
    from reproagent_api.routers import experiments, papers, runs

    c, ids = harness.db, harness.ids
    run_id = c.execute(
        "insert into public.execution_runs "
        "(id,experiment_id,plan_id,code_version,plan_version,status,stdout_path,stderr_path) "
        "values (%s,%s,%s,1,1,'succeeded',%s,%s) returning id",
        (
            uuid4(), str(ids["experiment"]), str(ids["plan"]),
            "foreign/stdout.txt", "foreign/stderr.txt",
        ),
    ).fetchone()["id"]
    report_id = c.execute(
        "select * from public.append_report_version(%s,'{\"secret\":true}'::jsonb,'secret report')",
        (run_id,),
    ).fetchone()["id"]
    artifact_id = uuid4()
    storage_path = f"{run_id}/attempt-1/artifacts/metrics.json"
    c.execute(
        "insert into public.execution_attempts "
        "(run_id,attempt,plan_id,plan_version,code_version,status) "
        "values (%s,1,%s,1,1,'succeeded')",
        (run_id, str(ids["plan"])),
    )
    c.execute(
        "insert into public.execution_artifacts (id,run_id,attempt,artifact_type,storage_path) "
        "values (%s,%s,1,'metrics.json',%s)",
        (artifact_id, run_id, storage_path),
    )
    c.execute(
        "insert into public.agent_traces (workflow_id,node_name,input_json,output_json) "
        "values (%s,'execute_code','{}','{}')",
        (ids["workflow"],),
    )
    c.execute(
        "insert into storage.buckets (id,name,public) values ('runs','runs',false) "
        "on conflict (id) do nothing"
    )
    c.execute(
        "insert into storage.objects (id,bucket_id,name) values (%s,'runs',%s)",
        (uuid4(), storage_path),
    )
    harness.admin.files[storage_path] = b"foreign metrics"

    for module in (experiments, papers, runs):
        monkeypatch.setattr(module, "make_service_client", lambda settings: harness.admin)
    app = create_app(harness.settings)
    app.dependency_overrides[get_current_user_id] = lambda: ids["other"]
    client = TestClient(app)

    foreign_get = [
        f"/v1/papers/{ids['paper']}",
        f"/v1/papers/{ids['paper']}/card",
        f"/v1/papers/{ids['paper']}/experiments",
        f"/v1/papers/{ids['paper']}/pdf",
        f"/v1/experiments/{ids['experiment']}",
        f"/v1/experiments/{ids['experiment']}/plans",
        f"/v1/experiments/{ids['experiment']}/plans/1",
        f"/v1/experiments/{ids['experiment']}/runs",
        f"/v1/runs/{run_id}",
        f"/v1/runs/{run_id}/logs",
        f"/v1/runs/{run_id}/artifacts",
        f"/v1/runs/{run_id}/files",
        f"/v1/runs/{run_id}/report",
        f"/v1/workflows/{ids['workflow']}",
        f"/v1/workflows/{ids['workflow']}/traces",
        f"/v1/papers/{ids['paper']}/workflow",
    ]
    foreign_post = [
        (f"/v1/papers/{ids['paper']}/experiments", {"name": "stolen"}, {}),
        (f"/v1/papers/{ids['paper']}/card/edits", {
            "base_version": 1,
            "edits": [{"field_path": "method", "value": "stolen"}],
        }, {}),
        (f"/v1/experiments/{ids['experiment']}/plans", {
            "base_version": 1, "review_mode": "assisted", "change_reason": "stolen",
        }, {}),
        (f"/v1/experiments/{ids['experiment']}/plans/1/approve", None, {}),
        (f"/v1/experiments/{ids['experiment']}/runs", None,
         {"headers": {"Idempotency-Key": "foreign-run-key"}}),
        (f"/v1/runs/{run_id}/report", None, {}),
        (f"/v1/runs/{run_id}/cancel", None, {}),
        (f"/v1/workflows/{ids['workflow']}/cancel", None, {}),
        (f"/v1/workflows/{ids['workflow']}/resume", {"stage": "spec"}, {}),
        (f"/v1/workflows/{ids['workflow']}/retry", None, {}),
    ]
    guessed = uuid4()
    foreign_get.extend([
        f"/v1/runs/{guessed}/logs",
        f"/v1/runs/{guessed}/artifacts",
        f"/v1/runs/{guessed}/report",
        f"/v1/workflows/{guessed}/traces",
    ])

    c.execute("savepoint service_role_api")
    try:
        c.execute("set local role service_role")
        for path in foreign_get:
            response = client.get(path)
            assert response.status_code in {403, 404}, (path, response.status_code, response.text)
        for path, body, kwargs in foreign_post:
            response = client.post(path, json=body, **kwargs)
            assert response.status_code in {403, 404}, (path, response.status_code, response.text)
        assert harness.runner.cancelled == []
        assert c.execute(
            "select status from public.execution_runs where id=%s", (run_id,)
        ).fetchone()["status"] == "succeeded"
        assert c.execute(
            "select count(*) as count from public.reproduction_reports where id=%s", (report_id,)
        ).fetchone()["count"] == 1
        assert c.execute(
            "select count(*) as count from public.execution_artifacts where id=%s", (artifact_id,)
        ).fetchone()["count"] == 1
    finally:
        c.execute("rollback to savepoint service_role_api")
        c.execute("release savepoint service_role_api")


def test_service_role_rpc_rejects_foreign_actor_and_guessed_evidence_is_absent(harness):
    ids = harness.ids
    harness.db.execute("savepoint service_role_rpc")
    try:
        harness.db.execute("set local role service_role")
        owner_run_id = harness.db.execute(
            "select public.create_execution_run(%s,%s,1,%s,%s,%s)",
            (
                str(ids["experiment"]),
                str(ids["plan"]),
                str(ids["owner"]),
                "service-role-owner",
                "owner-fingerprint",
            ),
        ).fetchone()["create_execution_run"]
        assert harness.db.execute(
            "select status from public.execution_runs where id=%s", (owner_run_id,)
        ).fetchone()["status"] == "queued"
        assert harness.db.execute(
            "select count(*) as count from public.execution_run_idempotency where run_id=%s",
            (owner_run_id,),
        ).fetchone()["count"] == 1
        harness.db.execute("savepoint forbidden_actor")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="FORBIDDEN"):
            harness.db.execute(
                "select public.create_execution_run(%s,%s,1,%s,%s,%s)",
                (
                    str(ids["experiment"]),
                    str(ids["plan"]),
                    str(ids["other"]),
                    f"foreign-{uuid4().hex}",
                    "fingerprint",
                ),
            )
        harness.db.execute("rollback to savepoint forbidden_actor")
        guessed = uuid4()
        assert (
            harness.db.execute(
                "select count(*) as count from public.execution_artifacts where run_id=%s",
                (guessed,),
            ).fetchone()["count"]
            == 0
        )
        assert (
            harness.db.execute(
                "select count(*) as count from public.reproduction_reports where run_id=%s",
                (guessed,),
            ).fetchone()["count"]
            == 0
        )
        assert (
            harness.db.execute(
                "select count(*) as count from public.agent_traces where workflow_id=%s",
                (guessed,),
            ).fetchone()["count"]
            == 0
        )
        assert harness.admin.files == {}
    finally:
        harness.db.execute("rollback to savepoint service_role_rpc")
        harness.db.execute("release savepoint service_role_rpc")
