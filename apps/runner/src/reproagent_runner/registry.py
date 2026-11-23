"""In-memory registry of asynchronous runs.

POST /runs schedules a background task that drives the sandbox in a thread pool
(so the hard timeout never blocks the event loop). The registry tracks status,
holds the live container id for prompt cancellation, and exposes the collected
RunResult and artifacts. State is per-process and ephemeral by design.
"""

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from reproagent_schemas import (
    ErrorCategory,
    ExecImageStatus,
    RunAccepted,
    RunLogs,
    RunRequest,
    RunResult,
    RunStatus,
)

from .config import RunnerSettings
from .sandbox import Sandbox, _truncate, cleanup_run_dir, make_client, resolve_host_work_dir


def _decode(raw) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


@dataclass
class RunRecord:
    request: RunRequest
    run_id: UUID
    run_dir: Path
    status: RunStatus = RunStatus.QUEUED
    result: RunResult | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    container_id: str | None = None
    task: asyncio.Task | None = None
    finished_at: float | None = None


class RunRegistry:
    def __init__(self, settings: RunnerSettings, client=None):
        self._settings = settings
        self._client = client if client is not None else make_client(settings)
        host_work_dir = resolve_host_work_dir(self._client, settings.work_dir)
        self._sandbox = Sandbox(self._client, settings, host_work_dir=host_work_dir)
        self._runs: dict[UUID, RunRecord] = {}
        settings.work_dir.mkdir(parents=True, exist_ok=True)

    def start(self, request: RunRequest) -> RunAccepted:
        active = sum(
            1 for r in self._runs.values() if r.status in (RunStatus.QUEUED, RunStatus.RUNNING)
        )
        if active >= self._settings.max_concurrent_runs:
            raise OverflowError("runner concurrency limit reached")
        run_id = uuid4()
        record = RunRecord(
            request=request, run_id=run_id, run_dir=self._settings.work_dir / str(run_id)
        )
        self._runs[run_id] = record
        record.task = asyncio.create_task(self._execute(record))
        return RunAccepted(run_id=run_id, status=RunStatus.QUEUED)

    async def _execute(self, record: RunRecord) -> None:
        record.status = RunStatus.RUNNING
        loop = asyncio.get_running_loop()

        def on_container(container_id: str) -> None:
            record.container_id = container_id

        try:
            result = await loop.run_in_executor(
                None,
                self._sandbox.execute,
                record.request,
                record.run_dir,
                record.cancel_event,
                on_container,
            )
        except Exception as exc:  # sandbox infrastructure failure
            result = RunResult(
                run_id=record.run_id,
                status=RunStatus.FAILED,
                error=f"sandbox execution failed: {exc}",
                error_category=ErrorCategory.UNKNOWN_ERROR,
            )
        record.result = result
        record.status = result.status
        record.finished_at = time.monotonic()

    def reap_expired(self) -> None:
        """Evict terminal records past their TTL and remove their work dirs.

        Results/artifacts are fetched promptly by the API after a run finishes;
        the TTL only bounds how long finished state lingers in memory and disk.
        """
        now = time.monotonic()
        ttl = self._settings.run_ttl_seconds
        for run_id, record in list(self._runs.items()):
            if record.finished_at is None or now - record.finished_at < ttl:
                continue
            cleanup_run_dir(record.run_dir)
            del self._runs[run_id]

    async def reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.reap_interval_seconds)
            self.reap_expired()

    def _record(self, run_id: UUID) -> RunRecord:
        record = self._runs.get(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    def get(self, run_id: UUID) -> RunResult:
        record = self._record(run_id)
        if record.result is not None:
            return record.result
        return RunResult(run_id=run_id, status=record.status)

    def live_logs(self, run_id: UUID) -> RunLogs:
        """Logs so far: from the live container while running, else the result."""
        record = self._record(run_id)
        if record.result is not None:
            return RunLogs(stdout=record.result.stdout, stderr=record.result.stderr)
        if record.container_id is not None:
            try:
                container = self._client.containers.get(record.container_id)
                limit = self._settings.max_output_chars
                return RunLogs(
                    stdout=_truncate(_decode(container.logs(stdout=True, stderr=False)), limit),
                    stderr=_truncate(_decode(container.logs(stdout=False, stderr=True)), limit),
                )
            except Exception:
                # container already removed or daemon unreachable: nothing live to show
                pass
        return RunLogs()

    def cancel(self, run_id: UUID) -> RunResult:
        record = self._record(run_id)
        record.cancel_event.set()
        if record.container_id is not None:
            try:
                self._client.containers.get(record.container_id).kill()
            except Exception:
                pass
        return self.get(run_id)

    def artifacts(self, run_id: UUID) -> list[str]:
        record = self._record(run_id)
        output_dir = record.run_dir / "output"
        if not output_dir.exists():
            return []
        return sorted(p.name for p in output_dir.iterdir() if p.is_file())

    def artifact_path(self, run_id: UUID, name: str) -> Path:
        record = self._record(run_id)
        if Path(name).name != name or name in ("", ".", ".."):
            raise KeyError(name)
        path = record.run_dir / "output" / name
        if not path.is_file():
            raise KeyError(name)
        return path

    def image_status(self) -> ExecImageStatus:
        """Report the pinned exec image so a rerun can refuse to start blind."""
        image = self._settings.exec_image
        try:
            obj = self._client.images.get(image)
            labels = obj.labels or {}
            return ExecImageStatus(
                image=image,
                available=True,
                digest=obj.id,
                dataset_fingerprint=labels.get("reproagent.dataset_fingerprint"),
                built_at=labels.get("reproagent.built_at"),
            )
        except Exception as exc:  # daemon down or image removed
            return ExecImageStatus(
                image=image, available=False, error=_truncate(str(exc), 300)
            )