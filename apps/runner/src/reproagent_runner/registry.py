"""Durable registry of asynchronous sandbox runs.

POST /runs schedules a background task that drives the sandbox in a thread pool.
Each accepted request and lifecycle transition is atomically journaled under
``work_dir``. Startup recovers completed results and fails unknown in-flight
outcomes instead of silently executing the same training twice.
"""

import asyncio
import json
import logging
import math
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
    RunTimings,
    request_fingerprint,
)

from .config import RunnerSettings
from .journal import atomic_write_json, atomic_write_text
from .sandbox import Sandbox, _truncate, cleanup_run_dir, make_client, resolve_host_work_dir

logger = logging.getLogger(__name__)


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
    accepted_at: float | None = None
    fingerprint: str = ""
    cancel_requested: bool = False


TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.TIMEOUT,
    RunStatus.CANCELLED,
}


class RequestConflictError(ValueError):
    """The idempotency identity was already used for different execution content."""


class RunRegistry:
    def __init__(self, settings: RunnerSettings, client=None):
        self._settings = settings
        self._client = client if client is not None else make_client(settings)
        settings.work_dir.mkdir(parents=True, exist_ok=True)
        self._journal_dir = settings.work_dir / ".journal"
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        self._owner_id = self._load_or_create_owner()
        host_work_dir = resolve_host_work_dir(self._client, settings.work_dir)
        self._sandbox = Sandbox(
            self._client, settings, host_work_dir=host_work_dir, owner_id=self._owner_id
        )
        self._runs: dict[UUID, RunRecord] = {}
        self._recover_journal()
        self._remove_owned_orphans()

    def _load_or_create_owner(self) -> str:
        path = self._settings.work_dir / "runner-owner"
        try:
            owner = path.read_text(encoding="utf-8").strip()
            if owner:
                return owner
        except OSError:
            pass
        owner = str(uuid4())
        atomic_write_text(path, owner)
        return owner

    def _journal_path(self, run_id: UUID) -> Path:
        return self._journal_dir / f"{run_id}.json"

    def _write_record(self, record: RunRecord) -> None:
        atomic_write_json(self._journal_path(record.run_id), {
            "schema_version": 1,
            "owner_id": self._owner_id,
            "request_id": str(record.request.request_id),
            "run_id": str(record.run_id),
            "fingerprint": record.fingerprint,
            "request": record.request.model_dump(mode="json"),
            "status": record.status.value,
            "result": record.result.model_dump(mode="json") if record.result else None,
            "container_id": record.container_id,
            "finished_at": record.finished_at,
            "accepted_at": record.accepted_at,
            "cancel_requested": record.cancel_requested,
        })

    def _record_from_payload(self, payload) -> RunRecord:
        if not isinstance(payload, dict):
            raise ValueError("journal record is not an object")
        if payload.get("schema_version") != 1 or payload.get("owner_id") != self._owner_id:
            raise ValueError("unsupported journal or owner")
        request = RunRequest.model_validate(payload["request"])
        run_id = UUID(payload["run_id"])
        if run_id != request.request_id or UUID(payload["request_id"]) != run_id:
            raise ValueError("journal identity mismatch")
        fingerprint = request_fingerprint(request)
        if fingerprint != payload["fingerprint"]:
            raise ValueError("journal fingerprint mismatch")
        result = (
            RunResult.model_validate(payload["result"])
            if payload.get("result") is not None
            else None
        )
        if result is not None and result.run_id != run_id:
            raise ValueError("journal result identity mismatch")
        status = RunStatus(payload["status"])
        finished_at = payload.get("finished_at")
        if finished_at is not None and (
            isinstance(finished_at, bool)
            or not isinstance(finished_at, (int, float))
            or not math.isfinite(finished_at)
        ):
            raise ValueError("journal finished_at is not a finite timestamp")
        accepted_at = payload.get("accepted_at")
        if accepted_at is not None and (
            isinstance(accepted_at, bool)
            or not isinstance(accepted_at, (int, float))
            or not math.isfinite(accepted_at)
        ):
            raise ValueError("journal accepted_at is not a finite timestamp")
        container_id = payload.get("container_id")
        if container_id is not None and (not isinstance(container_id, str) or not container_id):
            raise ValueError("journal container_id is invalid")
        cancel_requested = payload.get("cancel_requested", status is RunStatus.CANCELLED)
        if not isinstance(cancel_requested, bool):
            raise ValueError("journal cancel_requested is not boolean")
        if status in TERMINAL_STATUSES:
            if result is None or result.status != status or finished_at is None:
                raise ValueError("terminal journal status/result/finished_at mismatch")
            if status is not RunStatus.CANCELLED and container_id is not None:
                raise ValueError("completed journal still references a container")
        elif result is not None or finished_at is not None or cancel_requested:
            raise ValueError("nonterminal journal contains terminal state")
        return RunRecord(
            request=request,
            run_id=run_id,
            run_dir=self._settings.work_dir / str(run_id),
            status=status,
            result=result,
            container_id=container_id,
            finished_at=finished_at,
            accepted_at=accepted_at,
            fingerprint=fingerprint,
            cancel_requested=cancel_requested,
        )

    def _recover_journal(self) -> None:
        for path in sorted(self._journal_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = self._record_from_payload(payload)
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
                logger.warning("ignoring corrupt run journal %s: %s", path.name, exc)
                continue

            try:
                if record.finished_at is not None and (
                    time.time() - record.finished_at >= self._settings.run_ttl_seconds
                ):
                    self._evict(record)
                    continue
            except Exception as exc:  # one bad record must not block the others
                logger.warning("run journal TTL recovery failed %s: %s", path.name, exc)
                continue

            self._runs[record.run_id] = record
            try:
                if record.status is RunStatus.CANCELLED:
                    if record.container_id is not None:
                        self._remove_container(record.container_id, record.run_id)
                        record.container_id = None
                        self._write_record(record)
                elif record.status not in TERMINAL_STATUSES:
                    self._remove_container(record.container_id, record.run_id)
                    record.result = RunResult(
                        run_id=record.run_id,
                        status=RunStatus.FAILED,
                        error=(
                            "Runner restarted before execution completed; the outcome is unknown "
                            "and was not retried"
                        ),
                        error_category=ErrorCategory.UNKNOWN_ERROR,
                        audit=record.result.audit if record.result else None,
                    )
                    record.status = RunStatus.FAILED
                    record.cancel_requested = False
                    record.container_id = None
                    record.finished_at = time.time()
                    self._write_record(record)
            except Exception as exc:  # isolate container/journal recovery failures
                logger.warning("run journal recovery failed %s: %s", path.name, exc)

    def _owned_container(self, container_id: str | None, run_id: UUID):
        if not container_id:
            return None
        try:
            container = self._client.containers.get(container_id)
            labels = container.labels or {}
            identity = str(run_id)
            if (
                labels.get("reproagent.managed") != "true"
                or labels.get("reproagent.owner") != self._owner_id
                or identity not in (
                    labels.get("reproagent.run_id"), labels.get("reproagent.request_id")
                )
            ):
                # Docker ids can be reused only after removal, but never trust an
                # id alone: cleanup must not kill an unrelated/reassigned container.
                logger.warning("refusing to remove container without matching Runner labels")
                return None
            return container
        except Exception:
            return None

    def _kill_container(self, container_id: str | None, run_id: UUID) -> None:
        container = self._owned_container(container_id, run_id)
        if container is None:
            return
        try:
            container.kill()
        except Exception:
            pass

    def _remove_container(self, container_id: str | None, run_id: UUID) -> None:
        container = self._owned_container(container_id, run_id)
        if container is None:
            return
        self._kill_container(container_id, run_id)
        try:
            container.remove(force=True)
        except Exception:
            pass

    def _remove_owned_orphans(self) -> None:
        try:
            containers = self._client.containers.list(all=True, filters={
                "label": ["reproagent.managed=true", f"reproagent.owner={self._owner_id}"],
            })
        except Exception as exc:
            logger.warning("Runner orphan-container scan failed: %s", exc)
            return
        for container in containers:
            try:
                container.kill()
            except Exception:
                pass
            try:
                container.remove(force=True)
            except Exception as exc:
                logger.warning("Runner orphan-container cleanup failed: %s", exc)

    def _evict(self, record: RunRecord) -> None:
        cleanup_run_dir(record.run_dir)
        self._journal_path(record.run_id).unlink(missing_ok=True)
        self._runs.pop(record.run_id, None)

    def start(self, request: RunRequest) -> RunAccepted:
        existing = next(
            (
                record
                for record in self._runs.values()
                if record.request.request_id == request.request_id
            ),
            None,
        )
        if existing is not None:
            if request_fingerprint(existing.request) != request_fingerprint(request):
                raise RequestConflictError(
                    "request_id already identifies different execution content"
                )
            return RunAccepted(run_id=existing.run_id, status=existing.status)
        active = sum(
            1 for r in self._runs.values() if r.status in (RunStatus.QUEUED, RunStatus.RUNNING)
        )
        if active >= self._settings.max_concurrent_runs:
            raise OverflowError("runner concurrency limit reached")
        run_id = request.request_id
        record = RunRecord(
            request=request,
            run_id=run_id,
            run_dir=self._settings.work_dir / str(run_id),
            fingerprint=request_fingerprint(request),
            accepted_at=time.time(),
        )
        self._runs[run_id] = record
        self._write_record(record)
        logger.info("runner run accepted", extra={"event": "run_accepted", "run_id": str(run_id)})
        record.task = asyncio.create_task(self._execute(record))
        return RunAccepted(run_id=run_id, status=RunStatus.QUEUED)

    async def _execute(self, record: RunRecord) -> None:
        if record.status is RunStatus.CANCELLED:
            return
        started = time.perf_counter()
        runner_queue_seconds = (
            max(0.0, time.time() - record.accepted_at)
            if record.accepted_at is not None
            else None
        )
        record.status = RunStatus.RUNNING
        self._write_record(record)
        loop = asyncio.get_running_loop()

        def on_container(container_id: str) -> None:
            record.container_id = container_id
            self._write_record(record)

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
        if record.cancel_requested or record.status is RunStatus.CANCELLED:
            # Cancellation is persisted before cancel() returns. A late sandbox
            # result must not replace that winning intent after a restart.
            if record.result is None:
                record.result = RunResult(
                    run_id=record.run_id,
                    status=RunStatus.CANCELLED,
                    error="cancelled while execution was in flight",
                )
            result = record.result
        record.result = result
        record.status = result.status
        record.container_id = None
        runner_total_seconds = time.perf_counter() - started
        timing = result.timings.model_dump()
        timing["runner_queue_seconds"] = runner_queue_seconds
        timing["runner_total_seconds"] = runner_total_seconds
        result = result.model_copy(update={"timings": RunTimings(**timing)})
        record.result = result
        if record.finished_at is None:
            record.finished_at = time.time()
        self._write_record(record)
        logger.info(
            "runner run finished",
            extra={
                "event": "run_finished",
                "run_id": str(record.run_id),
                "status": record.status.value,
                "duration_ms": round(runner_total_seconds * 1000, 2),
            },
        )

    def status_counts(self) -> dict[str, int]:
        counts = {"queued": 0, "running": 0, "terminal": 0}
        for record in self._runs.values():
            key = "terminal" if record.status in TERMINAL_STATUSES else record.status.value
            counts[key] = counts.get(key, 0) + 1
        counts["concurrency_limit"] = self._settings.max_concurrent_runs
        return counts

    def reap_expired(self) -> None:
        """Evict terminal records past their TTL and remove their work dirs.

        Results/artifacts are fetched promptly by the API after a run finishes;
        the TTL only bounds how long finished state lingers in memory and disk.
        """
        now = time.time()
        ttl = self._settings.run_ttl_seconds
        for record in list(self._runs.values()):
            if record.finished_at is None or now - record.finished_at < ttl:
                continue
            self._evict(record)

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
        if record.status in TERMINAL_STATUSES:
            return self.get(run_id)
        record.cancel_event.set()
        record.cancel_requested = True
        record.result = RunResult(
            run_id=run_id,
            status=RunStatus.CANCELLED,
            error="cancelled while execution was in flight",
        )
        record.status = RunStatus.CANCELLED
        record.finished_at = time.time()
        # Persist intent before any cleanup await/return so restart recovery can
        # distinguish a cancellation win from an unknown failed outcome.
        self._write_record(record)
        self._kill_container(record.container_id, run_id)
        logger.info("runner run cancelled", extra={"event": "run_cancelled", "run_id": str(run_id)})
        return record.result

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