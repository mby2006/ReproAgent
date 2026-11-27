"""Unit tests for run orchestration (runs router) and the Worker repair loop.

The Agent and Runner are mocked at the HTTP boundary with real codegen logic on
the Agent side, so the contract + orchestration are exercised without Docker or
an LLM. Ownership flows runs -> experiments -> projects.user_id.
"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError as PostgrestAPIError
from reproagent_agent.codegen import (
    classify_error,
    generate_code,
    generate_from_plan,
    repair_bundle,
    validate_bundle,
)
from reproagent_agent.evaluation import evaluate_run
from reproagent_agent.planning import build_plan
from reproagent_api import worker
from reproagent_api.auth import get_anon_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.routers import runs
from reproagent_api.services.runs import async_persist_result, execution_request_id
from reproagent_api.workflow import make_memory_checkpointer
from reproagent_schemas import (
    Assumption,
    AssumptionKind,
    ClassifyRequest,
    CodegenRequest,
    ConfirmationSource,
    EvaluateRequest,
    EvidenceField,
    EvidenceStatus,
    ExperimentPlan,
    FieldRecommendation,
    RepairRequest,
    ReportedResult,
    ReproductionCard,
    ReviewRequest,
    ReviewMode,
    RiskTier,
    RunAudit,
    RunResult,
    RunStatus,
    StaticValidateRequest,
)

USER_ID = uuid4()
BOB_ID = uuid4()
PROJECT_ID = uuid4()
PAPER_ID = uuid4()
SPEC_ID = uuid4()
EXPERIMENT_ID = uuid4()
PLAN_ID = uuid4()

TOKENS = {"good-token": USER_ID, "bob-token": BOB_ID}
AUTH = {"Authorization": "Bearer good-token"}
BOB_AUTH = {"Authorization": "Bearer bob-token"}
IDEM_KEY = {"Idempotency-Key": "test-key-00001"}


def _rpc_error(code: str, sqlstate: str = "P0001") -> PostgrestAPIError:
    """Build the PostgREST error the 6-arg RPC really raises.

    ``RAISE EXCEPTION '<code>' USING errcode = '<sqlstate>'`` surfaces through
    supabase-py as ``APIError`` whose ``message`` is the bare code and whose
    ``code`` is only the (shared) SQLSTATE. The fakes raise this exact shape so
    the router's structured error mapping is exercised, not a loose RuntimeError.
    """
    return PostgrestAPIError(
        {"code": sqlstate, "message": code, "hint": None, "details": None}
    )


class FakeAuth:
    def get_user(self, token: str):
        if token not in TOKENS:
            raise RuntimeError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(id=str(TOKENS[token])))


def make_card() -> ReproductionCard:
    def explicit(value, page=8):
        return EvidenceField(
            value=value,
            status=EvidenceStatus.PAPER_EXPLICIT,
            evidence_quote=f"...{value}...",
            source_page=page,
        )

    return ReproductionCard(
        method=explicit("LeNet-5 gradient-based learning"),
        model_architecture=explicit("LeNet-5 convolutional network"),
        dataset=explicit("MNIST handwritten digits"),
        optimizer=explicit("SGD"),
        loss_function=explicit("cross entropy"),
        hyperparameters={
            "learning_rate": explicit("0.01"),
            "batch_size": explicit(64, page=9),
            "epochs": explicit(2, page=9),
        },
        evaluation_metrics=[explicit("accuracy")],
        reported_results=[
            ReportedResult(
                metric="accuracy",
                value=0.99,
                status=EvidenceStatus.PAPER_EXPLICIT,
                evidence_quote="...test accuracy 99%...",
                source_page=10,
            )
        ],
    )


PLAN_DEFAULTS = {
    "approved_at": None,
    "parent_version_id": None,
    "change_reason": None,
    "related_error": None,
    "related_run_id": None,
    "changes_json": [],
    "origin": "generated",
    "budget_json": {},
}

EXPERIMENT_STATUSES = {
    "pending",
    "planning",
    "approved",
    "running",
    "succeeded",
    "failed",
    "cancelled",
}


def _check_experiment_status(name, status):
    if name == "experiments" and status is not None and status not in EXPERIMENT_STATUSES:
        raise AssertionError(f"experiments_status_check violated: {status!r}")


def _path(row, key):
    cur = row
    for part in key.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


class FakeResult:
    def __init__(self, data):
        self.data = data


class _Not:
    def __init__(self, query):
        self._query = query

    def is_(self, key, value):
        self._query._filters.append(("not_is", key, value))
        return self._query


class FakeQuery:
    def __init__(self, admin, name):
        self._admin = admin
        self._name = name
        self._rows = admin.tables.setdefault(name, [])
        self._filters = []
        self._order = None
        self._limit = None
        self._insert = None
        self._update = None
        self._upsert = None
        self._on_conflict = None
        self._ignore_duplicates = False
        self._delete = False
        self._select = None
        self._single = False
        self.not_ = _Not(self)

    def select(self, *args):
        self._select = ",".join(args)
        return self

    def eq(self, key, value):
        self._filters.append(("eq", key, value))
        return self

    def in_(self, key, values):
        self._filters.append(("in", key, list(values)))
        return self

    def neq(self, key, value):
        self._filters.append(("neq", key, value))
        return self

    def lte(self, key, value):
        self._filters.append(("lte", key, value))
        return self

    def order(self, key, desc=False):
        self._order = (key, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, row):
        self._insert = row
        return self

    def update(self, patch):
        self._update = patch
        return self

    def upsert(self, row, *, on_conflict=None, ignore_duplicates=False):
        self._upsert = row
        self._on_conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        return self

    def delete(self):
        self._delete = True
        return self

    def single(self):
        self._single = True
        return self

    def _match(self, row):
        for kind, key, value in self._filters:
            actual = _path(row, key)
            if kind == "eq" and actual != value:
                return False
            if kind == "in" and actual not in value:
                return False
            if kind == "neq" and actual == value:
                return False
            if kind == "lte" and (
                actual is None or datetime.fromisoformat(actual) > datetime.fromisoformat(value)
            ):
                return False
            if kind == "not_is" and value == "null" and actual is None:
                return False
        return True

    def _new_row(self, item):
        defaults = PLAN_DEFAULTS if self._name == "experiment_plans" else {}
        if self._name == "execution_runs":
            defaults = {
                "code_version": 0,
                "plan_version": 1,
                "status": "queued",
                "repair_attempt": 0,
                "metrics_json": {},
                "audit_json": {},
                "runtime_json": {},
            }
        elif self._name == "job_outbox":
            defaults = {
                "status": "pending",
                "attempts": 0,
                "last_error": None,
            }
        elif self._name == "experiments":
            defaults = {"spec_id": None, "status": "pending"}
        row = {
            "id": str(uuid4()),
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            **defaults,
        }
        row.update(item)
        _check_experiment_status(self._name, row.get("status"))
        if self._name == "experiments" and "project_id" in row:
            row.setdefault("projects", {"user_id": self._admin.owners.get(row["project_id"])})
        return row

    def execute(self):
        if self._upsert is not None:
            keys = (self._on_conflict or "id").split(",")
            existing = next((
                row for row in self._rows
                if all(row.get(key) == self._upsert.get(key) for key in keys)
            ), None)
            if existing is not None and self._ignore_duplicates:
                return FakeResult([])
            if existing is None:
                existing = self._new_row(self._upsert)
                self._rows.append(existing)
            else:
                existing.update(self._upsert)
            return FakeResult([existing])
        if self._insert is not None:
            items = self._insert if isinstance(self._insert, list) else [self._insert]
            inserted = []
            for item in items:
                row = self._new_row(item)
                self._rows.append(row)
                inserted.append(row)
            return FakeResult(inserted)

        matched = [r for r in self._rows if self._match(r)]
        if self._delete:
            for r in matched:
                self._rows.remove(r)
            return FakeResult(matched)
        if self._update is not None:
            _check_experiment_status(self._name, self._update.get("status"))
            for r in matched:
                r.update(self._update)
            return FakeResult(matched)
        rows = list(matched)
        if self._order:
            key, desc = self._order
            rows = sorted(rows, key=lambda r: (r.get(key) is None, r.get(key)), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        if self._select and "*" not in self._select:
            cols = [c.strip() for c in self._select.split(",") if "(" not in c and "!" not in c]
            rows = [{k: r.get(k) for k in cols} for r in rows]
        if self._single:
            return FakeResult(rows[0] if rows else None)
        return FakeResult(rows)


class FakeBucket:
    def __init__(self, store):
        self._store = store

    def upload(self, path, data, options=None):
        self._store[path] = data

    def download(self, path):
        return self._store[path]


class FakeStorage:
    def __init__(self):
        self.files = {}

    def create_bucket(self, name, options=None):
        return None

    def from_(self, name):
        return FakeBucket(self.files)


class FakeAdmin:
    def __init__(self, tables, owners):
        self.tables = tables
        self.owners = owners
        self.storage = FakeStorage()
        self.rpc_calls = []

    def table(self, name):
        return FakeQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        if name == "reconcile_execution_run":
            run = next(
                (row for row in self.tables["execution_runs"]
                 if row["id"] == params["p_run_id"]),
                None,
            )
            guarded = "p_repair_attempt" in params
            if not guarded:
                if run is None or run["status"] not in ("queued", "dispatching", "running"):
                    return False
            elif (
                run is None
                or run.get("repair_attempt", 0) != params["p_repair_attempt"]
                or not (
                    run["status"] in ("queued", "dispatching", "running")
                    or (run["status"] == "cancelled" and params["p_status"] == "cancelled")
                )
            ):
                return False
            now = datetime.now(UTC).isoformat()
            run_order = (run.get("created_at") or "", run["id"])
            has_newer_run = any(
                row["experiment_id"] == run["experiment_id"]
                and (row.get("created_at") or "", row["id"]) > run_order
                for row in self.tables["execution_runs"]
            )
            if params.get("p_require_latest") and has_newer_run:
                return False
            if run["status"] != "cancelled":
                run.update({
                    "status": params["p_status"],
                    "error": params["p_error"],
                    "finished_at": now,
                    "updated_at": now,
                })
            if not has_newer_run:
                experiment = next(
                    row for row in self.tables["experiments"]
                    if row["id"] == run["experiment_id"]
                )
                experiment["status"] = (
                    "failed" if params["p_status"] == "timeout" else params["p_status"]
                )
            return True
        if name != "create_execution_run":
            raise AssertionError(f"unexpected rpc: {name}")
        experiment = next(
            row for row in self.tables["experiments"] if row["id"] == params["p_experiment_id"]
        )
        plan = next(
            row for row in self.tables["experiment_plans"] if row["id"] == params["p_plan_id"]
        )
        # Validate plan belongs to experiment and version matches
        if plan["experiment_id"] != experiment["id"]:
            raise ValueError("plan does not belong to experiment")
        if plan["version"] != params["p_plan_version"]:
            raise ValueError("plan version mismatch")

        # --- New 6-arg authorization checks ---
        actor_id = params.get("p_actor_id")
        idempotency_key = params.get("p_idempotency_key")
        request_fingerprint = params.get("p_request_fingerprint")

        # Latest version check
        plans_for_exp = [
            r for r in self.tables["experiment_plans"]
            if r["experiment_id"] == experiment["id"]
        ]
        max_version = max(r["version"] for r in plans_for_exp) if plans_for_exp else 0
        if params["p_plan_version"] != max_version:
            raise _rpc_error("STALE_PLAN_APPROVAL")

        # Approval check
        if not plan.get("approved_at"):
            raise _rpc_error("PLAN_NOT_APPROVED")

        # Active run check (before idempotency for non-matching key)
        active = [
            r for r in self.tables["execution_runs"]
            if r["experiment_id"] == experiment["id"]
            and r["status"] in ("queued", "dispatching", "running")
        ]

        # Idempotency check
        idem_table = self.tables.setdefault("execution_run_idempotency", [])
        existing_idem = next((
            r for r in idem_table
            if r["experiment_id"] == experiment["id"]
            and r["actor_id"] == actor_id
            and r["idempotency_key"] == idempotency_key
        ), None) if idempotency_key else None

        if existing_idem is not None:
            if existing_idem["request_fingerprint"] != request_fingerprint:
                raise _rpc_error("IDEMPOTENCY_CONFLICT")
            return existing_idem["run_id"]

        if active:
            raise _rpc_error("ACTIVE_RUN_EXISTS")

        run_id = str(uuid4())
        self.tables["execution_runs"].append({
            "id": run_id,
            "experiment_id": experiment["id"],
            "plan_id": plan["id"],
            "code_version": 0,
            "plan_version": plan["version"],
            "status": "queued",
            "repair_attempt": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        self.tables.setdefault("job_outbox", []).append({
            "id": str(uuid4()),
            "job_type": "run_experiment_task",
            "entity_id": run_id,
            "job_id": f"run-experiment:{run_id}",
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        })
        experiment["status"] = "running"
        if idempotency_key and actor_id:
            idem_table.append({
                "experiment_id": experiment["id"],
                "actor_id": actor_id,
                "idempotency_key": idempotency_key,
                "request_fingerprint": request_fingerprint,
                "run_id": run_id,
            })
        return run_id


class FakeArq:
    def __init__(self):
        self.jobs = []
        self.enqueued = []
        self._job_ids = set()

    async def enqueue_job(self, name, *args, _job_id=None):
        if _job_id is not None:
            if _job_id in self._job_ids:
                return None
            self._job_ids.add(_job_id)
        self.jobs.append((name, args))
        self.enqueued.append((name, args, _job_id))
        return None

    async def aclose(self):
        return None

    async def set(self, key, value, ex=None):
        self.heartbeat = (key, value, ex)
        return True


def agent_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path
        if path == "/v1/codegen":
            req = CodegenRequest.model_validate(body)
            bundle = generate_code(req.card, req.plan)
            return httpx.Response(200, json={"bundle": bundle.model_dump(mode="json")})
        if path == "/v1/codegen/validate":
            req = StaticValidateRequest.model_validate(body)
            return httpx.Response(200, json=validate_bundle(req.bundle).model_dump(mode="json"))
        if path == "/v1/codegen/classify":
            req = ClassifyRequest.model_validate(body)
            return httpx.Response(200, json=classify_error(req).model_dump(mode="json"))
        if path == "/v1/codegen/repair":
            req = RepairRequest.model_validate(body)
            return httpx.Response(200, json=repair_bundle(req).model_dump(mode="json"))
        if path == "/v1/evaluate":
            req = EvaluateRequest.model_validate(body)
            return httpx.Response(200, json=evaluate_run(req).model_dump(mode="json"))
        return httpx.Response(404, json={"detail": f"unexpected {path}"})

    return httpx.MockTransport(handler)


def make_result(status, exit_code, stderr="", metrics=None, artifacts=None) -> RunResult:
    return RunResult(
        run_id=uuid4(),
        status=status,
        exit_code=exit_code,
        stdout="epoch 1 loss=0.1" if status is RunStatus.SUCCEEDED else "",
        stderr=stderr,
        execution_time=1.5,
        metrics=metrics or {},
        artifacts=artifacts or [],
        error=stderr or None,
        audit=RunAudit(seed=42, code_version=1, plan_version=1),
    )


def runner_transport(results: list[RunResult]) -> httpx.MockTransport:
    """Each POST /runs consumes the next scripted result; GET returns it terminal."""
    queue = list(results)
    assigned: dict[str, RunResult] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/runs":
            result = queue.pop(0)
            run_id = str(uuid4())
            assigned[run_id] = result.model_copy(update={"run_id": UUID(run_id)})
            return httpx.Response(202, json={"run_id": run_id, "status": "queued"})
        if request.method == "GET" and path.startswith("/runs/") and path.endswith("/logs"):
            run_id = path.split("/")[2]
            result = assigned.get(run_id)
            if result is None:
                return httpx.Response(404, json={"detail": "run not found"})
            return httpx.Response(200, json={"stdout": result.stdout, "stderr": result.stderr})
        if request.method == "GET" and path.startswith("/runs/") and "/artifacts/" in path:
            return httpx.Response(200, content=b"artifact-bytes")
        if request.method == "GET" and path.startswith("/runs/") and path.endswith("/artifacts"):
            run_id = path.split("/")[2]
            return httpx.Response(200, json=assigned[run_id].artifacts)
        if request.method == "GET" and path.startswith("/runs/"):
            run_id = path.split("/")[2]
            return httpx.Response(200, json=assigned[run_id].model_dump(mode="json"))
        if request.method == "POST" and path.endswith("/cancel"):
            run_id = path.split("/")[2]
            base = assigned.get(run_id) or make_result(RunStatus.CANCELLED, None)
            cancelled = base.model_copy(update={"status": RunStatus.CANCELLED})
            return httpx.Response(200, json=cancelled.model_dump(mode="json"))
        return httpx.Response(404, json={"detail": f"unexpected {path}"})

    return httpx.MockTransport(handler)


def seed_tables(*, approved=True, with_spec=True):
    card = make_card()
    plan = build_plan(card)
    return {
        "projects": [{"id": str(PROJECT_ID), "user_id": str(USER_ID)}],
        "papers": [
            {
                "id": str(PAPER_ID),
                "project_id": str(PROJECT_ID),
                "title": "LeNet-5",
                "pdf_path": "u/p.pdf",
                "projects": {"user_id": str(USER_ID)},
            }
        ],
        "reproduction_specs": [
            {
                "id": str(SPEC_ID),
                "paper_id": str(PAPER_ID),
                "version": 1,
                "spec_json": card.model_dump(mode="json"),
                "status": "confirmed",
            }
        ],
        "experiments": [
            {
                "id": str(EXPERIMENT_ID),
                "project_id": str(PROJECT_ID),
                "paper_id": str(PAPER_ID),
                "spec_id": str(SPEC_ID) if with_spec else None,
                "status": "approved",
                "projects": {"user_id": str(USER_ID)},
            }
        ],
        "experiment_plans": [
            {
                "id": str(PLAN_ID),
                "experiment_id": str(EXPERIMENT_ID),
                "version": 1,
                "config_json": plan.model_dump(mode="json"),
                "approved_at": "2026-01-01T00:00:00+00:00" if approved else None,
            }
        ],
        "execution_runs": [],
        "execution_attempts": [],
        "execution_attempt_files": [],
        "workflows": [],
        "comparisons": [],
        "job_outbox": [],
        "execution_run_idempotency": [],
        "generated_files": [],
        "execution_run_files": [],
        "execution_artifacts": [],
    }


@pytest.fixture
def env(monkeypatch):
    tables = seed_tables()
    admin = FakeAdmin(tables, {str(PROJECT_ID): str(USER_ID)})
    monkeypatch.setattr(runs, "make_service_client", lambda s: admin)

    async def offline_redis_pool(*args, **kwargs):
        return None

    monkeypatch.setattr("reproagent_api.main.create_pool", offline_redis_pool)
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    app = create_app(settings)
    app.state.runner_transport = runner_transport([make_result(RunStatus.SUCCEEDED, 0)])
    app.dependency_overrides[get_anon_client] = lambda: SimpleNamespace(auth=FakeAuth())
    with TestClient(app) as client:
        app.state.arq = FakeArq()
        yield client, admin, app


def test_start_run_requires_auth(env):
    client, _, _ = env
    response = client.post(f"/v1/experiments/{EXPERIMENT_ID}/runs", headers=IDEM_KEY)
    assert response.status_code == 401


def test_start_run_enqueues_and_sets_running(env):
    client, admin, app = env
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert len(admin.tables["execution_runs"]) == 1
    run = admin.tables["execution_runs"][0]
    assert run["plan_id"] == str(PLAN_ID)
    assert run["plan_version"] == 1
    assert admin.tables["experiments"][0]["status"] == "running"
    assert app.state.arq.jobs == [("run_experiment_task", (body["run_id"],))]


def test_start_run_without_approved_plan_conflicts(env):
    client, admin, _ = env
    admin.tables["experiment_plans"][0]["approved_at"] = None
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
    )
    assert response.status_code == 409


def test_start_run_foreign_user_not_found(env):
    client, admin, _ = env
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**BOB_AUTH, **IDEM_KEY}
    )
    assert response.status_code == 404
    assert admin.rpc_calls == []
    assert admin.tables["job_outbox"] == []


def _seed_run(admin, status="succeeded", runner_run_id=None):
    run_id = str(uuid4())
    admin.tables["execution_runs"].append(
        {
            "id": run_id,
            "experiment_id": str(EXPERIMENT_ID),
            "plan_id": str(PLAN_ID),
            "code_version": 1,
            "plan_version": 1,
            "status": status,
            "repair_attempt": 0,
            "exit_code": 0,
            "metrics_json": {"accuracy": 0.98},
            "audit_json": {"seed": 42},
            "runtime_json": {"execution_time": 1.5},
            "error_category": None,
            "error": None,
            "code_hash": "abc",
            "runner_run_id": runner_run_id,
            "stdout_path": "p/stdout.txt",
            "stderr_path": "p/stderr.txt",
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "experiments": {"project_id": str(PROJECT_ID), "projects": {"user_id": str(USER_ID)}},
        }
    )
    return run_id


def test_get_run_detail_and_ownership(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    detail = client.get(f"/v1/runs/{run_id}", headers=AUTH)
    assert detail.status_code == 200
    assert detail.json()["metrics_json"]["accuracy"] == 0.98
    assert client.get(f"/v1/runs/{run_id}", headers=BOB_AUTH).status_code == 404


def test_prediction_examples_are_private_versioned_run_artifacts(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    url = f"/v1/runs/{run_id}/prediction-examples"
    assert client.get(url, headers=AUTH).status_code == 404
    path = f"{run_id}/attempt-0/artifacts/prediction_examples.json"
    admin.tables["execution_artifacts"].append({"run_id": run_id, "storage_path": path})
    sample = {"index": 0, "label": 7, "prediction": 1, "pixels": "00" * 784}
    admin.storage.files[path] = json.dumps({"version": 1, "samples": [sample]}).encode()
    assert client.get(url, headers=BOB_AUTH).status_code == 404
    assert client.get(url, headers=AUTH).json()["samples"] == [sample]
    admin.storage.files[path] = b"{}"
    assert client.get(url, headers=AUTH).status_code == 502


def test_list_runs(env):
    client, admin, _ = env
    _seed_run(admin)
    response = client.get(f"/v1/experiments/{EXPERIMENT_ID}/runs", headers=AUTH)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_files_and_artifacts(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    file_id = str(uuid4())
    admin.tables["generated_files"].append(
        {
            "id": file_id,
            "experiment_id": str(EXPERIMENT_ID),
            "version": 1,
            "path": "train.py",
            "sha256": "deadbeef",
            "content": "print('hi')",
            "change_reason": "initial deterministic codegen",
            "related_error": None,
        }
    )
    admin.tables["execution_run_files"].append(
        {"run_id": run_id, "generated_file_id": file_id}
    )
    admin.tables["execution_artifacts"].append(
        {
            "id": str(uuid4()),
            "run_id": run_id,
            "artifact_type": "metrics.json",
            "storage_path": f"{run_id}/attempt-0/artifacts/metrics.json",
            "metadata_json": {"attempt": 0},
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    files = client.get(f"/v1/runs/{run_id}/files", headers=AUTH).json()
    assert files[0]["path"] == "train.py"
    assert files[0]["sha256"] == "deadbeef"
    artifacts = client.get(f"/v1/runs/{run_id}/artifacts", headers=AUTH).json()
    assert artifacts[0]["artifact_type"] == "metrics.json"


def test_cancel_running_run_calls_runner(env):
    client, admin, app = env
    runner_run_id = str(uuid4())
    run_id = _seed_run(admin, status="running", runner_run_id=runner_run_id)
    cancelled = make_result(RunStatus.CANCELLED, None)
    app.state.runner_transport = runner_transport([cancelled])
    # pre-assign so the cancel handler can find the runner run
    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert admin.tables["execution_runs"][0]["status"] == "cancelled"


def test_cancel_terminal_run_is_noop(env):
    client, admin, _ = env
    run_id = _seed_run(admin, status="succeeded")
    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_cancel_queued_run_without_runner(env):
    client, admin, _ = env
    run_id = _seed_run(admin, status="queued")
    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert admin.tables["execution_runs"][0]["status"] == "cancelled"


def test_cancel_dispatching_run_without_runner(env):
    client, admin, app = env
    run_id = _seed_run(admin, status="dispatching")
    expected = execution_request_id(UUID(run_id), 0)
    cancelled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cancelled.append(request.url.path.split("/")[2])
        return httpx.Response(404, json={"detail": "run not found"})

    app.state.runner_transport = httpx.MockTransport(handler)
    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert admin.tables["execution_runs"][0]["status"] == "cancelled"
    assert cancelled == [str(expected)]


def test_cancel_rechecks_runner_id_persisted_after_initial_null_read(env, monkeypatch):
    client, admin, app = env
    runner_run_id = str(uuid4())
    run_id = _seed_run(admin, status="running")
    cancelled: list[str] = []
    original_get_run = runs._get_run
    reads = 0

    def racing_get_run(admin, run_id, user_id):
        nonlocal reads
        row = original_get_run(admin, run_id, user_id)
        reads += 1
        if reads == 2:
            admin.table("execution_runs").update({"runner_run_id": runner_run_id}).eq(
                "id", str(run_id)
            ).execute()
            row["runner_run_id"] = runner_run_id
        return row

    def handler(request: httpx.Request) -> httpx.Response:
        cancelled.append(request.url.path.split("/")[2])
        result = make_result(RunStatus.CANCELLED, None).model_copy(
            update={"run_id": UUID(request.url.path.split("/")[2])}
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    monkeypatch.setattr(runs, "_get_run", racing_get_run)
    app.state.runner_transport = httpx.MockTransport(handler)
    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert cancelled == [runner_run_id]


def test_cancel_cancels_runner_id_discovered_on_final_read(env, monkeypatch):
    client, admin, app = env
    runner_run_id = str(uuid4())
    run_id = _seed_run(admin, status="running")
    expected = execution_request_id(UUID(run_id), 0)
    cancelled: list[str] = []
    original_get_run = runs._get_run
    reads = 0

    def racing_get_run(admin, run_id, user_id):
        nonlocal reads
        row = original_get_run(admin, run_id, user_id)
        reads += 1
        if reads == 3:
            admin.table("execution_runs").update({"runner_run_id": runner_run_id}).eq(
                "id", str(run_id)
            ).execute()
            row["runner_run_id"] = runner_run_id
        return row

    def handler(request: httpx.Request) -> httpx.Response:
        cancelled.append(request.url.path.split("/")[2])
        result = make_result(RunStatus.CANCELLED, None).model_copy(
            update={"run_id": UUID(request.url.path.split("/")[2])}
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    monkeypatch.setattr(runs, "_get_run", racing_get_run)
    app.state.runner_transport = httpx.MockTransport(handler)
    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert cancelled == [str(expected), runner_run_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "row_attempt", "artifact_attempt"),
    [("succeeded", 0, 0), ("running", 1, 1)],
)
async def test_stale_result_persistence_preserves_protected_artifacts(
    env, status, row_attempt, artifact_attempt
):
    _, admin, _ = env
    run_id = _seed_run(admin, status=status)
    admin.tables["execution_runs"][0]["repair_attempt"] = row_attempt
    protected = {
        "id": str(uuid4()),
        "run_id": run_id,
        "artifact_type": "protected.txt",
        "storage_path": f"{run_id}/attempt-{artifact_attempt}/artifacts/protected.txt",
        "metadata_json": {"attempt": artifact_attempt},
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    admin.tables["execution_artifacts"].append(protected)

    class ArtifactRunner:
        async def get_artifact_bytes(self, runner_run_id, name):
            return b"stale"

    result = make_result(RunStatus.FAILED, 1, artifacts=["stale.txt"]).model_copy(
        update={"run_id": UUID(run_id)}
    )
    await async_persist_result(admin, run_id, uuid4(), result, ArtifactRunner(), 0)

    assert admin.tables["execution_artifacts"] == [protected]
    assert admin.tables["execution_runs"][0]["metrics_json"] == {"accuracy": 0.98}


def test_cancel_preserves_success_that_completes_during_runner_call(env):
    client, admin, app = env
    runner_run_id = str(uuid4())
    run_id = _seed_run(admin, status="running", runner_run_id=runner_run_id)
    finished_at = "2026-01-01T00:00:00+00:00"

    def handler(request: httpx.Request) -> httpx.Response:
        admin.table("execution_runs").update(
            {"status": "succeeded", "finished_at": finished_at, "error": "real result"}
        ).eq("id", run_id).execute()
        cancelled = make_result(RunStatus.CANCELLED, None)
        return httpx.Response(200, json=cancelled.model_dump(mode="json"))

    app.state.runner_transport = httpx.MockTransport(handler)
    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    assert run["finished_at"] == finished_at
    assert run["error"] == "real result"


def test_cancel_runner_failure_returns_502(env):
    client, admin, app = env
    run_id = _seed_run(admin, status="running", runner_run_id=str(uuid4()))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(500, json={"detail": "runner exploded"})
        return httpx.Response(404, json={"detail": "unexpected"})

    app.state.runner_transport = httpx.MockTransport(handler)
    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)
    assert response.status_code == 502
    assert admin.tables["execution_runs"][0]["status"] == "running"


def test_logs_reads_from_storage(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    admin.storage.files["p/stdout.txt"] = b"epoch 1 done"
    admin.storage.files["p/stderr.txt"] = b""
    logs = client.get(f"/v1/runs/{run_id}/logs", headers=AUTH).json()
    assert logs["stdout"] == "epoch 1 done"
    assert logs["stderr"] == ""
    assert client.get(f"/v1/runs/{run_id}/logs", headers=BOB_AUTH).status_code == 404


def test_logs_proxies_runner_while_running(env):
    client, admin, app = env
    runner_run_id = str(uuid4())
    run_id = _seed_run(admin, status="running", runner_run_id=runner_run_id)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"stdout": "epoch 1/3 live", "stderr": ""})

    app.state.runner_transport = httpx.MockTransport(handler)
    logs = client.get(f"/v1/runs/{run_id}/logs", headers=AUTH).json()
    assert logs["stdout"] == "epoch 1/3 live"
    assert seen["path"] == f"/runs/{runner_run_id}/logs"


def test_logs_running_falls_back_to_storage_when_runner_down(env):
    client, admin, app = env
    run_id = _seed_run(admin, status="running", runner_run_id=str(uuid4()))
    admin.storage.files["p/stdout.txt"] = b"persisted output"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("runner unreachable", request=request)

    app.state.runner_transport = httpx.MockTransport(handler)
    logs = client.get(f"/v1/runs/{run_id}/logs", headers=AUTH).json()
    assert logs["stdout"] == "persisted output"


def test_logs_terminal_uses_storage_even_with_runner_id(env):
    client, admin, app = env
    run_id = _seed_run(admin, status="succeeded", runner_run_id=str(uuid4()))
    admin.storage.files["p/stdout.txt"] = b"final output"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("runner must not be called for terminal runs")

    app.state.runner_transport = httpx.MockTransport(handler)
    logs = client.get(f"/v1/runs/{run_id}/logs", headers=AUTH).json()
    assert logs["stdout"] == "final output"


# --- Worker repair loop -----------------------------------------------------


@contextmanager
def _always_acquired(settings, workflow_id):
    """Keep direct worker calls offline by replacing the Postgres advisory lock."""
    yield True


def run_worker(admin, results, monkeypatch, agent_tr=None):
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    monkeypatch.setattr(worker, "_drive_lock", _always_acquired)
    run_id = str(uuid4())
    admin.tables["execution_runs"].append(
        {
            "id": run_id,
            "experiment_id": str(EXPERIMENT_ID),
            "plan_id": str(PLAN_ID),
            "code_version": 1,
            "plan_version": 1,
            "status": "queued",
            "repair_attempt": 0,
            "metrics_json": {},
            "audit_json": {},
            "runtime_json": {},
        }
    )
    ctx = {
        "settings": settings,
        "agent_transport": agent_tr or agent_transport(),
        "runner_transport": runner_transport(results),
        "checkpointer": make_memory_checkpointer(),
    }
    import asyncio

    asyncio.run(worker.run_experiment_task(ctx, run_id))
    return run_id


def test_worker_success_persists_versions_and_artifacts(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    metrics = {
        "schema_version": "1.0",
        "target_metric": "accuracy",
        "metric_value": 0.985,
        "accuracy": 0.985,
    }
    result = make_result(
        RunStatus.SUCCEEDED, 0, metrics=metrics, artifacts=["metrics.json", "model.pt"]
    )
    run_id = run_worker(admin, [result], monkeypatch)
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    assert run["exit_code"] == 0
    assert run["metrics_json"]["metric_value"] == 0.985
    assert run["finished_at"]
    assert admin.tables["experiments"][0]["status"] == "succeeded"
    # one code version (5 files) pinned to the run
    assert len(admin.tables["generated_files"]) == 5
    assert len(admin.tables["execution_run_files"]) == 5
    assert {f["version"] for f in admin.tables["generated_files"]} == {1}
    # logs + artifacts uploaded to storage; attempt paths are 1-based history ordinals
    assert f"{run_id}/attempt-1/stdout.txt" in admin.storage.files
    assert len(admin.tables["execution_artifacts"]) == 2
    # report auto-generated on the terminal path, grounded in real run data
    reports = admin.tables["reproduction_reports"]
    assert len(reports) == 1
    report = reports[0]["report_json"]
    assert report["final_status"] == "METHOD_VALIDATED"
    refs = report["references"]
    assert refs["run_id"] == run_id
    assert refs["plan_id"] == str(PLAN_ID)
    assert refs["spec_version_id"] == str(SPEC_ID)
    assert refs["paper_id"] == str(PAPER_ID)
    assert refs["plan_version"] == 1 and refs["code_version"] == 1
    assert len(refs["generated_file_ids"]) == 5
    acc = next(c for c in report["metric_comparisons"] if c["metric"] == "accuracy")
    assert acc["experiment_value"] == 0.985  # from real metrics_json, not fabricated
    assert acc["paper_value"] == 0.99
    assert "Full Reproduction" in report["summary"]
    import re

    sections = re.findall(r"^## (\d+)\. ", reports[0]["markdown"], re.MULTILINE)
    assert [int(s) for s in sections] == list(range(1, 15))


def test_worker_import_error_auto_repairs_then_succeeds(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    failed = make_result(
        RunStatus.FAILED, 1, stderr="ModuleNotFoundError: No module named 'nonexistent'"
    )
    success = make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})
    run_worker(admin, [failed, success], monkeypatch)
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    assert run["error_category"] == "IMPORT_ERROR"
    # two code versions appended; run pinned to the latest (version 2)
    versions = {f["version"] for f in admin.tables["generated_files"]}
    assert versions == {1, 2}
    pinned_ids = {p["generated_file_id"] for p in admin.tables["execution_run_files"]}
    v2_ids = {f["id"] for f in admin.tables["generated_files"] if f["version"] == 2}
    assert pinned_ids == v2_ids
    repaired = [f for f in admin.tables["generated_files"] if f["version"] == 2]
    assert all(f["related_error"] for f in repaired)
    assert all("auto-repair" in f["change_reason"] for f in repaired)


def test_worker_oom_creates_new_plan_version(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    oom = make_result(RunStatus.FAILED, 137, stderr="RuntimeError: CUDA out of memory")
    success = make_result(RunStatus.SUCCEEDED, 0, metrics={"metric_value": 0.9})
    run_worker(admin, [oom, success], monkeypatch)
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"
    assert run["error_category"] == "OUT_OF_MEMORY"
    plans = admin.tables["experiment_plans"]
    assert len(plans) == 2
    v2 = next(p for p in plans if p["version"] == 2)
    assert v2["origin"] == "oom_retry"
    assert v2["parent_version_id"] == str(PLAN_ID)
    assert v2["approved_at"]
    base_batch = plans[0]["config_json"]["training_config"]["batch_size"]["value"]
    new_batch = v2["config_json"]["training_config"]["batch_size"]["value"]
    assert new_batch == max(1, base_batch // 2)
    # base plan version untouched
    assert plans[0]["config_json"]["training_config"]["batch_size"]["value"] == base_batch
    assert run["plan_version"] == 2


def test_worker_static_validation_failure_repairs_then_fails(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    # sabotage the card so codegen still works but force a validation failure by
    # patching the agent validate endpoint to report a problem.
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    monkeypatch.setattr(worker, "make_service_client", lambda s: admin)
    monkeypatch.setattr(worker, "_drive_lock", _always_acquired)
    run_id = str(uuid4())
    admin.tables["execution_runs"].append(
        {
            "id": run_id,
            "experiment_id": str(EXPERIMENT_ID),
            "plan_id": str(PLAN_ID),
            "code_version": 1,
            "plan_version": 1,
            "status": "queued",
            "repair_attempt": 0,
        }
    )

    base = agent_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/codegen":
            req = CodegenRequest.model_validate(body)
            bundle = generate_code(req.card, req.plan)
            return httpx.Response(200, json={"bundle": bundle.model_dump(mode="json")})
        if request.url.path == "/v1/codegen/validate":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "issues": [
                        {"code": "banned", "message": "uses os.environ", "path": "train.py"}
                    ],
                },
            )
        if request.url.path == "/v1/codegen/repair":
            return base.handle_request(request)
        if request.url.path == "/v1/evaluate":
            req = EvaluateRequest.model_validate(json.loads(request.content))
            return httpx.Response(200, json=evaluate_run(req).model_dump(mode="json"))
        return httpx.Response(404)

    import asyncio

    ctx = {
        "settings": settings,
        "agent_transport": httpx.MockTransport(handler),
        "runner_transport": runner_transport([make_result(RunStatus.SUCCEEDED, 0)]),
        "checkpointer": make_memory_checkpointer(),
    }
    asyncio.run(worker.run_experiment_task(ctx, run_id))
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "failed"
    assert "static validation failed" in run["error"]
    assert admin.tables["experiments"][0]["status"] == "failed"
    # never reached the runner: no code version pinned
    assert admin.tables["execution_run_files"] == []
    wf = admin.tables["workflows"][-1]
    assert wf["status"] == "failed"
    assert wf["budget_json"]["repair_count"] == 3


def test_worker_non_repairable_error_stops(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    unknown = make_result(RunStatus.FAILED, 1, stderr="some totally novel failure xyz")
    run_worker(admin, [unknown], monkeypatch)
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "failed"
    assert run["error_category"] == "UNKNOWN_ERROR"
    assert len(admin.tables["generated_files"]) == 5  # only version 1, no repair
    # failed runs still get a report, with EXECUTION_FAILED and the real error
    reports = admin.tables["reproduction_reports"]
    assert len(reports) == 1
    assert reports[0]["report_json"]["final_status"] == "EXECUTION_FAILED"
    assert "some totally novel failure xyz" in reports[0]["markdown"]


def test_worker_report_failure_does_not_change_run_status(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    base = agent_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/evaluate":
            return httpx.Response(500, json={"detail": "evaluator down"})
        return base.handle_request(request)

    result = make_result(RunStatus.SUCCEEDED, 0, metrics={"accuracy": 0.98})
    run_worker(admin, [result], monkeypatch, agent_tr=httpx.MockTransport(handler))
    run = admin.tables["execution_runs"][0]
    assert run["status"] == "succeeded"  # best-effort report never alters the run
    assert admin.tables.get("reproduction_reports", []) == []


# --- Report endpoints ---------------------------------------------------------

FINAL_STATUSES = {
    "METHOD_VALIDATED",
    "SCALED_RUN_COMPLETED",
    "PARTIAL_MATCH",
    "DEVIATION_FOUND",
    "INSUFFICIENT_EVIDENCE",
    "EXECUTION_FAILED",
}


def _pin_real_code(admin, run_id):
    bundle = generate_from_plan(build_plan(make_card()))
    for file in bundle.files:
        file_id = str(uuid4())
        admin.tables["generated_files"].append(
            {
                "id": file_id,
                "experiment_id": str(EXPERIMENT_ID),
                "version": 1,
                "path": file.path,
                "sha256": file.sha256,
                "content": file.content,
                "change_reason": "initial deterministic codegen",
                "related_error": None,
            }
        )
        admin.tables["execution_run_files"].append(
            {"run_id": run_id, "generated_file_id": file_id}
        )


def test_get_report_missing_is_404(env):
    client, admin, _ = env
    run_id = _seed_run(admin)
    response = client.get(f"/v1/runs/{run_id}/report", headers=AUTH)
    assert response.status_code == 404


def test_regenerate_report_upserts_and_is_traceable(env):
    client, admin, app = env
    app.state.agent_transport = agent_transport()
    run_id = _seed_run(admin)
    _pin_real_code(admin, run_id)
    response = client.post(f"/v1/runs/{run_id}/report", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == run_id
    report = body["report_json"]
    assert report["final_status"] in FINAL_STATUSES
    assert report["references"]["run_id"] == run_id
    assert report["references"]["plan_version"] == 1
    assert report["references"]["code_version"] == 1
    assert len(report["method_checks"]) == 8
    assert len(report["experiment_checks"]) == 9
    assert "Full Reproduction" in report["summary"]
    assert body["markdown"].startswith("# 复现报告")
    # regenerate again -> upsert, still exactly one row
    again = client.post(f"/v1/runs/{run_id}/report", headers=AUTH)
    assert again.status_code == 200
    assert len(admin.tables["reproduction_reports"]) == 1
    # readable via GET, owned by the user only
    got = client.get(f"/v1/runs/{run_id}/report", headers=AUTH)
    assert got.status_code == 200
    assert got.json()["markdown"] == body["markdown"]
    assert client.get(f"/v1/runs/{run_id}/report", headers=BOB_AUTH).status_code == 404
    assert client.post(f"/v1/runs/{run_id}/report", headers=BOB_AUTH).status_code == 404


def test_regenerate_report_non_terminal_conflicts(env):
    client, admin, app = env
    app.state.agent_transport = agent_transport()
    run_id = _seed_run(admin, status="running")
    response = client.post(f"/v1/runs/{run_id}/report", headers=AUTH)
    assert response.status_code == 409


def _delegate_plan(admin, actor=USER_ID):
    from reproagent_api.services.plan_edits import apply_delegated_review

    row = admin.tables["experiment_plans"][0]
    plan = ExperimentPlan.model_validate(row["config_json"])
    context = ReviewRequest(card=make_card(), plan=plan, scope=str(actor))
    plan, records = apply_delegated_review(plan, [FieldRecommendation(
        id=str(uuid4()), field_path=f.field_path, action="accept", suggested_value=f.value,
        confidence=0.95, reasoning="Bounded proposal", risk="Monitor training",
    ) for f in context.review_fields()], actor=actor)
    row["config_json"] = plan.model_dump(mode="json")
    row["changes_json"] = [r.model_dump(mode="json") for r in records]
    return row


def _invalidate_authority(admin, problem):
    row = admin.tables["experiment_plans"][0]
    if problem == "missing_approval":
        row["approved_at"] = None
    elif problem == "wrong_owner":
        _delegate_plan(admin, BOB_ID)
    elif problem == "new_draft":
        admin.tables["experiment_plans"].append({
            **row, "id": str(uuid4()), "version": 2, "approved_at": None,
        })
    elif problem == "new_approved":
        admin.tables["experiment_plans"].append({**row, "id": str(uuid4()), "version": 2})
    elif problem == "wrong_experiment":
        row["experiment_id"] = str(uuid4())
    elif problem == "wrong_spec":
        admin.tables["reproduction_specs"][0]["paper_id"] = str(uuid4())
    elif problem == "draft_card":
        admin.tables["reproduction_specs"][0]["status"] = "draft"
    elif problem == "missing_owner":
        admin.tables["projects"] = []
    elif problem == "mutated_plan":
        row["config_json"]["training_config"]["learning_rate"]["value"] = 0.5
    elif problem == "critical_pending":
        row["config_json"]["assumptions"].append({
            "field_path": "training_config.learning_rate", "kind": "inferred", "value": 0.01,
            "rationale": "Not yet reviewed", "requires_confirmation": True,
            "human_confirmed": False, "risk_tier": "critical",
        })


@pytest.mark.parametrize("problem", [
    "missing_approval", "wrong_owner", "new_draft", "new_approved", "wrong_experiment",
    "wrong_spec", "draft_card", "missing_owner", "critical_pending",
])
def test_worker_rechecks_approval_owner_and_latest_pinned_version(monkeypatch, problem):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    _invalidate_authority(admin, problem)
    run_worker(admin, [], monkeypatch)
    assert admin.tables["execution_runs"][0]["status"] == "failed"
    assert admin.tables["generated_files"] == []
    assert admin.tables.get("workflows", []) == []


@pytest.mark.parametrize("problem", [
    "missing_approval", "wrong_owner", "new_draft", "wrong_spec", "draft_card",
    "missing_owner", "critical_pending",
])
def test_runs_api_rechecks_approval_and_authority(env, problem):
    client, admin, app = env
    _invalidate_authority(admin, problem)
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
    )
    assert response.status_code == 409
    assert admin.tables["execution_runs"] == []
    assert admin.tables["job_outbox"] == []
    assert admin.rpc_calls == []
    assert app.state.arq.jobs == []


def test_runs_api_uses_latest_approved_and_allows_valid_delegation(env):
    client, admin, _ = env
    row = _delegate_plan(admin)
    newest = {**row, "id": str(uuid4()), "version": 2}
    admin.tables["experiment_plans"].append(newest)
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
    )
    assert response.status_code == 202
    assert admin.tables["execution_runs"][0]["plan_id"] == newest["id"]


@pytest.mark.parametrize("problem", [
    "new_draft", "new_approved", "missing_approval", "wrong_owner", "mutated_plan",
])
def test_execution_rechecks_after_codegen_without_switching_plans(monkeypatch, problem):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    base = agent_transport()

    def handler(request):
        response = base.handle_request(request)
        if request.url.path == "/v1/codegen/validate":
            _invalidate_authority(admin, problem)
        return response

    run_worker(admin, [], monkeypatch, agent_tr=httpx.MockTransport(handler))
    assert admin.tables["execution_runs"][0]["status"] == "failed"
    assert admin.tables["execution_runs"][0]["plan_id"] == str(PLAN_ID)
    assert admin.tables["generated_files"] == []
    assert admin.tables["execution_run_files"] == []


def test_oom_normal_scaling_never_claims_human_confirmation(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    before = json.dumps(admin.tables["experiment_plans"][0], sort_keys=True)
    oom = make_result(RunStatus.FAILED, 137, stderr="CUDA out of memory")
    run_worker(admin, [oom, make_result(RunStatus.SUCCEEDED, 0)], monkeypatch)
    assert json.dumps(admin.tables["experiment_plans"][0], sort_keys=True) == before
    row = admin.tables["experiment_plans"][1]
    param = row["config_json"]["training_config"]["batch_size"]
    assert param["source"] == "mvp_scaling"
    assert param["human_confirmed"] is False and param["confirmed_by"] is None
    decision = row["changes_json"][0]
    assert decision["actor"] is None and decision["confirmed_by"] is None
    assert "System OOM scaling" in decision["recommendation_reasoning"]
    assert row["approved_at"]


def test_oom_delegated_preserves_only_existing_authority_and_system_audit(monkeypatch):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    original = _delegate_plan(admin)
    before = json.dumps(original, sort_keys=True)
    oom = make_result(RunStatus.FAILED, 137, stderr="CUDA out of memory")
    run_worker(admin, [oom, make_result(RunStatus.SUCCEEDED, 0)], monkeypatch)
    assert admin.tables["execution_runs"][0]["status"] == "succeeded"
    assert json.dumps(original, sort_keys=True) == before
    row = admin.tables["experiment_plans"][1]
    plan = ExperimentPlan.model_validate(row["config_json"])
    assert plan.approval_ready(USER_ID)
    assert row["config_json"]["delegated_at"] == original["config_json"]["delegated_at"]
    assert row["config_json"]["delegated_by"] == original["config_json"]["delegated_by"]
    param = plan.training_config.batch_size
    assert param.source.value == "inferred" and param.confirmed_by.value == "auto"
    assert not param.human_confirmed and param.requires_confirmation
    assert len(row["changes_json"]) == 2
    assert all(r["actor"] is None for r in row["changes_json"])
    assert row["changes_json"][1]["confirmed_by"] == "auto"
    assert row["changes_json"][0]["before"] == row["changes_json"][0]["after"]


@pytest.mark.parametrize("delegated", [False, True])
def test_oom_stops_at_existing_replan_budget(monkeypatch, delegated):
    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    if delegated:
        _delegate_plan(admin)
    oom = make_result(RunStatus.FAILED, 137, stderr="CUDA out of memory")
    run_worker(admin, [oom, oom, oom], monkeypatch)
    assert len(admin.tables["experiment_plans"]) == 3  # initial + at most two replans
    assert admin.tables["execution_runs"][0]["status"] == "failed"
    assert admin.tables["workflows"][0]["budget_json"]["replan_count"] == 2


@pytest.mark.parametrize("problem", ["wrong_owner", "missing_approval", "new_draft", "minimum"])
def test_oom_cannot_create_new_authority_or_exceed_scaling_scope(problem):
    from reproagent_api.services.runs import create_oom_plan

    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    row = _delegate_plan(admin)
    run_id = UUID(_seed_run(admin, status="running"))
    if problem == "minimum":
        row["config_json"]["training_config"]["batch_size"]["value"] = 1
    elif problem == "wrong_owner":
        row["config_json"]["delegated_by"] = str(BOB_ID)
    else:
        _invalidate_authority(admin, problem)
    count = len(admin.tables["experiment_plans"])
    with pytest.raises(ValueError):
        create_oom_plan(admin, str(EXPERIMENT_ID), row, run_id, "CUDA out of memory")
    assert len(admin.tables["experiment_plans"]) == count


def test_recovery_cannot_fall_back_to_historical_approval():
    from reproagent_schemas import BudgetConfig

    admin = FakeAdmin(seed_tables(), {str(PROJECT_ID): str(USER_ID)})
    _invalidate_authority(admin, "new_draft")
    with pytest.raises(ValueError, match="not been approved"):
        worker._recovery_execution_state(admin, {
            "experiment_id": str(EXPERIMENT_ID), "project_id": str(PROJECT_ID),
            "paper_id": str(PAPER_ID), "spec_version_id": str(SPEC_ID),
        }, BudgetConfig())


# --- Idempotency-Key and authorization boundary tests -----------------------


def test_start_run_missing_idempotency_key(env):
    client, _, _ = env
    response = client.post(f"/v1/experiments/{EXPERIMENT_ID}/runs", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_start_run_invalid_idempotency_key(env):
    client, _, _ = env
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs",
        headers={**AUTH, "Idempotency-Key": "short"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_INVALID"


def test_stale_plan_cannot_authorize_newer_unapproved(env):
    """Old approved plan cannot authorize if a newer unapproved plan exists."""
    client, admin, _ = env
    # Create a new version 2 that is NOT approved
    admin.tables["experiment_plans"].append({
        "id": str(uuid4()),
        "experiment_id": str(EXPERIMENT_ID),
        "version": 2,
        "config_json": admin.tables["experiment_plans"][0]["config_json"],
        "approved_at": None,
    })
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
    )
    # Python fast-fail rejects this before RPC is reached
    assert response.status_code == 409
    assert admin.tables["execution_runs"] == []


def test_active_run_exists_blocks_new(env):
    """Cannot create a second active run for the same experiment."""
    client, admin, _ = env
    # Create initial run (approved plan v1)
    resp1 = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs",
        headers={**AUTH, "Idempotency-Key": "key-first-run"},
    )
    assert resp1.status_code == 202
    # Attempt second run with different key
    resp2 = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs",
        headers={**AUTH, "Idempotency-Key": "key-second-run"},
    )
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["code"] == "ACTIVE_RUN_EXISTS"


def test_idempotent_retry_returns_same_run(env):
    """Same Idempotency-Key + fingerprint returns the original run without duplication."""
    client, admin, _ = env
    resp1 = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs",
        headers={**AUTH, "Idempotency-Key": "same-key-12345"},
    )
    assert resp1.status_code == 202
    run_id_1 = resp1.json()["run_id"]
    # Retry with same key
    resp2 = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs",
        headers={**AUTH, "Idempotency-Key": "same-key-12345"},
    )
    assert resp2.status_code == 202
    run_id_2 = resp2.json()["run_id"]
    assert run_id_1 == run_id_2
    # Only one run exists
    assert len(admin.tables["execution_runs"]) == 1
    # Only one outbox row
    assert len(admin.tables["job_outbox"]) == 1


def test_idempotency_conflict_different_fingerprint(env):
    """Same key but different fingerprint raises IDEMPOTENCY_CONFLICT."""
    client, admin, _ = env
    # First call creates run with a fingerprint
    resp1 = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs",
        headers={**AUTH, "Idempotency-Key": "conflict-key-1"},
    )
    assert resp1.status_code == 202
    # Manually alter the stored idempotency fingerprint to simulate a conflict
    admin.tables["execution_run_idempotency"][0]["request_fingerprint"] = "different-hash"
    # Second call with same key but computed fingerprint differs
    resp2 = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs",
        headers={**AUTH, "Idempotency-Key": "conflict-key-1"},
    )
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_rpc_stale_plan_approval(env):
    """RPC-level STALE_PLAN_APPROVAL maps the structured code to a 409."""
    client, admin, _ = env

    def stale_rpc(name, params):
        admin.rpc_calls.append((name, params))
        if name == "create_execution_run":
            raise _rpc_error("STALE_PLAN_APPROVAL")
        return None

    admin.rpc = stale_rpc
    # The seeded plan is the latest approved plan, so the Python fast-fail is
    # bypassed here only because the fake RPC itself refuses the call; the router
    # must read the structured code from the PostgREST error, not the repr.
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_PLAN_APPROVAL"
    assert admin.rpc_calls != []


def test_rpc_unknown_sqlstate_is_server_error_not_conflict(env):
    """A PostgREST error without a known authorization code is a 500, not a 409."""
    client, admin, _ = env

    def boom_rpc(name, params):
        admin.rpc_calls.append((name, params))
        # Same shared SQLSTATE the RPC uses, but an unrelated message.
        raise _rpc_error("permission denied for table execution_runs", sqlstate="42501")

    admin.rpc = boom_rpc
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "RUN_CREATION_FAILED"


def test_rpc_malformed_run_id_is_server_error(env):
    """A non-UUID value from a successful RPC surfaces as 500, not a 409 conflict."""
    client, admin, _ = env

    def garbage_rpc(name, params):
        admin.rpc_calls.append((name, params))
        return "not-a-uuid"

    admin.rpc = garbage_rpc
    response = client.post(
        f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "RUN_CREATION_FAILED"
    assert admin.tables["execution_runs"] == []


def test_rpc_non_postgrest_error_is_not_swallowed_as_conflict(env):
    """An unexpected Python error from the RPC path must not become a 409."""
    client, admin, _ = env

    def bug_rpc(name, params):
        admin.rpc_calls.append((name, params))
        raise KeyError("p_actor_id")

    admin.rpc = bug_rpc
    with pytest.raises(KeyError):
        client.post(
            f"/v1/experiments/{EXPERIMENT_ID}/runs", headers={**AUTH, **IDEM_KEY}
        )


# --- Python approval_ready rules (mirrors the SQL authorization contract) -----
#
# These scenarios pin down ExperimentPlan.approval_ready(actor) so the Python side
# cannot silently drift from the rules the create_execution_run RPC re-implements.
# The SQL half of the equivalence (the RPC making the same accept/reject decision)
# lives in tests/integration/test_run_authorization.py::TestApprovalReadyEquivalence
# and requires a live database: run with `pytest -m integration`.
_APPROVAL_READY_CASES = [
    ("assisted_clean", "assisted", "accuracy", [], "none", True),
    ("unsupported_metric", "assisted", "f1_score", [], "none", False),
    ("critical_unconfirmed_blocks", "assisted", "accuracy", [("critical", False, None)], "none", False),
    ("critical_confirmed_ok", "assisted", "accuracy", [("critical", True, None)], "none", True),
    ("routine_unconfirmed_ok", "assisted", "accuracy", [("routine", False, None)], "none", True),
    ("info_unconfirmed_ok", "assisted", "accuracy", [("info", False, None)], "none", True),
    ("delegated_clean", "delegated", "accuracy", [], "self", True),
    ("delegated_critical_blocks", "delegated", "accuracy", [("critical", False, None)], "self", False),
    ("delegated_routine_blocks", "delegated", "accuracy", [("routine", False, None)], "self", False),
    ("delegated_info_ok", "delegated", "accuracy", [("info", False, None)], "self", True),
    ("delegated_auto_routine_ok", "delegated", "accuracy", [("routine", False, "auto")], "self", True),
    ("delegated_actor_mismatch", "delegated", "accuracy", [], "other", False),
]


def _approval_case_plan(review_mode, target_metric, assumptions, delegation, actor, other):
    base = build_plan(make_card())
    plan = base.model_copy(update={"evaluation_config": base.evaluation_config.model_copy(
        update={"target_metric": target_metric}
    )})
    if assumptions:
        plan = plan.model_copy(update={"assumptions": [
            Assumption(
                field_path=f"training_config.case_{i}", kind=AssumptionKind.INFERRED,
                value=0.1, rationale="scenario", requires_confirmation=True,
                human_confirmed=human, confirmed_by=(ConfirmationSource(confirmed) if confirmed else None),
                risk_tier=RiskTier(tier),
            )
            for i, (tier, human, confirmed) in enumerate(assumptions)
        ]})
    if review_mode == "delegated":
        delegated_by = actor if delegation == "self" else other
        plan = plan.model_copy(update={
            "review_mode": ReviewMode.DELEGATED,
            "delegated_at": datetime.now(UTC),
            "delegated_by": delegated_by,
        })
    return plan


@pytest.mark.parametrize(("name", "review_mode", "metric", "assumptions", "delegation", "expected"),
                         _APPROVAL_READY_CASES, ids=[c[0] for c in _APPROVAL_READY_CASES])
def test_approval_ready_python_rules(name, review_mode, metric, assumptions, delegation, expected):
    actor, other = USER_ID, BOB_ID
    plan = _approval_case_plan(review_mode, metric, assumptions, delegation, actor, other)
    assert plan.approval_ready(actor) is expected, name
