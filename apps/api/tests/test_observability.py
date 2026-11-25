import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.observability import JsonLogFormatter, Metrics


def test_metrics_use_bounded_route_labels_and_render_prometheus_text():
    metrics = Metrics()
    metrics.observe("GET", "/v1/papers/123", 200, 12.5)
    metrics.observe("GET", "/v1/papers/456", 500, 2.5)
    body = metrics.render()
    assert 'method="GET"' in body
    assert 'route="/v1/papers/{id}"' in body
    assert "/v1/papers/123" not in body
    assert "reproagent_http_requests_total" in body
    assert "reproagent_http_duration_seconds_sum" in body
    assert 'status="200"' in body
    assert 'status="500"' in body


def test_metrics_escape_untrusted_route_labels():
    metrics = Metrics()
    metrics.observe("GET", '/unknown/"\\\n', 404, 1)
    body = metrics.render()
    assert 'route="/unknown/\\"\\\\\\n"' in body


def test_metrics_cap_label_cardinality_across_overflow_statuses():
    metrics = Metrics()
    for index in range(150):
        metrics.observe("GET", f"/unknown-{index}", 200, 1)
    for status in range(201, 250):
        metrics.observe("GET", f"/unbounded-{status}", status, 1)
    assert len(metrics.snapshot()) <= 100
    assert ("OTHER", "/__overflow__", "overflow") in metrics.snapshot()


def test_json_logs_do_not_include_query_strings(caplog):
    formatter = JsonLogFormatter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "request", (), None)
    record.request_id = "abc"
    record.route = "/v1/papers"
    record.status = 200
    payload = json.loads(formatter.format(record))
    assert payload["request_id"] == "abc"
    assert payload["route"] == "/v1/papers"
    assert "query" not in payload


@pytest.fixture
def settings():
    return ApiSettings(
        supabase_url="http://supabase.test",
        supabase_anon_key="anon-test-key",
        supabase_service_role_key="service-test-key",
        agent_base_url="http://agent.test",
        ops_token="ops-secret",
        _env_file=None,
    )


def test_readiness_fails_closed_until_fresh_healthy_snapshot(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"


def test_readiness_serves_cached_snapshot_without_probing(settings, monkeypatch):
    app = create_app(settings)

    async def forbidden(*args, **kwargs):
        raise AssertionError("readiness must not probe dependencies")

    monkeypatch.setattr("reproagent_api.routers.health.check_dependencies", forbidden)
    with TestClient(app) as client:
        app.state.health_snapshot = {
            "checked_at": 1,
            "status": "ok",
            "dependencies": [],
        }
        response = client.get("/ready")
        assert response.status_code == 503


def test_readiness_serves_fresh_healthy_snapshot_without_probing(settings, monkeypatch):
    app = create_app(settings)

    async def forbidden(*args, **kwargs):
        raise AssertionError("readiness must not probe dependencies")

    monkeypatch.setattr("reproagent_api.routers.health.check_dependencies", forbidden)
    with TestClient(app) as client:
        app.state.health_snapshot = {
            "checked_at": time.monotonic(),
            "status": "ok",
            "dependencies": [],
        }
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_metrics_require_configured_ops_token(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"X-Ops-Token": "wrong"}).status_code == 401
        response = client.get("/metrics", headers={"X-Ops-Token": "ops-secret"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")


def test_metrics_are_absent_when_token_is_not_configured(settings):
    settings.ops_token = ""
    with TestClient(create_app(settings)) as client:
        assert client.get("/metrics").status_code == 404
