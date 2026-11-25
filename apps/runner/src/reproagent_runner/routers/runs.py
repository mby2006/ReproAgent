import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from reproagent_schemas import ExecImageStatus, RunAccepted, RunLogs, RunRequest, RunResult

from ..registry import RequestConflictError

router = APIRouter(tags=["runs"])


def _registry(request: Request):
    return request.app.state.registry


@router.get("/images/status", response_model=ExecImageStatus)
async def image_status(request: Request) -> ExecImageStatus:
    """A rerun must know whether its pinned image is still present."""
    return _registry(request).image_status()


@router.post("/runs", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_run(body: RunRequest, request: Request) -> RunAccepted:
    try:
        return _registry(request).start(body)
    except RequestConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc


@router.get("/runs/{run_id}", response_model=RunResult)
async def get_run(run_id: UUID, request: Request) -> RunResult:
    try:
        return _registry(request).get(run_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc


@router.get("/runs/{run_id}/logs", response_model=RunLogs)
async def get_run_logs(run_id: UUID, request: Request) -> RunLogs:
    try:
        return await asyncio.to_thread(_registry(request).live_logs, run_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc


@router.post("/runs/{run_id}/cancel", response_model=RunResult)
async def cancel_run(run_id: UUID, request: Request) -> RunResult:
    try:
        return _registry(request).cancel(run_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc


@router.get("/runs/{run_id}/artifacts", response_model=list[str])
async def get_artifacts(run_id: UUID, request: Request) -> list[str]:
    try:
        return _registry(request).artifacts(run_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc


@router.get("/runs/{run_id}/artifacts/{name}")
async def get_artifact(run_id: UUID, name: str, request: Request) -> FileResponse:
    try:
        path = _registry(request).artifact_path(run_id, name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found") from exc
    return FileResponse(path, filename=name, media_type="application/octet-stream")