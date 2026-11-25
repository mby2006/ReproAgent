"""Integration: per-workflow advisory lock prevents concurrent graph drivers.

Reproduces the arq race where restart recovery (recover_workflow_task) overlaps the
original queued run_workflow_task on the SAME thread_id. Without mutual exclusion
both drive the shared checkpoint, duplicating nodes/spec versions and forking the
checkpoint chain. The lock is session-scoped Postgres advisory, so these tests need
a real `supabase start` Postgres and fail when it is unavailable.

Run explicitly:
    uv run pytest tests/integration/test_workflow_lock.py -m integration -q
"""

import asyncio
import base64
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from reproagent_api import worker
from reproagent_api.clients.supabase import make_service_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.workflow import make_memory_checkpointer
from reproagent_paperparse import ground_card, parse_pdf
from reproagent_schemas import (
    ExtractResponse,
    GroundResponse,
    LLMUsage,
    ParsedDocument,
    ParseResponse,
    ReproductionCard,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_understanding import (  # noqa: E402
    MOCK_CARD,
    FakeArq,
    build_pdf,
    token_for,
    upload,
)


@pytest.fixture(scope="module")
def settings() -> ApiSettings:
    return ApiSettings()


@pytest.fixture(scope="module", autouse=True)
def _require_db(settings):
    try:
        with psycopg.connect(settings.supabase_db_url, connect_timeout=5) as conn:
            conn.execute("select 1")
    except Exception as exc:  # noqa: BLE001 - required integration dependency
        pytest.fail(f"supabase postgres unavailable: {exc}")


@pytest.fixture(scope="module")
def alice_client(settings):
    admin = make_service_client(settings)
    suffix = uuid4().hex[:12]
    user = admin.auth.admin.create_user(
        {
            "email": f"lock-{suffix}@example.test",
            "password": "test-password-123",
            "email_confirm": True,
        }
    ).user
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.arq = FakeArq()
        client.headers["Authorization"] = f"Bearer {token_for(settings, user.email)}"
        yield client
    admin.auth.admin.delete_user(user.id)


async def test_drive_lock_is_mutually_exclusive_and_released(settings):
    wid = str(uuid4())
    with worker._drive_lock(settings, wid) as first:
        assert first is True
        # A second connection (== another arq job) cannot take the same thread.
        with worker._drive_lock(settings, wid) as second:
            assert second is False
        # An unrelated workflow is independent.
        with worker._drive_lock(settings, str(uuid4())) as other:
            assert other is True
    # Closing the holder's session releases the lock for the next driver.
    with worker._drive_lock(settings, wid) as again:
        assert again is True


async def test_concurrent_run_and_recovery_drive_the_thread_once(settings, alice_client):
    ids = upload(alice_client, build_pdf())
    workflow_id, paper_id = ids["workflow_id"], ids["paper_id"]

    entered_extract = asyncio.Event()
    release_extract = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path
        if path == "/v1/parse":
            document = parse_pdf(base64.b64decode(body["pdf_b64"]))
            payload = ParseResponse(document=document).model_dump(mode="json")
            return httpx.Response(200, json=payload)
        if path == "/v1/extract":
            # Hold the first driver inside build_reproduction_spec so recovery overlaps.
            entered_extract.set()
            await release_extract.wait()
            usage = LLMUsage(
                model_name="stub", prompt_tokens=100, completion_tokens=50, total_tokens=150
            )
            payload = ExtractResponse(card_raw=MOCK_CARD, usages=[usage]).model_dump(mode="json")
            return httpx.Response(200, json=payload)
        if path == "/v1/ground":
            doc = ParsedDocument.model_validate(body["document"])
            card = ReproductionCard.model_validate(ground_card(body["card_raw"], doc))
            return httpx.Response(200, json=GroundResponse(card=card).model_dump(mode="json"))
        return httpx.Response(404, json={"detail": f"unexpected path {path}"})

    ctx = {
        "settings": settings,
        "agent_transport": httpx.MockTransport(handler),
        "checkpointer": make_memory_checkpointer(),
    }

    run_task = asyncio.create_task(worker.run_workflow_task(ctx, workflow_id))
    await asyncio.wait_for(entered_extract.wait(), timeout=30)

    # Recovery sees a non-terminal/non-awaiting workflow but the thread is locked:
    # it must skip immediately rather than drive a second concurrent graph.
    recovery_task = asyncio.create_task(worker.recover_workflow_task(ctx, workflow_id))
    await asyncio.wait_for(recovery_task, timeout=30)

    release_extract.set()
    await asyncio.wait_for(run_task, timeout=60)

    admin = make_service_client(settings)
    traces = (
        admin.table("agent_traces")
        .select("node_name").eq("workflow_id", workflow_id).execute().data
    )
    assert [t["node_name"] for t in traces].count("ingest_paper") == 1
    assert [t["node_name"] for t in traces].count("build_reproduction_spec") == 1

    specs = (
        admin.table("reproduction_specs")
        .select("version").eq("paper_id", paper_id).order("version").execute().data
    )
    assert [s["version"] for s in specs] == [1]

    wf = admin.table("workflows").select("status").eq("id", workflow_id).execute().data[0]
    assert wf["status"] == "awaiting_spec_review"