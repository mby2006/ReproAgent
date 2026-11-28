from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import AgentSettings, get_settings
from .graph.build import build_planning_graph, build_understanding_graph
from .llm.base import LLMAdapter
from .llm.factory import get_llm_adapter
from .review import ReviewService
from .routers import (
    codegen,
    comparisons,
    evaluation,
    health,
    plans,
    review,
    understand,
)


def create_app(settings: AgentSettings | None = None, llm: LLMAdapter | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        adapter = llm if llm is not None else get_llm_adapter(settings)
        app.state.llm = adapter
        app.state.review_service = ReviewService(adapter, settings)
        app.state.understanding_graph = build_understanding_graph(adapter)
        app.state.planning_graph = build_planning_graph()
        yield

    app = FastAPI(title="ReproAgent Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(understand.router)
    app.include_router(plans.router)
    app.include_router(codegen.router)
    app.include_router(evaluation.router)
    app.include_router(review.router)
    app.include_router(comparisons.router)
    return app


app = create_app()
