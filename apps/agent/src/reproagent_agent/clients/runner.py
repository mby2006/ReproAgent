from uuid import UUID

import httpx
from reproagent_schemas import RunAccepted, RunRequest, RunResult

from ..config import AgentSettings


class RunnerClient:
    """HTTP contract: Agent/Worker -> Runner. Execution happens only in the Runner.

    Runs are asynchronous: POST /runs returns a RunAccepted, then the caller polls
    GET /runs/{run_id} until a terminal status. The Runner never receives secrets.
    """

    def __init__(self, settings: AgentSettings, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=settings.runner_base_url, timeout=600.0, transport=transport
        )

    async def start_run(self, request: RunRequest) -> RunAccepted:
        response = await self._client.post("/runs", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return RunAccepted.model_validate(response.json())

    async def get_run(self, run_id: UUID) -> RunResult:
        response = await self._client.get(f"/runs/{run_id}")
        response.raise_for_status()
        return RunResult.model_validate(response.json())

    async def cancel_run(self, run_id: UUID) -> RunResult:
        response = await self._client.post(f"/runs/{run_id}/cancel")
        response.raise_for_status()
        return RunResult.model_validate(response.json())

    async def get_artifacts(self, run_id: UUID) -> list[str]:
        response = await self._client.get(f"/runs/{run_id}/artifacts")
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()