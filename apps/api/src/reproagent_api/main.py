import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress

import httpx
from arq.connections import RedisSettings, create_pool
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from reproagent_schemas import MAX_PDF_BYTES

from .config import ApiSettings, get_settings
from .health_checks import check_dependencies
from .observability import Metrics, RequestTelemetryMiddleware, configure_logging
from .routers import (
    comparisons,
    experiments,
    health,
    papers,
    runs,
    workflows,
)

logger = logging.getLogger(__name__)

MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BODY_BYTES = MAX_PDF_BYTES + 1024 * 1024
HEALTH_PROBE_INTERVAL_SECONDS = 15
HEALTH_SNAPSHOT_TTL_SECONDS = 45


async def monitor_health(app: FastAPI):
    while True:
        await asyncio.sleep(HEALTH_PROBE_INTERVAL_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                result = await check_dependencies(
                    app.state.settings, client, app.state.arq
                )
            app.state.health_snapshot = {
                "checked_at": time.monotonic(),
                "status": result.status.value,
                "dependencies": [
                    dependency.model_dump(mode="json") for dependency in result.dependencies
                ],
            }
        except Exception as exc:
            app.state.health_snapshot = {
                "checked_at": time.monotonic(),
                "status": "unavailable",
                "dependencies": [],
            }
            logger.warning("health probe failed: %s", type(exc).__name__)


class RequestBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_upload = scope["method"] == "POST" and scope["path"].rstrip("/") == "/v1/papers"
        limit = MAX_UPLOAD_BODY_BYTES if is_upload else MAX_JSON_BODY_BYTES
        headers = dict(scope["headers"])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                await self._reject(send, 400, "Invalid Content-Length")
                return
            if declared < 0:
                await self._reject(send, 400, "Invalid Content-Length")
                return
            if declared > limit:
                await self._reject(send, 413, "Request body too large")
                return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await self._reject(send, 413, "Request body too large")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return {"type": "http.disconnect"}

        scope = dict(scope)
        scope["headers"] = [
            (key, value)
            for key, value in scope["headers"]
            if key not in (b"content-length", b"transfer-encoding")
        ] + [(b"content-length", str(size).encode())]
        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send, status_code: int, detail: str) -> None:
        body = f'{{"detail":"{detail}"}}'.encode()
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging("api")
        app.state.arq = None
        app.state.health_snapshot = None
        try:
            app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        except Exception:
            logger.warning("Redis unavailable; uploads and readiness will report degraded")
        monitor = asyncio.create_task(monitor_health(app))
        try:
            yield
        finally:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
            if app.state.arq is not None:
                await app.state.arq.aclose()

    app = FastAPI(title="ReproAgent API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.arq = None
    app.state.health_snapshot = None
    app.state.metrics = Metrics()
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestTelemetryMiddleware, metrics=app.state.metrics)
    app.include_router(health.router)
    app.include_router(papers.router)
    app.include_router(experiments.router)
    app.include_router(runs.router)
    app.include_router(workflows.router)
    app.include_router(comparisons.router)
    return app


app = create_app()
