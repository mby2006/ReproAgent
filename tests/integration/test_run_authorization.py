"""Integration: atomic run authorization via 6-arg create_execution_run RPC.

Requires a disposable Postgres database cloned from the configured template. The
shared ``db`` fixture applies every repository migration, rolls back each test,
and force-drops the database at session end. These tests verify:
  - STALE_PLAN_APPROVAL, PLAN_NOT_APPROVED, PLAN_NOT_READY,
    DELEGATION_ACTOR_MISMATCH
  - ACTIVE_RUN_EXISTS
  - Idempotency (same key + fingerprint returns existing run,
    different fingerprint conflicts)
  - approval_ready consistency between Python and SQL
  - Concurrent active-run uniqueness

Run:  uv run pytest tests/integration/test_run_authorization.py -m integration -v
"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from reproagent_schemas import (
    Assumption,
    AssumptionKind,
    ConfirmationSource,
    ExperimentPlan,
    ReviewMode,
    RiskTier,
)

pytestmark = pytest.mark.integration

INSERT_PLAN_SQL = (
    "insert into public.experiment_plans "
    "(id, experiment_id, version, config_json, approved_at) "
    "values (%s, %s, %s, %s::jsonb, %s)"
)


def run_creation_fingerprint(experiment_id, plan_id, plan_version):
    raw = f"{experiment_id}:{plan_id}:{plan_version}"
    return hashlib.sha256(raw.encode()).hexdigest()


def assert_service_role(cur):
    role = cur.execute(
        "select current_user, rolsuper, rolbypassrls "
        "from pg_roles where rolname = current_user"
    ).fetchone()
    assert role == {
        "current_user": "service_role", "rolsuper": False, "rolbypassrls": True
    }


def insert_auth_user(cur, user_id, prefix):
    """Auth-user scaffolding stays privileged; production SQL does not."""
    if cur.execute("select current_user").fetchone()["current_user"] != "postgres":
        cur.execute("set local role postgres")
    try:
        cur.execute(
            "insert into auth.users (id, email) values (%s, %s) on conflict do nothing",
            (str(user_id), f"{prefix}-{user_id}@example.com"),
        )
    finally:
        cur.execute("set local role service_role")


def call_rpc(
    cur, experiment_id, plan_id, plan_version, actor_id, idem_key, fingerprint
):
    """Call create_execution_run RPC; return (run_id, error_code)."""
    assert_service_role(cur)
    try:
        cur.execute(
            "select public.create_execution_run(%s, %s, %s, %s, %s, %s)",
            (experiment_id, plan_id, plan_version, actor_id, idem_key, fingerprint),
        )
        row = cur.fetchone()
        return next(iter(row.values())), None
    except (psycopg.errors.RaiseException, psycopg.errors.InsufficientPrivilege) as e:
        msg = str(e).strip()
        return None, msg


# ============================================================
# Actor Fixture
# ============================================================


@pytest.fixture
def actor(db):
    """Create an auth user, then switch production SQL to service_role."""
    user_id = uuid4()
    insert_auth_user(db, user_id, "test")
    assert_service_role(db)
    return str(user_id)


@pytest.fixture
def experiment(db, actor):
    """Create project -> paper -> spec -> experiment chain."""
    project_id = uuid4()
    paper_id = uuid4()
    spec_id = uuid4()
    exp_id = uuid4()

    db.execute(
        "insert into public.projects (id, user_id, name) values (%s, %s, %s)",
        (str(project_id), actor, "test project"),
    )
    db.execute(
        "insert into public.papers (id, project_id, title, pdf_path) "
        "values (%s, %s, %s, %s)",
        (str(paper_id), str(project_id), "Test Paper", "test/paper.pdf"),
    )
    db.execute(
        "insert into public.reproduction_specs "
        "(id, paper_id, version, spec_json, status, origin) "
        "values (%s, %s, 1, '{}'::jsonb, 'confirmed', 'extraction')",
        (str(spec_id), str(paper_id)),
    )
    db.execute(
        "insert into public.experiments "
        "(id, project_id, paper_id, spec_id, name, status) "
        "values (%s, %s, %s, %s, 'test exp', 'approved')",
        (str(exp_id), str(project_id), str(paper_id), str(spec_id)),
    )
    return str(exp_id)


# VALID_CONFIG is deliberately a schema-valid ExperimentPlan payload so the same
# dict can be validated by Python (approval_ready) and inserted as config_json for
# the SQL RPC, proving both paths decide identically on identical input.
VALID_CONFIG = {
    "review_mode": "assisted",
    "rationale": "test",
    "scaling_profile": "test",
    "delegated_at": None,
    "delegated_by": None,
    "dataset_config": {
        "name": "mnist", "num_classes": 10, "input_shape": [1, 28, 28],
        "train_subset_size": 100, "test_subset_size": 50,
    },
    "model_config": {
        "architecture": "lenet-5", "input_shape": [1, 28, 28], "num_classes": 10,
    },
    "preprocessing_config": {"steps": []},
    "training_config": {
        "optimizer": {
            "value": "SGD", "source": "paper_explicit",
            "evidence_quote": "...SGD...", "source_page": 8,
        },
        "learning_rate": {
            "value": 0.01, "source": "paper_explicit",
            "evidence_quote": "...0.01...", "source_page": 8,
        },
        "loss_function": {
            "value": "cross_entropy", "source": "paper_explicit",
            "evidence_quote": "...cross entropy...", "source_page": 8,
        },
        "batch_size": {"value": 32, "source": "mvp_scaling"},
        "epochs": {"value": 2, "source": "mvp_scaling"},
    },
    "evaluation_config": {
        "metrics": ["accuracy"], "target_metric": "accuracy",
        "tolerance": 0.05, "comparison_note": "test",
    },
    "runtime_budget": {"max_seconds": 60, "device": "cpu", "seed": 42},
    "expected_runtime": {"estimated_seconds": 30, "basis": "test"},
    "resource_estimates": {},
    "assumptions": [],
    "deviations_from_paper": [],
}

DELEGATED_CONFIG = {
    **VALID_CONFIG,
    "review_mode": "delegated",
}


@pytest.fixture
def plan(db, experiment, actor):
    """Insert a valid approved plan and return its id.

    ``approved_at`` must be a real timestamp value. Passing the Python string
    ``"now()"`` as a bind parameter is invalid: PostgreSQL cannot cast the
    literal text ``'now()'`` to ``timestamptz``. psycopg2 adapts a ``datetime``
    object to a timestamp literal, so we bind ``datetime.now(UTC)`` instead.
    """
    plan_id = uuid4()
    db.execute(
        INSERT_PLAN_SQL,
        (str(plan_id), experiment, 1, json.dumps(VALID_CONFIG), datetime.now(UTC)),
    )
    return str(plan_id)


# ============================================================
# Tests
# ============================================================


class TestAuthorization:
    def test_stale_plan_approval(self, db, experiment, actor, plan):
        """Older plan version cannot authorize if newer exists."""
        newer_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(newer_id), experiment, 2, json.dumps(VALID_CONFIG), None),
        )
        fp = run_creation_fingerprint(experiment, plan, 1)
        _, err = call_rpc(db, experiment, plan, 1, actor, "test-stale-001", fp)
        assert "STALE_PLAN_APPROVAL" in err

    def test_plan_not_approved(self, db, experiment, actor):
        """Unapproved plan fails with PLAN_NOT_APPROVED."""
        plan_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(plan_id), experiment, 1, json.dumps(VALID_CONFIG), None),
        )
        fp = run_creation_fingerprint(experiment, str(plan_id), 1)
        _, err = call_rpc(
            db, experiment, str(plan_id), 1, actor, "test-noappr-1", fp
        )
        assert "PLAN_NOT_APPROVED" in err

    def test_plan_not_ready_target_metric(self, db, experiment, actor):
        """Unsupported target metric fails with PLAN_NOT_READY."""
        bad_cfg = {
            **VALID_CONFIG,
            "evaluation_config": {
                **VALID_CONFIG["evaluation_config"],
                "target_metric": "f1_score",
            },
        }
        plan_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(plan_id), experiment, 1, json.dumps(bad_cfg), datetime.now(UTC)),
        )
        fp = run_creation_fingerprint(experiment, str(plan_id), 1)
        _, err = call_rpc(
            db, experiment, str(plan_id), 1, actor, "test-notready1", fp
        )
        assert "PLAN_NOT_READY" in err

    def test_plan_not_ready_critical_assumption(self, db, experiment, actor):
        """Unconfirmed critical assumption blocks run creation."""
        config = {**VALID_CONFIG, "assumptions": [{
            "field_path": "training_config.optimizer",
            "kind": "inferred",
            "value": "adam",
            "rationale": "not confirmed",
            "requires_confirmation": True,
            "human_confirmed": False,
            "risk_tier": "critical",
        }]}
        plan_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(plan_id), experiment, 1, json.dumps(config), datetime.now(UTC)),
        )
        fp = run_creation_fingerprint(experiment, str(plan_id), 1)
        _, err = call_rpc(
            db, experiment, str(plan_id), 1, actor, "test-crit-001", fp
        )
        assert "PLAN_NOT_READY" in err

    def test_delegation_actor_mismatch(self, db, experiment, actor):
        """Delegated plan with wrong actor fails."""
        other_actor = str(uuid4())
        insert_auth_user(db, UUID(other_actor), "other")
        config = {
            **DELEGATED_CONFIG,
            "delegated_by": other_actor,
            "delegated_at": datetime.now(UTC).isoformat(),
        }
        plan_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(plan_id), experiment, 1, json.dumps(config), datetime.now(UTC)),
        )
        fp = run_creation_fingerprint(experiment, str(plan_id), 1)
        _, err = call_rpc(
            db, experiment, str(plan_id), 1, actor, "test-deleg-01", fp
        )
        assert "DELEGATION_ACTOR_MISMATCH" in err

    def test_active_run_exists(self, db, experiment, actor, plan):
        """Cannot create a second active run."""
        fp = run_creation_fingerprint(experiment, plan, 1)
        run_id, err = call_rpc(db, experiment, plan, 1, actor, "test-active-1", fp)
        assert err is None
        assert run_id is not None

        # Attempt second run with different key
        _, err2 = call_rpc(db, experiment, plan, 1, actor, "test-active-2", fp)
        assert "ACTIVE_RUN_EXISTS" in err2


class TestIdempotency:
    def test_same_key_same_fingerprint_returns_existing(
        self, db, experiment, actor, plan
    ):
        fp = run_creation_fingerprint(experiment, plan, 1)
        run_id1, err1 = call_rpc(
            db, experiment, plan, 1, actor, "idem-same-0001", fp
        )
        assert err1 is None

        run_id2, err2 = call_rpc(
            db, experiment, plan, 1, actor, "idem-same-0001", fp
        )
        assert err2 is None
        assert run_id1 == run_id2

    def test_same_key_different_fingerprint_conflicts(
        self, db, experiment, actor, plan
    ):
        fp = run_creation_fingerprint(experiment, plan, 1)
        _, err1 = call_rpc(
            db, experiment, plan, 1, actor, "idem-diff-0001", fp
        )
        assert err1 is None

        # Same key, different fingerprint
        _, err2 = call_rpc(
            db, experiment, plan, 1, actor, "idem-diff-0001", "other-hash"
        )
        assert "IDEMPOTENCY_CONFLICT" in err2


class TestApprovalReadyConsistency:
    """Python approval_ready and SQL produce same blocking results."""

    def test_routine_assumptions_do_not_block(self, db, experiment, actor):
        """Routine-tier unconfirmed does not block assisted plans."""
        config = {**VALID_CONFIG, "assumptions": [{
            "field_path": "training_config.batch_size",
            "kind": "mvp_scaling",
            "value": 32,
            "rationale": "routine",
            "requires_confirmation": True,
            "human_confirmed": False,
            "risk_tier": "routine",
        }]}
        plan_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(plan_id), experiment, 1, json.dumps(config), datetime.now(UTC)),
        )
        fp = run_creation_fingerprint(experiment, str(plan_id), 1)
        run_id, err = call_rpc(
            db, experiment, str(plan_id), 1, actor, "test-routine-1", fp
        )
        assert err is None
        assert run_id is not None

    def test_info_assumptions_never_block(self, db, experiment, actor):
        """Info-tier never block, even in delegated mode."""
        config = {
            **DELEGATED_CONFIG,
            "delegated_by": actor,
            "delegated_at": datetime.now(UTC).isoformat(),
            "assumptions": [{
                "field_path": "runtime_budget.max_seconds",
                "kind": "mvp_scaling",
                "value": 60,
                "rationale": "info only",
                "requires_confirmation": True,
                "human_confirmed": False,
                "risk_tier": "info",
            }],
        }
        plan_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(plan_id), experiment, 1, json.dumps(config), datetime.now(UTC)),
        )
        fp = run_creation_fingerprint(experiment, str(plan_id), 1)
        _, err = call_rpc(
            db, experiment, str(plan_id), 1, actor, "test-info-001", fp
        )
        assert err is None

    def test_delegated_non_info_requires_confirmation(
        self, db, experiment, actor
    ):
        """Delegated mode: routine unconfirmed without AUTO blocks."""
        config = {
            **DELEGATED_CONFIG,
            "delegated_by": actor,
            "delegated_at": datetime.now(UTC).isoformat(),
            "assumptions": [{
                "field_path": "training_config.learning_rate",
                "kind": "inferred",
                "value": 0.1,
                "rationale": "not yet auto-confirmed",
                "requires_confirmation": True,
                "human_confirmed": False,
                "risk_tier": "routine",
            }],
        }
        plan_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(plan_id), experiment, 1, json.dumps(config), datetime.now(UTC)),
        )
        fp = run_creation_fingerprint(experiment, str(plan_id), 1)
        _, err = call_rpc(
            db, experiment, str(plan_id), 1, actor, "test-deleg-02", fp
        )
        assert "PLAN_NOT_READY" in err


class TestForbidden:
    def test_non_owner_forbidden(self, db, experiment, actor, plan):
        """Actor who does not own the experiment gets FORBIDDEN."""
        fake_actor = str(uuid4())
        insert_auth_user(db, UUID(fake_actor), "fake")
        fp = run_creation_fingerprint(experiment, plan, 1)
        _, err = call_rpc(
            db, experiment, plan, 1, fake_actor, "test-forbid-01", fp
        )
        assert "FORBIDDEN" in err


# (kind, expected_ready) pairs shared with the offline Python-only unit test in
# apps/api/tests/test_runs.py::test_approval_ready_python_rules. The SQL half of
# the equivalence only runs here, against a live Postgres: pytest -m integration.
_APPROVAL_READY_CASES = [
    ("assisted_clean", True),
    ("unsupported_metric", False),
    ("critical_unconfirmed", False),
    ("critical_confirmed", True),
    ("routine_unconfirmed", True),
    ("info_unconfirmed", True),
    ("delegated_clean", True),
    ("delegated_critical", False),
    ("delegated_routine", False),
    ("delegated_info", True),
    ("delegated_auto_routine", True),
    ("delegated_actor_mismatch", False),
]


def _assumption(field_path: str, tier: RiskTier, *, human: bool,
                confirmed: ConfirmationSource | None = None) -> dict:
    return Assumption(
        field_path=field_path, kind=AssumptionKind.INFERRED, value="scenario",
        rationale="scenario", requires_confirmation=True, human_confirmed=human,
        confirmed_by=confirmed, risk_tier=tier,
    ).model_dump(mode="json")


def _approval_ready_config(kind: str, actor: str) -> dict:
    """Return a schema-valid plan config for one approval_ready scenario.

    Every scenario is built as an ``ExperimentPlan`` and serialised, so the exact
    dict handed to PostgreSQL is the same model the Python ``approval_ready``
    predicate is evaluated against.
    """
    config = dict(VALID_CONFIG)
    other_actor = "00000000-0000-0000-0000-0000000000ff"
    delegated = kind.startswith("delegated_")
    if delegated:
        config["review_mode"] = ReviewMode.DELEGATED.value
        config["delegated_by"] = other_actor if kind == "delegated_actor_mismatch" else actor
        config["delegated_at"] = datetime.now(UTC).isoformat()
    if kind == "unsupported_metric":
        config["evaluation_config"] = {
            **config["evaluation_config"], "target_metric": "f1_score"
        }
    elif kind != "delegated_clean":
        assumptions = {
            "critical_unconfirmed": ("critical", False, None),
            "critical_confirmed": ("critical", True, None),
            "routine_unconfirmed": ("routine", False, None),
            "info_unconfirmed": ("info", False, None),
            "delegated_critical": ("critical", False, None),
            "delegated_routine": ("routine", False, None),
            "delegated_info": ("info", False, None),
            "delegated_auto_routine": ("routine", False, "auto"),
        }.get(kind)
        if assumptions is not None:
            tier, human, confirmed = assumptions
            config["assumptions"] = [_assumption(
                "training_config.optimizer", RiskTier(tier), human=human,
                confirmed=(ConfirmationSource(confirmed) if confirmed else None),
            )]
    return config


class TestApprovalReadyEquivalence:
    """The SQL authorization accepts/rejects exactly as ExperimentPlan.approval_ready does."""

    @pytest.mark.parametrize(("kind", "expected_ready"), _APPROVAL_READY_CASES)
    def test_python_and_sql_agree(self, db, experiment, actor, kind, expected_ready):
        config = _approval_ready_config(kind, actor)
        plan = ExperimentPlan.model_validate(config)
        python_ready = plan.approval_ready(UUID(actor))
        # The Python predicate matches the documented expectation for this scenario.
        assert python_ready is expected_ready, f"Python approval_ready drifted: {kind}"

        # Feed the identical serialised config to the database and assert the RPC
        # makes the same decision. Ownership/latest/approval are held constant, so
        # the accept/reject outcome depends only on the readiness rules.
        plan_id = uuid4()
        db.execute(
            INSERT_PLAN_SQL,
            (str(plan_id), experiment, 1,
             json.dumps(plan.model_dump(mode="json")), datetime.now(UTC)),
        )
        fp = run_creation_fingerprint(experiment, str(plan_id), 1)
        run_id, err = call_rpc(
            db, experiment, str(plan_id), 1, actor, f"equiv-{kind}-key", fp
        )
        if expected_ready:
            assert err is None, f"SQL rejected a plan Python accepts ({kind}): {err}"
            assert run_id is not None
        else:
            assert err is not None, f"SQL accepted a plan Python rejects: {kind}"
            assert "PLAN_NOT_READY" in err or "DELEGATION_ACTOR_MISMATCH" in err, (
                f"unexpected SQL rejection for {kind}: {err}"
            )
