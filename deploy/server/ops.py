#!/usr/bin/env python3
"""Checked production migrations, backups, restores, and image rollbacks."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
UPSTREAM = Path(os.environ.get("UPSTREAM_DOCKER_DIR", "/opt/supabase-src/docker"))
ENV_FILE = Path(os.environ.get("REPROAGENT_ENV_FILE", HERE / ".env.prod"))
DATA_ROOT = Path(os.environ.get("REPROAGENT_DATA_ROOT", "/var/lib/reproagent"))
STORAGE_ROOT = DATA_ROOT / "supabase" / "storage"
BACKUP_ROOT = DATA_ROOT / "backups"
RELEASE_ROOT = DATA_ROOT / "releases"
MIGRATION_LOCK = 734_611_250_001
IMAGES = {
    "api": "reproagent-api",
    "worker": "reproagent-api",
    "agent": "reproagent-agent",
    "runner": "reproagent-runner",
    "exec": "reproagent-exec",
}
TRANSACTION_CONTROL = re.compile(
    r"^\s*(?:BEGIN|COMMIT|END|ROLLBACK)\b[^;]*;", re.IGNORECASE | re.MULTILINE
)
NON_TRANSACTIONAL = re.compile(
    r"^\s*(?:CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY|VACUUM|"
    r"ALTER\s+TYPE\s+[^;]+\s+ADD\s+VALUE)\b",
    re.IGNORECASE | re.MULTILINE,
)
DOLLAR_QUOTED = re.compile(r"\$\$.*?\$\$", re.DOTALL)
TAGGED_DOLLAR_QUOTED = re.compile(
    r"\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\$.*?\$(?P=tag)\$", re.DOTALL
)


class OpsError(RuntimeError):
    pass


class MigrationError(OpsError):
    pass


class BackupError(OpsError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum: str


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_migrations(directory: Path) -> list[Migration]:
    migrations = []
    seen = set()
    for path in sorted(directory.glob("*.sql")):
        version = path.stem
        if not re.fullmatch(r"\d{14}_[a-z0-9_]+", version):
            raise MigrationError(f"invalid migration filename: {path.name}")
        if version in seen:
            raise MigrationError(f"duplicate migration version: {version}")
        seen.add(version)
        migrations.append(Migration(version, path, sha256(path)))
    if not migrations:
        raise MigrationError(f"no SQL migrations found in {directory}")
    return migrations


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ledger_ddl() -> str:
    return """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version text PRIMARY KEY,
    checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now(),
    applied_by text NOT NULL DEFAULT current_user
);
""".strip()


def build_migration_sql(item: Migration) -> str:
    sql = item.path.read_text(encoding="utf-8")
    executable_sql = DOLLAR_QUOTED.sub("", TAGGED_DOLLAR_QUOTED.sub("", sql))
    if TRANSACTION_CONTROL.search(executable_sql):
        raise MigrationError(f"{item.path.name}: migration must not contain transaction control")
    if NON_TRANSACTIONAL.search(executable_sql):
        raise MigrationError(f"{item.path.name}: statement cannot run inside a transaction")
    if item.path.suffix != ".sql":
        raise MigrationError(f"unsupported migration type: {item.path.name}")
    delimiter = "reproagent_migration"
    while f"${delimiter}$" in sql:
        delimiter += "_"
    return (
        "BEGIN;\n"
        "SET LOCAL lock_timeout = '30s';\n"
        f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK});\n"
        f"DO ${delimiter}$\n"
        "BEGIN\n"
        f"  IF EXISTS (SELECT 1 FROM public.schema_migrations "
        f"WHERE version = {sql_literal(item.version)} "
        f"AND checksum <> {sql_literal(item.checksum)}) THEN\n"
        f"    RAISE EXCEPTION 'checksum mismatch for migration {item.version}';\n"
        "  END IF;\n"
        f"  IF NOT EXISTS (SELECT 1 FROM public.schema_migrations "
        f"WHERE version = {sql_literal(item.version)}) THEN\n"
        f"    EXECUTE ${delimiter}_sql$\n{sql.rstrip()}\n"
        f"${delimiter}_sql$;\n"
        "  END IF;\n"
        f"END ${delimiter}$;\n"
        "INSERT INTO public.schema_migrations (version, checksum)\n"
        f"VALUES ({sql_literal(item.version)}, {sql_literal(item.checksum)})\n"
        "ON CONFLICT (version) DO NOTHING;\n"
        "COMMIT;\n"
    )


def parse_ledger(output: str) -> dict[str, str]:
    ledger = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[1]):
            raise MigrationError(f"malformed migration ledger row: {line!r}")
        ledger[parts[0]] = parts[1]
    return ledger


def pending_migrations(
    migrations: list[Migration], ledger: dict[str, str]
) -> list[Migration]:
    unknown = set(ledger) - {item.version for item in migrations}
    if unknown:
        raise MigrationError(f"ledger references missing migrations: {sorted(unknown)}")
    pending = []
    for item in migrations:
        applied = ledger.get(item.version)
        if applied is None:
            pending.append(item)
        elif applied != item.checksum:
            raise MigrationError(f"checksum mismatch for applied migration {item.version}")
    return pending


def _file_record(path: Path, root: Path) -> dict[str, object]:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or path.is_symlink():
        raise BackupError(f"backup file must be a regular file inside {root}: {path}")
    return {
        "name": resolved.relative_to(root.resolve()).as_posix(),
        "bytes": resolved.stat().st_size,
        "sha256": sha256(resolved),
    }


def write_manifest(
    directory: Path,
    database: Path,
    storage: Path,
    revision: str,
    migrations: list[Migration],
) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "revision": revision,
        "database": _file_record(database, directory),
        "storage": _file_record(storage, directory),
        "migrations": [
            {"version": item.version, "sha256": item.checksum} for item in migrations
        ],
    }
    target = directory / "manifest.json"
    temporary = directory / ".manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    return manifest


def read_manifest(directory: Path) -> dict[str, object]:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"cannot read backup manifest: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise BackupError("unsupported backup manifest version")
    for key in ("database", "storage"):
        record = manifest.get(key)
        if not isinstance(record, dict) or not re.fullmatch(
            r"[0-9a-f]{64}", str(record.get("sha256", ""))
        ):
            raise BackupError(f"invalid {key} manifest record")
    return manifest


def verify_backup(directory: Path, *, check_archive, check_dump) -> dict[str, object]:
    manifest = read_manifest(directory)
    for key, checker in (("database", check_dump), ("storage", check_archive)):
        record = manifest[key]
        relative = Path(record["name"])
        if relative.is_absolute() or ".." in relative.parts:
            raise BackupError("manifest contains an unsafe path")
        path = directory / relative
        expected = record["sha256"]
        if not path.is_file() or path.is_symlink():
            raise BackupError(f"missing backup file: {path}")
        if path.stat().st_size != record["bytes"] or sha256(path) != expected:
            raise BackupError(f"checksum mismatch: {path}")
        try:
            checker(path)
        except Exception as exc:
            raise BackupError(f"cannot read {key} backup {path}: {exc}") from exc
    return manifest


def check_service_health(base_url: str, *, opener=None) -> dict[str, object]:
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results = {}
    for path in ("/ready", "/health/dependencies"):
        try:
            with opener.open(base_url.rstrip("/") + path, timeout=5) as response:
                results[path] = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                results[path] = json.load(exc)
            except ValueError:
                results[path] = {"status": "unavailable", "detail": f"HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise OpsError(f"health probe {path} failed: {type(exc).__name__}") from exc
    if results["/ready"].get("status") != "ok":
        raise OpsError("API is not ready")
    if results["/health/dependencies"].get("status") != "ok":
        raise OpsError("API dependencies are degraded")
    return results


class Ops:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run
        self.sb = self._compose("supabase", slim=True)
        self.app = self._compose("reproagent-prod")

    @staticmethod
    def _compose(project: str, *, slim: bool = False) -> list[str]:
        command = [
            "docker", "compose", "-p", project, "--env-file", str(ENV_FILE),
        ]
        if slim:
            command += [
                "-f", str(UPSTREAM / "docker-compose.yml"),
                "-f", str(UPSTREAM / "docker-compose.kong.yml"),
                "-f", str(HERE / "docker-compose.slim.yml"),
            ]
        else:
            command += ["-f", str(HERE / "docker-compose.prod.yml")]
        return command

    def run(self, command, **kwargs):
        kwargs.setdefault("check", True)
        return self.runner(command, **kwargs)

    def text(self, command, **kwargs) -> str:
        kwargs.setdefault("text", True)
        kwargs.setdefault("capture_output", True)
        return self.run(command, **kwargs).stdout.strip()

    def psql(self, sql: str) -> str:
        return self.text([
            *self.sb, "exec", "-T", "db", "psql", "-X", "-U", "postgres",
            "-v", "ON_ERROR_STOP=1", "-tA", "-c", sql,
        ])

    def scalar(self, sql: str) -> str:
        output = self.psql(sql)
        if "\n" in output:
            raise MigrationError(f"expected one database value, got: {output!r}")
        return output

    def has_ledger(self) -> bool:
        return self.scalar("select to_regclass('public.schema_migrations') is not null;") == "t"

    def has_core_schema(self) -> bool:
        return self.scalar("select to_regclass('public.papers') is not null;") == "t"

    def dump_database(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as output:
            self.run([
                *self.sb, "exec", "-T", "db", "pg_dump", "-U", "postgres",
                "--format=custom", "postgres",
            ], stdout=output)
        path.chmod(0o600)
        with path.open("rb") as source:
            self.run([
                *self.sb, "exec", "-T", "db", "pg_restore", "--list",
            ], stdin=source)

    def snapshot(self, migrations: list[Migration], directory: Path | None = None) -> Path:
        revision = self.text(["git", "-C", str(REPO), "rev-parse", "HEAD"])
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = directory or BACKUP_ROOT / "pre-migration" / f"{stamp}-{revision[:12]}"
        dump = target / "database.dump"
        storage = target / "storage.tar"
        temporary_dump = target / ".database.dump.tmp"
        temporary_storage = target / ".storage.tar.tmp"
        target.mkdir(parents=True, exist_ok=True)
        try:
            self.dump_database(temporary_dump)
            with temporary_storage.open("wb") as output:
                self.run([
                    "tar", "--numeric-owner", "-cf", "-", "-C", str(STORAGE_ROOT), ".",
                ], stdout=output)
            temporary_dump.chmod(0o600)
            temporary_storage.chmod(0o600)
            temporary_dump.replace(dump)
            temporary_storage.replace(storage)
            write_manifest(target, dump, storage, revision, migrations)
            verify_backup(
                target,
                check_dump=lambda path: self._check_dump(path),
                check_archive=lambda path: self._check_tar(path),
            )
        except Exception:
            for path in (temporary_dump, temporary_storage):
                path.unlink(missing_ok=True)
            raise
        return target

    def _check_dump(self, path: Path) -> None:
        with path.open("rb") as source:
            self.run([*self.sb, "exec", "-T", "db", "pg_restore", "--list"], stdin=source)

    @staticmethod
    def _check_tar(path: Path) -> None:
        with tarfile.open(path, "r:") as archive:
            for member in archive:
                parts = Path(member.name).parts
                if (
                    member.issym()
                    or member.islnk()
                    or Path(member.name).is_absolute()
                    or ".." in parts
                ):
                    raise BackupError(f"unsafe storage archive member: {member.name}")

    def initialize_ledger(self) -> None:
        self.psql(
            f"BEGIN;\nSELECT pg_advisory_xact_lock({MIGRATION_LOCK});\n"
            f"{ledger_ddl()}\nCOMMIT;"
        )

    def adopt_existing(self, migrations: list[Migration]) -> None:
        if self.has_ledger():
            raise MigrationError("migration ledger already exists; adoption is not allowed")
        if not self.has_core_schema():
            raise MigrationError("adoption requires an existing public.papers table")
        values = ",\n".join(
            f"({sql_literal(item.version)}, {sql_literal(item.checksum)})" for item in migrations
        )
        self.psql(
            f"BEGIN;\nSELECT pg_advisory_xact_lock({MIGRATION_LOCK});\n{ledger_ddl()}\n"
            f"INSERT INTO public.schema_migrations (version, checksum) VALUES\n{values}\n"
            "ON CONFLICT (version) DO NOTHING;\nCOMMIT;"
        )

    def migrate(self, *, adopt_existing: bool = False) -> None:
        migrations = load_migrations(REPO / "infra" / "supabase" / "migrations")
        if adopt_existing:
            self.adopt_existing(migrations)
            print("Adopted existing schema; historical SQL content was not verified.")
            return
        if not self.has_ledger():
            if self.has_core_schema():
                raise MigrationError(
                    "existing schema has no ledger; verify a backup, then run "
                    "migrate --adopt-existing"
                )
            self.initialize_ledger()
        ledger_sql = "select version || '|' || checksum from public.schema_migrations;"
        ledger = parse_ledger(self.psql(ledger_sql))
        pending = pending_migrations(migrations, ledger)
        if not pending:
            print("Database schema is current; no snapshot or migration needed.")
            return
        running = self.app_running()
        if running:
            self.stop_app()
        try:
            snapshot = self.snapshot(migrations)
            print(f"Verified pre-migration snapshot: {snapshot}")
            for item in pending:
                print(f"Applying {item.path.name}")
                self.psql(build_migration_sql(item))
            after = parse_ledger(self.psql(ledger_sql))
            if pending_migrations(migrations, after):
                raise MigrationError("migration ledger verification failed")
        except Exception:
            if running:
                print(
                    "Migration failed; application remains stopped for inspection.",
                    file=sys.stderr,
                )
            raise
        if running:
            self.start_app()
        print(
            f"Applied {len(pending)} migration(s). "
            "Forward-fix is preferred to database rollback."
        )

    def app_containers(self, *, include_stopped: bool = False) -> str:
        containers = []
        for service in ("api", "worker", "agent", "runner"):
            command = [
                "docker", "ps", "-aq" if include_stopped else "-q",
                "--filter", "label=com.docker.compose.project=reproagent-prod",
                "--filter", f"label=com.docker.compose.service={service}",
            ]
            containers.extend(self.text(command).splitlines())
        return "\n".join(dict.fromkeys(containers))

    def app_running(self) -> bool:
        return bool(self.app_containers())

    def stop_app(self) -> None:
        containers = self.app_containers()
        if containers:
            self.run(["docker", "stop", *containers.splitlines()])

    def start_app(self) -> None:
        containers = self.app_containers(include_stopped=True)
        if containers:
            self.run(["docker", "start", *containers.splitlines()])

    def backup(self, directory: Path | None = None) -> Path:
        revision = self.text(["git", "-C", str(REPO), "rev-parse", "HEAD"])
        migrations = load_migrations(REPO / "infra" / "supabase" / "migrations")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = directory or BACKUP_ROOT / stamp
        target.mkdir(parents=True, exist_ok=True)
        dump = target / "database.dump"
        storage = target / "storage.tar"
        temporary_dump = target / ".database.dump.tmp"
        temporary_storage = target / ".storage.tar.tmp"
        running = self.app_running()
        try:
            if running:
                self.stop_app()
            self.dump_database(temporary_dump)
            with temporary_storage.open("wb") as output:
                self.run([
                    "tar", "--numeric-owner", "-cf", "-", "-C", str(STORAGE_ROOT), ".",
                ], stdout=output)
            temporary_dump.chmod(0o600)
            temporary_storage.chmod(0o600)
            temporary_dump.replace(dump)
            temporary_storage.replace(storage)
            write_manifest(target, dump, storage, revision, migrations)
            verify_backup(
                target,
                check_dump=lambda path: self._check_dump(path),
                check_archive=lambda path: self._check_tar(path),
            )
        finally:
            for path in (temporary_dump, temporary_storage):
                path.unlink(missing_ok=True)
            if running:
                self.start_app()
        print(f"Verified backup: {target}")
        print("Copy this directory to storage outside this server and rehearse restore.")
        return target

    def restore(self, directory: Path, *, storage: bool, confirm: str) -> None:
        if confirm != "DESTROY_CURRENT_DATABASE":
            raise BackupError("restore requires --confirm DESTROY_CURRENT_DATABASE")
        verify_backup(
            directory,
            check_dump=lambda path: self._check_dump(path),
            check_archive=lambda path: self._check_tar(path),
        )
        manifest = read_manifest(directory)
        dump = directory / manifest["database"]["name"]
        running = self.app_running()
        try:
            if running:
                self.stop_app()
            with dump.open("rb") as source:
                self.run([
                    *self.sb, "exec", "-T", "db", "pg_restore", "-U", "postgres",
                    "--clean", "--if-exists", "--no-owner", "--no-privileges",
                    "--exit-on-error", "-d", "postgres",
                ], stdin=source)
            if storage:
                previous = self._restore_storage(directory / manifest["storage"]["name"])
                print(f"Previous Storage files retained at: {previous}")
        finally:
            if running:
                self.start_app()
        print("Database restored. Check schema_migrations before starting a newer application.")

    def _restore_storage(self, archive_path: Path) -> None:
        if not STORAGE_ROOT.is_dir():
            raise BackupError(f"storage directory is missing: {STORAGE_ROOT}")
        previous = STORAGE_ROOT.with_name(
            f"storage.before-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        STORAGE_ROOT.rename(previous)
        try:
            STORAGE_ROOT.mkdir()
            with tarfile.open(archive_path, "r:") as archive:
                archive.extractall(STORAGE_ROOT, filter="data")
        except Exception:
            shutil.rmtree(STORAGE_ROOT)
            previous.rename(STORAGE_ROOT)
            raise
        return previous

    def verify(self, directory: Path) -> None:
        verify_backup(
            directory,
            check_dump=lambda path: self._check_dump(path),
            check_archive=lambda path: self._check_tar(path),
        )
        print(f"Backup checksums and archive structure are valid: {directory}")

    def health(self) -> None:
        port = os.environ.get("API_PORT", "8000")
        result = check_service_health(f"http://127.0.0.1:{port}")
        print(json.dumps(result, indent=2))

    def _revision(self, revision: str) -> str:
        resolved = self.text(["git", "-C", str(REPO), "rev-parse", f"{revision}^{{commit}}"])
        if not re.fullmatch(r"[0-9a-f]{40}", resolved):
            raise OpsError("release revision must resolve to a full commit SHA")
        return resolved

    def _require_clean_head(self, revision: str) -> str:
        resolved = self._revision(revision)
        head = self.text(["git", "-C", str(REPO), "rev-parse", "HEAD"])
        dirty = self.text(["git", "-C", str(REPO), "status", "--porcelain"])
        if resolved != head:
            raise OpsError("release must target the current HEAD commit")
        if dirty:
            raise OpsError("release requires a clean working tree")
        return resolved

    def _image(self, name: str, revision: str) -> str:
        return f"{IMAGES[name]}:{revision}"

    def _image_exists(self, image: str) -> bool:
        result = self.run(["docker", "image", "inspect", image], check=False, capture_output=True)
        return result.returncode == 0

    def release(self, revision: str) -> str:
        resolved = self._require_clean_head(revision)
        env = {**os.environ, "IMAGE_TAG": resolved}
        for service in ("api", "agent", "runner"):
            image = self._image(service, resolved)
            if not self._image_exists(image):
                self.run([*self.app, "build", service], env=env)
        exec_image = self._image("exec", resolved)
        if not self._image_exists(exec_image):
            self.run(
                ["bash", str(REPO / "scripts" / "build_exec_image.sh")],
                env={**os.environ, "EXEC_IMAGE": exec_image},
            )
        for image in {self._image(name, resolved) for name in IMAGES}:
            if not self._image_exists(image):
                raise OpsError(f"release image missing after build: {image}")
        RELEASE_ROOT.mkdir(parents=True, exist_ok=True)
        receipt = RELEASE_ROOT / f"{resolved}.json"
        receipt.write_text(
            json.dumps({"revision": resolved, "images": sorted(
                self._image(name, resolved) for name in IMAGES
            )}, indent=2) + "\n",
            encoding="utf-8",
        )
        self.run([*self.app, "up", "-d", "--no-build"], env=env)
        self._write_current(resolved)
        print(f"Released and deployed {resolved}. Previous image tags were retained.")
        return resolved

    def rollback(self, revision: str) -> str:
        resolved = self._revision(revision)
        receipt = RELEASE_ROOT / f"{resolved}.json"
        if not receipt.is_file():
            raise OpsError(f"no release receipt for {resolved}")
        for name in IMAGES:
            image = self._image(name, resolved)
            if not self._image_exists(image):
                raise OpsError(f"cannot roll back; image is missing: {image}")
        self.run(
            [*self.app, "up", "-d", "--no-build"],
            env={**os.environ, "IMAGE_TAG": resolved},
        )
        self._write_current(resolved)
        print(f"Application rolled back to {resolved}; database was not changed.")
        return resolved

    @staticmethod
    def _write_current(revision: str) -> None:
        RELEASE_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = RELEASE_ROOT / ".current.tmp"
        temporary.write_text(revision + "\n", encoding="utf-8")
        temporary.replace(RELEASE_ROOT / "current")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--adopt-existing", action="store_true")
    backup = sub.add_parser("backup")
    backup.add_argument("directory", nargs="?", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("directory", type=Path)
    sub.add_parser("health", help="check cached readiness and live dependencies")
    restore = sub.add_parser("restore")
    restore.add_argument("directory", type=Path)
    restore.add_argument("--storage", action="store_true")
    restore.add_argument("--confirm", required=True)
    release = sub.add_parser("release")
    release.add_argument("revision")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("revision")
    args = parser.parse_args(argv)
    ops = Ops()
    try:
        if args.command == "migrate":
            ops.migrate(adopt_existing=args.adopt_existing)
        elif args.command == "backup":
            ops.backup(args.directory)
        elif args.command == "verify":
            ops.verify(args.directory)
        elif args.command == "health":
            ops.health()
        elif args.command == "restore":
            ops.restore(args.directory, storage=args.storage, confirm=args.confirm)
        elif args.command == "release":
            ops.release(args.revision)
        elif args.command == "rollback":
            ops.rollback(args.revision)
    except (OpsError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
