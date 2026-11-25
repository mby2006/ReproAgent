"""Locked-down sibling-container execution via the mounted Docker socket.

Generated code NEVER runs in the Runner (or API/Agent) process. Each run spawns a
short-lived container from the baked exec image with: no network, all Linux
capabilities dropped, no-new-privileges + default seccomp, a non-root user, a
read-only rootfs, CPU/memory/PID/disk limits and a hard timeout. The container is
force-removed after the run; only the bind-mounted /output survives so metrics
and artifacts can be collected. No host credentials are ever passed in.
"""

import hashlib
import json
import shutil
import socket
import threading
import time
from pathlib import Path
from uuid import UUID

import docker
from docker.errors import APIError, ImageNotFound
from docker.types import LogConfig
from reproagent_schemas import (
    ErrorCategory,
    MetricsReport,
    RunAudit,
    RunRequest,
    RunResult,
    RunStatus,
    compute_code_hash,
)
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from .config import RunnerSettings


class SandboxError(RuntimeError):
    pass


def _write_code(request: RunRequest, run_dir: Path) -> tuple[Path, Path]:
    code_dir = run_dir / "code"
    output_dir = run_dir / "output"
    code_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o777)
    for code_file in request.code_files:
        target = code_dir / code_file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code_file.content, encoding="utf-8")
    return code_dir, output_dir


def _host_config(
    request: RunRequest, settings: RunnerSettings, code_dir: Path, output_dir: Path
) -> dict:
    budget = request.resource_budget
    config: dict = {
        "network_mode": "none",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "read_only": True,
        "user": settings.sandbox_user,
        "mem_limit": f"{budget.memory_mb}m",
        "memswap_limit": f"{budget.memory_mb}m",
        "nano_cpus": int(budget.cpu_count * 1_000_000_000),
        "pids_limit": budget.pids_limit,
        "tmpfs": {"/tmp": f"size={settings.tmpfs_size_mb}m,mode=1777"},
        # bound daemon-side json-file logs so a stdout flood cannot fill the host
        # disk (mem_limit does not cover the Docker logging driver)
        "log_config": LogConfig(
            type=LogConfig.types.JSON, config={"max-size": "1m", "max-file": "1"}
        ),
        "binds": [
            f"{code_dir}:/work/code:ro",
            f"{output_dir}:/output:rw",
        ],
        "working_dir": "/work/code",
        "auto_remove": False,
    }
    if settings.enforce_disk_quota:
        config["storage_opt"] = {"size": f"{budget.disk_mb}m"}
    return config


def _create_container(client, image: str, request: RunRequest, host_config: dict, labels: dict):
    command = ["python", request.entrypoint]
    kwargs = dict(host_config, labels=labels)
    volumes = {}
    for bind in kwargs.pop("binds", []):
        source, dest, mode = bind.rsplit(":", 2)
        volumes[source] = {"bind": dest, "mode": mode}
    try:
        return client.containers.create(
            image, command=command, detach=True, volumes=volumes, **kwargs
        )
    except APIError as exc:
        message = str(exc).lower()
        if "storage" in message or "quota" in message or "not supported" in message:
            kwargs.pop("storage_opt", None)
            return client.containers.create(
                image, command=command, detach=True, volumes=volumes, **kwargs
            )
        raise


def _dependency_lock_hash(request: RunRequest) -> str:
    joined = "\n".join(sorted(request.dependencies))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _image_audit(client, image: str, request: RunRequest) -> RunAudit:
    labels: dict = {}
    digest = None
    try:
        image_obj = client.images.get(image)
        digest = image_obj.id
        labels = image_obj.labels or {}
    except (ImageNotFound, APIError):
        pass
    return RunAudit(
        image_digest=digest,
        python_version=labels.get("reproagent.python_version"),
        dependency_lock_hash=_dependency_lock_hash(request),
        dataset_fingerprint=labels.get("reproagent.dataset_fingerprint"),
        seed=request.seed,
        code_hash=compute_code_hash(request.code_files),
        plan_version=request.plan_version,
        code_version=request.code_version,
    )


def _read_metrics(output_dir: Path, filename: str) -> tuple[dict, str | None]:
    path = output_dir / filename
    if not path.exists():
        return {}, "metrics file was not produced"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        report = MetricsReport.model_validate(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return {}, f"invalid metrics output: {exc}"
    return report.model_dump(), None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _dir_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


class Sandbox:
    def __init__(
        self, client, settings: RunnerSettings, host_work_dir: str | None = None,
        owner_id: str | None = None,
    ):
        self._client = client
        self._settings = settings
        self._host_work_dir = host_work_dir or str(settings.work_dir)
        self._owner_id = owner_id

    def execute(
        self,
        request: RunRequest,
        run_dir: Path,
        cancel_event: threading.Event,
        on_container=None,
    ) -> RunResult:
        run_id = UUID(run_dir.name)
        code_dir, output_dir = _write_code(request, run_dir)
        audit = _image_audit(self._client, self._settings.exec_image, request)
        host_run_dir = Path(self._host_work_dir) / run_dir.name
        host_config = _host_config(
            request, self._settings, host_run_dir / "code", host_run_dir / "output"
        )
        budget = request.resource_budget

        if cancel_event.is_set():
            return RunResult(
                run_id=run_id,
                status=RunStatus.CANCELLED,
                error="cancelled before the container started",
                audit=audit,
            )

        labels = {
            "reproagent.managed": "true",
            "reproagent.run_id": str(request.request_id),
            "reproagent.request_id": str(request.request_id),
        }
        if self._owner_id is not None:
            labels["reproagent.owner"] = self._owner_id
        try:
            container = _create_container(
                self._client, self._settings.exec_image, request, host_config, labels
            )
        except (ImageNotFound, APIError) as exc:
            return RunResult(
                run_id=run_id,
                status=RunStatus.FAILED,
                exit_code=None,
                error=f"sandbox container could not be created: {exc}",
                error_category=ErrorCategory.UNKNOWN_ERROR,
                audit=audit,
            )

        status = RunStatus.FAILED
        exit_code: int | None = None
        started = time.monotonic()
        disk_exceeded = False
        disk_limit = budget.disk_mb * 1024 * 1024
        try:
            if on_container is not None:
                on_container(container.id)
            container.start()
            deadline = started + budget.max_seconds
            # Poll instead of one blocking wait: storage_opt cannot quota the
            # bind-mounted /output, so its size is checked here, and cancel stays
            # responsive throughout the run.
            while True:
                if cancel_event.is_set():
                    status = RunStatus.CANCELLED
                    self._kill(container)
                    break
                if time.monotonic() >= deadline:
                    status = RunStatus.TIMEOUT
                    self._kill(container)
                    break
                if self._settings.enforce_disk_quota and _dir_size(output_dir) > disk_limit:
                    status = RunStatus.FAILED
                    disk_exceeded = True
                    self._kill(container)
                    break
                try:
                    wait_result = container.wait(timeout=2)
                except (ReadTimeout, RequestsConnectionError):
                    # /wait is streamed chunked by the daemon, so a read timeout
                    # can surface as ConnectionError instead of ReadTimeout.
                    continue
                except Exception:
                    status = RunStatus.CANCELLED if cancel_event.is_set() else RunStatus.TIMEOUT
                    self._kill(container)
                    break
                exit_code = int(wait_result.get("StatusCode", -1))
                if cancel_event.is_set():
                    status = RunStatus.CANCELLED
                elif exit_code == 0:
                    status = RunStatus.SUCCEEDED
                else:
                    status = RunStatus.FAILED
                break
            if disk_exceeded or status in (RunStatus.CANCELLED, RunStatus.TIMEOUT):
                try:
                    exit_code = int(container.wait(timeout=10).get("StatusCode", -1))
                except Exception:
                    exit_code = -1
        finally:
            execution_time = time.monotonic() - started
            stdout = self._logs(container, stdout=True)
            stderr = self._logs(container, stdout=False)
            self._remove(container)

        metrics: dict = {}
        error: str | None = None
        error_category: ErrorCategory | None = None
        if disk_exceeded:
            error = f"run exceeded disk quota of {budget.disk_mb}MB on /output and was killed"
            error_category = ErrorCategory.UNKNOWN_ERROR
        artifacts = sorted(p.name for p in output_dir.iterdir() if p.is_file())
        if status is RunStatus.SUCCEEDED:
            metrics, metric_error = _read_metrics(output_dir, request.metrics_filename)
            if metric_error is not None:
                status = RunStatus.FAILED
                error = metric_error
                error_category = ErrorCategory.METRIC_ERROR
        if status is RunStatus.TIMEOUT:
            error = f"run exceeded hard timeout of {budget.max_seconds}s and was killed"
            error_category = ErrorCategory.TIMEOUT
        elif status is RunStatus.FAILED and error is None:
            error = _truncate(stderr or stdout, 2000) or f"process exited with code {exit_code}"

        return RunResult(
            run_id=run_id,
            status=status,
            exit_code=exit_code,
            stdout=_truncate(stdout, self._settings.max_output_chars),
            stderr=_truncate(stderr, self._settings.max_output_chars),
            execution_time=execution_time,
            metrics=metrics,
            artifacts=artifacts,
            error=error,
            error_category=error_category,
            audit=audit,
        )

    def _kill(self, container) -> None:
        try:
            container.kill()
        except APIError:
            pass

    def _remove(self, container) -> None:
        try:
            container.remove(force=True)
        except APIError:
            pass

    def _logs(self, container, *, stdout: bool) -> str:
        try:
            raw = container.logs(stdout=stdout, stderr=not stdout, timestamps=False)
        except APIError:
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)


def resolve_host_work_dir(client, work_dir: Path) -> str:
    """Sibling-container binds need a daemon-visible host path.

    When the Runner itself runs in a container, its work_dir is a bind mount; we
    inspect our own mounts to recover the host Source so spawned containers can
    bind the same files. On a bare-host Runner the local path already aligns.
    """
    try:
        me = client.containers.get(socket.gethostname())
        for mount in me.attrs.get("Mounts", []):
            if mount.get("Destination") == str(work_dir):
                source = mount.get("Source")
                if source:
                    return source
    except Exception:
        pass
    return str(work_dir)


def make_client(settings: RunnerSettings):
    if settings.docker_host:
        return docker.DockerClient(base_url=settings.docker_host)
    return docker.from_env()


def cleanup_run_dir(run_dir: Path) -> None:
    shutil.rmtree(run_dir, ignore_errors=True)