import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from deploy.server.ops import (
    BackupError,
    Migration,
    MigrationError,
    Ops,
    OpsError,
    build_migration_sql,
    check_service_health,
    load_migrations,
    parse_ledger,
    pending_migrations,
    read_manifest,
    verify_backup,
    write_manifest,
)


def migration(tmp_path: Path, name="20261125000000_test.sql", sql="select 1;") -> Migration:
    path = tmp_path / name
    path.write_text(sql, encoding="utf-8")
    return Migration(path.stem, path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_migration_is_one_locked_transaction_with_ledger_write(tmp_path):
    item = migration(tmp_path, sql="alter table public.papers add column demo text;")
    sql = build_migration_sql(item)
    assert sql.startswith("BEGIN;\n")
    assert "pg_advisory_xact_lock" in sql
    assert "alter table public.papers add column demo text;" in sql
    assert f"'{item.version}', '{item.checksum}'" in sql
    assert sql.rstrip().endswith("COMMIT;")
    assert "IF NOT EXISTS (SELECT 1 FROM public.schema_migrations" in sql
    assert f"WHERE version = '{item.version}'" in sql
    assert "EXECUTE $reproagent_migration_sql$\nalter table" in sql
    assert "demo text;\n$reproagent_migration_sql$;" in sql


def test_migration_rechecks_checksum_after_acquiring_lock(tmp_path):
    sql = build_migration_sql(migration(tmp_path))
    assert "IF EXISTS (SELECT 1 FROM public.schema_migrations" in sql
    assert "AND checksum <>" in sql
    assert "RAISE EXCEPTION 'checksum mismatch" in sql


@pytest.mark.parametrize(
    "statement",
    [
        "create index concurrently papers_title_idx on public.papers (title);",
        "vacuum public.papers;",
        "alter type public.run_status add value 'queued';",
    ],
)
def test_migration_rejects_statements_that_cannot_run_in_a_transaction(tmp_path, statement):
    with pytest.raises(MigrationError, match="cannot run inside a transaction"):
        build_migration_sql(migration(tmp_path, sql=statement))


@pytest.mark.parametrize("sql", ["BEGIN; select 1; COMMIT;", "select 1;\nROLLBACK;"])
def test_migration_rejects_its_own_transaction_control(tmp_path, sql):
    with pytest.raises(MigrationError, match="transaction control"):
        build_migration_sql(migration(tmp_path, sql=sql))


def test_invalid_migration_filename_fails(tmp_path):
    migration(tmp_path, "not-a-version.sql")
    with pytest.raises(MigrationError, match="invalid migration filename"):
        load_migrations(tmp_path)


def test_applied_identical_migrations_are_noops_and_drift_fails(tmp_path):
    item = migration(tmp_path)
    assert pending_migrations([item], {item.version: item.checksum}) == []
    with pytest.raises(MigrationError, match="checksum mismatch"):
        pending_migrations([item], {item.version: "0" * 64})


def test_ledger_parser_ignores_empty_rows_and_rejects_malformed_data():
    checksum = "a" * 64
    assert parse_ledger(f"20261125000000_test|{checksum}\n\n") == {
        "20261125000000_test": checksum
    }
    with pytest.raises(MigrationError, match="malformed"):
        parse_ledger("missing-separator")


def test_storage_archive_rejects_parent_paths(tmp_path):
    path = tmp_path / "storage.tar"
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(BackupError, match="unsafe storage archive"):
        Ops._check_tar(path)


def test_backup_manifest_and_verification_detect_tampering(tmp_path):
    dump = tmp_path / "database.dump"
    archive = tmp_path / "storage.tar"
    dump.write_bytes(b"database")
    archive.write_bytes(b"storage")
    write_manifest(
        tmp_path,
        database=dump,
        storage=archive,
        revision="a" * 40,
        migrations=[migration(tmp_path)],
    )
    manifest = read_manifest(tmp_path)
    assert manifest["revision"] == "a" * 40
    assert manifest["database"]["bytes"] == 8
    verify_backup(tmp_path, check_archive=lambda path: None, check_dump=lambda path: None)
    archive.write_bytes(b"tampered")
    with pytest.raises(BackupError, match="checksum mismatch"):
        verify_backup(tmp_path, check_archive=lambda path: None, check_dump=lambda path: None)


def test_manifest_rejects_paths_outside_backup_directory(tmp_path):
    database = tmp_path / "database.dump"
    database.write_bytes(b"database")
    outside = tmp_path.parent / "outside-storage.tar"
    outside.write_bytes(b"storage")
    try:
        with pytest.raises(BackupError, match="inside"):
            write_manifest(
                tmp_path,
                database=database,
                storage=outside,
                revision="a" * 40,
                migrations=[],
            )
    finally:
        outside.unlink()


def test_manifest_is_valid_json(tmp_path):
    database = tmp_path / "database.dump"
    archive = tmp_path / "storage.tar"
    database.write_bytes(b"database")
    archive.write_bytes(b"storage")
    write_manifest(tmp_path, database, archive, "a" * 40, [])
    assert json.loads((tmp_path / "manifest.json").read_text())["schema_version"] == 1


def test_fresh_migration_snapshots_then_applies_one_transaction(tmp_path, monkeypatch):
    item = migration(tmp_path)
    ops = Ops()
    events = []
    monkeypatch.setattr(ops, "has_ledger", lambda: False)
    monkeypatch.setattr(ops, "has_core_schema", lambda: False)
    monkeypatch.setattr(ops, "app_running", lambda: False)
    monkeypatch.setattr(ops, "initialize_ledger", lambda: events.append("ledger"))
    monkeypatch.setattr(
        ops, "snapshot", lambda migrations: events.append("snapshot") or tmp_path
    )
    queries = iter(["", f"{item.version}|{item.checksum}", f"{item.version}|{item.checksum}"])

    def psql(sql):
        events.append(sql)
        return next(queries)

    monkeypatch.setattr(ops, "psql", psql)
    monkeypatch.setattr("deploy.server.ops.load_migrations", lambda directory: [item])
    ops.migrate()
    assert events[0] == "ledger"
    assert events[1].startswith("select version")
    assert events[2] == "snapshot"
    assert events[3] == build_migration_sql(item)


def test_snapshot_failure_blocks_all_migration_sql(tmp_path, monkeypatch):
    item = migration(tmp_path)
    ops = Ops()
    statements = []
    monkeypatch.setattr(ops, "has_ledger", lambda: True)
    monkeypatch.setattr(ops, "app_running", lambda: False)
    monkeypatch.setattr(ops, "psql", lambda sql: statements.append(sql) or "")
    monkeypatch.setattr(
        ops, "snapshot", lambda migrations: (_ for _ in ()).throw(BackupError("dump failed"))
    )
    monkeypatch.setattr("deploy.server.ops.load_migrations", lambda directory: [item])
    with pytest.raises(BackupError, match="dump failed"):
        ops.migrate()
    assert len(statements) == 1


def test_migration_quiesces_app_before_snapshot_and_restarts_after_success(tmp_path, monkeypatch):
    item = migration(tmp_path)
    ops = Ops()
    events = []
    monkeypatch.setattr(ops, "has_ledger", lambda: True)
    monkeypatch.setattr(ops, "app_running", lambda: True)
    monkeypatch.setattr(ops, "stop_app", lambda: events.append("stop"))
    monkeypatch.setattr(ops, "start_app", lambda: events.append("start"))
    monkeypatch.setattr(ops, "snapshot", lambda migrations: events.append("snapshot") or tmp_path)
    outputs = iter(["", "", f"{item.version}|{item.checksum}"])
    monkeypatch.setattr(ops, "psql", lambda sql: next(outputs))
    monkeypatch.setattr("deploy.server.ops.load_migrations", lambda directory: [item])
    ops.migrate()
    assert events == ["stop", "snapshot", "start"]


def test_migration_failure_keeps_application_stopped(tmp_path, monkeypatch):
    item = migration(tmp_path)
    ops = Ops()
    events = []
    monkeypatch.setattr(ops, "has_ledger", lambda: True)
    monkeypatch.setattr(ops, "app_running", lambda: True)
    monkeypatch.setattr(ops, "stop_app", lambda: events.append("stop"))
    monkeypatch.setattr(ops, "start_app", lambda: events.append("start"))
    monkeypatch.setattr(ops, "snapshot", lambda migrations: tmp_path)
    monkeypatch.setattr(
        ops,
        "psql",
        lambda sql: "" if sql.startswith("select version") else (_ for _ in ()).throw(
            MigrationError("DDL failed")
        ),
    )
    monkeypatch.setattr("deploy.server.ops.load_migrations", lambda directory: [item])
    with pytest.raises(MigrationError, match="DDL failed"):
        ops.migrate()
    assert events == ["stop"]


def test_existing_schema_without_ledger_requires_explicit_adoption(monkeypatch):
    ops = Ops()
    monkeypatch.setattr(ops, "has_ledger", lambda: False)
    monkeypatch.setattr(ops, "has_core_schema", lambda: True)
    with pytest.raises(MigrationError, match="adopt-existing"):
        ops.migrate()


def test_current_ledger_skips_snapshot(monkeypatch):
    item = Migration("20261125000000_test", Path("unused"), "a" * 64)
    ops = Ops()
    monkeypatch.setattr(ops, "has_ledger", lambda: True)
    monkeypatch.setattr(ops, "psql", lambda sql: f"{item.version}|{item.checksum}")
    monkeypatch.setattr(
        ops, "snapshot", lambda migrations: pytest.fail("current schema must not snapshot")
    )
    monkeypatch.setattr("deploy.server.ops.load_migrations", lambda directory: [item])
    ops.migrate()


def test_app_container_discovery_targets_each_service_separately(monkeypatch):
    ops = Ops()
    commands = []
    outputs = iter(["api-id", "worker-id", "agent-id", "runner-id"])
    monkeypatch.setattr(
        ops,
        "text",
        lambda command, **kwargs: commands.append(command) or next(outputs),
    )
    assert ops.app_containers() == "api-id\nworker-id\nagent-id\nrunner-id"
    assert len(commands) == 4
    for command, service in zip(commands, ("api", "worker", "agent", "runner"), strict=True):
        assert f"label=com.docker.compose.service={service}" in command
        assert command.count("--filter") == 2


def test_backup_restarts_application_when_dump_fails(tmp_path, monkeypatch):
    ops = Ops()
    events = []
    monkeypatch.setattr(ops, "app_running", lambda: True)
    monkeypatch.setattr(ops, "stop_app", lambda: events.append("stop"))
    monkeypatch.setattr(ops, "start_app", lambda: events.append("start"))
    monkeypatch.setattr(
        ops, "dump_database", lambda path: (_ for _ in ()).throw(BackupError("dump failed"))
    )
    monkeypatch.setattr("deploy.server.ops.load_migrations", lambda directory: [])
    monkeypatch.setattr(ops, "text", lambda command, **kwargs: "a" * 40)
    with pytest.raises(BackupError, match="dump failed"):
        ops.backup(tmp_path)
    assert events == ["stop", "start"]


def test_restore_requires_confirmation_before_touching_services(tmp_path, monkeypatch):
    ops = Ops()
    monkeypatch.setattr(
        ops, "app_running", lambda: pytest.fail("confirmation must be checked first")
    )
    with pytest.raises(BackupError, match="DESTROY_CURRENT_DATABASE"):
        ops.restore(tmp_path, storage=False, confirm="yes")


def test_restore_restarts_application_after_pg_restore_failure(tmp_path, monkeypatch):
    database = tmp_path / "database.dump"
    archive = tmp_path / "storage.tar"
    database.write_bytes(b"database")
    archive.write_bytes(b"storage")
    write_manifest(tmp_path, database, archive, "a" * 40, [])
    ops = Ops()
    events = []
    monkeypatch.setattr(ops, "app_running", lambda: True)
    monkeypatch.setattr(ops, "stop_app", lambda: events.append("stop"))
    monkeypatch.setattr(ops, "start_app", lambda: events.append("start"))
    monkeypatch.setattr(ops, "_check_dump", lambda path: None)
    monkeypatch.setattr(ops, "_check_tar", lambda path: None)
    monkeypatch.setattr(
        ops, "run", lambda command, **kwargs: (_ for _ in ()).throw(BackupError("restore failed"))
    )
    with pytest.raises(BackupError, match="restore failed"):
        ops.restore(tmp_path, storage=False, confirm="DESTROY_CURRENT_DATABASE")
    assert events == ["stop", "start"]


def test_repository_migrations_have_no_transaction_control():
    migrations = load_migrations(Path(__file__).resolve().parents[2] / "infra/supabase/migrations")
    assert len(migrations) == 11
    for item in migrations:
        assert build_migration_sql(item).startswith("BEGIN;")


def test_release_reuses_immutable_images_and_deploys_exact_sha(tmp_path, monkeypatch):
    revision = "a" * 40
    ops = Ops()
    commands = []
    monkeypatch.setattr(ops, "_require_clean_head", lambda value: revision)
    monkeypatch.setattr(ops, "_image_exists", lambda image: True)
    monkeypatch.setattr(ops, "run", lambda command, **kwargs: commands.append((command, kwargs)))
    monkeypatch.setattr("deploy.server.ops.RELEASE_ROOT", tmp_path)
    assert ops.release(revision) == revision
    deploy = commands[-1]
    assert deploy[0][-3:] == ["up", "-d", "--no-build"]
    assert deploy[1]["env"]["IMAGE_TAG"] == revision
    assert not any("build" in command for command, _ in commands)
    assert (tmp_path / f"{revision}.json").is_file()


def test_release_builds_missing_exec_image_with_exact_tag(tmp_path, monkeypatch):
    revision = "c" * 40
    ops = Ops()
    commands = []
    monkeypatch.setattr(ops, "_require_clean_head", lambda value: revision)
    built = []
    monkeypatch.setattr(
        ops,
        "_image_exists",
        lambda image: image in built or not image.startswith("reproagent-exec:"),
    )
    monkeypatch.setattr(
        ops,
        "run",
        lambda command, **kwargs: commands.append((command, kwargs))
        or built.append("reproagent-exec:" + revision),
    )
    monkeypatch.setattr("deploy.server.ops.RELEASE_ROOT", tmp_path)
    ops.release(revision)
    build = next(item for item in commands if item[0][0] == "bash")
    assert build[1]["env"]["EXEC_IMAGE"] == f"reproagent-exec:{revision}"
    assert "latest" not in build[1]["env"]["EXEC_IMAGE"]


def test_release_rejects_dirty_head_before_building(monkeypatch):
    revision = "d" * 40
    ops = Ops()
    outputs = iter([revision, revision, " M README.md"])
    monkeypatch.setattr(ops, "text", lambda command, **kwargs: next(outputs))
    monkeypatch.setattr(ops, "run", lambda *args, **kwargs: pytest.fail("must not build"))
    with pytest.raises(OpsError, match="clean working tree"):
        ops.release(revision)


def test_rollback_checks_receipt_and_every_image_before_deploy(tmp_path, monkeypatch):
    revision = "b" * 40
    ops = Ops()
    monkeypatch.setattr(ops, "_revision", lambda value: revision)
    monkeypatch.setattr("deploy.server.ops.RELEASE_ROOT", tmp_path)
    monkeypatch.setattr(ops, "run", lambda *args, **kwargs: pytest.fail("must not deploy"))
    with pytest.raises(OpsError, match="no release receipt"):
        ops.rollback(revision)
    (tmp_path / f"{revision}.json").write_text("{}")
    monkeypatch.setattr(ops, "_image_exists", lambda image: False)
    with pytest.raises(OpsError, match="image is missing"):
        ops.rollback(revision)


def test_operator_health_command_reports_dependencies_and_fails_on_degradation():
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    payloads = iter([
        {"service": "api", "status": "ok"},
        {"service": "api", "status": "degraded", "dependencies": []},
    ])

    class Opener:
        def open(self, request, timeout):
            assert timeout == 5
            return Response(json.dumps(next(payloads)).encode())

    with pytest.raises(OpsError, match="degraded"):
        check_service_health("http://127.0.0.1:8000", opener=Opener())


def test_operator_health_command_fails_when_readiness_is_unavailable():
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Opener:
        def open(self, request, timeout):
            return Response(json.dumps({"status": "unavailable"}).encode())

    with pytest.raises(OpsError, match="not ready"):
        check_service_health("http://127.0.0.1:8000", opener=Opener())
