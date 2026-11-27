import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

INTEGRATION_DIR = Path(__file__).parent
ROOT = INTEGRATION_DIR.parents[1]
MIGRATIONS = sorted((ROOT / "infra/supabase/migrations").glob("*.sql"))


@pytest.fixture(scope="session")
def database():
    """A disposable real Postgres database cloned from the configured template.

    Every repository migration runs in this database. Tests share one connection
    and roll back their transaction; the database is force-dropped at session end.
    The default stock template remains useful in fully bootstrapped Supabase
    environments. Task 9 uses ``REPRO_TEST_DATABASE_TEMPLATE=reproagent_task8_template``.
    """
    dsn = os.environ.get(
        "REPRO_TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:54322/postgres",
    )
    template = os.environ.get("REPRO_TEST_DATABASE_TEMPLATE", "template1")
    name = f"reproagent_negative_matrix_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5) as admin:
        admin.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(template)
            )
        )
        try:
            with psycopg.connect(
                dsn, dbname=name, autocommit=False, row_factory=dict_row, connect_timeout=5
            ) as connection:
                for migration in MIGRATIONS:
                    connection.execute(migration.read_text(encoding="utf-8"))
                # Stock Postgres templates do not include Supabase's default
                # application-role grants. Reproduce only the privileges needed
                # to exercise RLS (rather than mistaking permission errors for
                # ownership denial) in this disposable database.
                connection.execute(
                    "grant select on public.projects, public.papers, "
                    "public.reproduction_specs, public.experiments, "
                    "public.experiment_plans, public.generated_files, "
                    "public.execution_runs, public.execution_attempts, "
                    "public.execution_attempt_files, public.execution_run_files, "
                    "public.reproduction_reports, public.execution_artifacts, "
                    "public.workflows, public.agent_traces, storage.objects "
                    "to authenticated"
                )
                connection.execute(
                    "grant insert on public.papers to authenticated; "
                    "grant update on public.projects, public.papers, "
                    "public.experiments, public.execution_runs, public.workflows, "
                    "public.execution_artifacts to authenticated; "
                    "grant delete on public.projects to authenticated"
                )
                connection.commit()
                yield connection
        finally:
            admin.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )


@pytest.fixture
def db(database):
    with database.cursor() as cursor:
        yield cursor
        database.rollback()


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.is_relative_to(INTEGRATION_DIR):
            item.add_marker(pytest.mark.integration)
