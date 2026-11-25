import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4


class JsonLogFormatter(logging.Formatter):
    fields = (
        "request_id", "method", "route", "status", "duration_ms", "run_id", "workflow_id",
        "event", "error_type", "outbox_candidates", "stale_runs",
    )

    def format(self, record):
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in self.fields:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(service):
    if os.getenv("LOG_FORMAT", "").lower() != "json":
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class Metrics:
    def __init__(self, max_series=100):
        self.max_series = max_series
        self._lock = threading.Lock()
        self._series = {}

    @staticmethod
    def _route(method, route):
        method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"} else "OTHER"
        route = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "{id}", route)
        route = re.sub(r"/\d+(?=/|$)", "/{id}", route)
        if not route.startswith("/"):
            route = "/__unknown__"
        return method, route[:120]

    def observe(self, method, route, status, duration_seconds):
        method, route = self._route(method, route)
        key = (method, route, str(status))
        with self._lock:
            if key not in self._series and len(self._series) >= self.max_series - 1:
                key = ("OTHER", "/__overflow__", "overflow")
            count, duration = self._series.get(key, (0, 0.0))
            self._series[key] = (count + 1, duration + duration_seconds)

    def snapshot(self):
        with self._lock:
            return dict(self._series)

    def render(self):
        lines = [
            "# HELP reproagent_http_requests_total Process-local HTTP request count.",
            "# TYPE reproagent_http_requests_total counter",
            "# HELP reproagent_http_duration_seconds_sum Process-local HTTP duration sum.",
            "# TYPE reproagent_http_duration_seconds_sum counter",
        ]
        for (method, route, status), (count, duration) in sorted(self.snapshot().items()):
            def escape(value):
                return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

            labels = (
                f'method="{escape(method)}",route="{escape(route)}",'
                f'status="{escape(status)}"'
            )
            lines.append(f"reproagent_http_requests_total{{{labels}}} {count}")
            lines.append(f"reproagent_http_duration_seconds_sum{{{labels}}} {duration:.6f}")
        return "\n".join(lines) + "\n"


class RequestTelemetryMiddleware:
    def __init__(self, app, metrics):
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = dict(scope["headers"]).get(b"x-request-id", b"").decode(errors="replace")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            request_id = uuid4().hex
        scope = dict(scope)
        scope["request_id"] = request_id
        status = 500
        start = time.perf_counter()

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            route = scope.get("route")
            route = route.path if route is not None else scope["path"]
            self.metrics.observe(scope["method"], route, status, duration)
            logging.getLogger("reproagent_api.access").info(
                "HTTP request",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "route": route,
                    "status": status,
                    "duration_ms": round(duration * 1000, 2),
                },
            )
