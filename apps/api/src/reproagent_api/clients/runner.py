from uuid import UUID

import httpx
from reproagent_schemas import RunAccepted, RunLogs, RunRequest, RunResult

from ..config import ApiSettings


class RunnerClient:
    """HTTP contract: Worker -> Runner. Execution happens only inside the Runner.

    The Worker (API trust domain) drives runs and persists results; the Runner
    holds no keys. Runs are asynchronous: start, then poll until terminal.
    """

    def __init__(self, settings: ApiSettings, transport: httpx.AsyncBaseTransport | None = None):
        # trust_env=False: some hosts set NO_PROXY entries (e.g. "[::1]") that
        # httpx cannot parse; agent/runner traffic is local and never proxied.
        self._client = httpx.AsyncClient(
            base_url=settings.runner_base_url,
            timeout=600.0,
            transport=transport,
            trust_env=False,
        )

    async def start_run(self, request: RunRequest) -> RunAccepted:
        response = await self._client.post("/runs", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return RunAccepted.model_validate(response.json())

    async def get_run(self, run_id: UUID) -> RunResult:
        response = await self._client.get(f"/runs/{run_id}")
        response.raise_for_status()
        return RunResult.model_validate(response.json())

    async def get_run_logs(self, run_id: UUID) -> RunLogs:
        response = await self._client.get(f"/runs/{run_id}/logs")
        response.raise_for_status()
        return RunLogs.model_validate(response.json())

    async def cancel_run(self, run_id: UUID) -> RunResult:
        response = await self._client.post(f"/runs/{run_id}/cancel")
        response.raise_for_status()
        return RunResult.model_validate(response.json())

    async def get_artifacts(self, run_id: UUID) -> list[str]:
        response = await self._client.get(f"/runs/{run_id}/artifacts")
        response.raise_for_status()
        return response.json()

    async def get_artifact_bytes(self, run_id: UUID, name: str) -> bytes:
        response = await self._client.get(f"/runs/{run_id}/artifacts/{name}")
        response.raise_for_status()
        return response.content

    async def aclose(self) -> None:
        await self._client.aclose()