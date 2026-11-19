import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import RunnerSettings, get_settings
from .registry import RunRegistry
from .routers import health, runs


def create_app(settings: RunnerSettings | None = None, client=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry = RunRegistry(settings, client=client)
        app.state.registry = registry
        reaper = asyncio.create_task(registry.reap_loop())
        try:
            yield
        finally:
            reaper.cancel()

    app = FastAPI(title="ReproAgent Runner", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(runs.router)
    return app


app = create_app()