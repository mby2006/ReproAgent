import logging
from contextlib import asynccontextmanager

from arq.connections import RedisSettings, create_pool
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import ApiSettings, get_settings
from .routers import experiments, health, papers, reproductions, runs, workflows

logger = logging.getLogger(__name__)


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.arq = None
        try:
            app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        except Exception:
            logger.warning("Redis unavailable at %s; uploads will return 503", settings.redis_url)
        yield
        if app.state.arq is not None:
            await app.state.arq.aclose()

    app = FastAPI(title="ReproAgent API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(reproductions.router)
    app.include_router(papers.router)
    app.include_router(experiments.router)
    app.include_router(runs.router)
    app.include_router(workflows.router)
    return app


app = create_app()
