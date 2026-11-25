"""Best-effort delivery of durable outbox rows to arq."""

import logging
from datetime import UTC, datetime
from uuid import UUID

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def enqueue_pending_outbox(
    admin,
    redis,
    *,
    entity_id: UUID | None = None,
    limit: int = 100,
) -> int:
    """Deliver pending jobs, leaving failed queue attempts durable for retry.

    The deterministic arq job id makes a crash between Redis acceptance and the
    database update safe: the next delivery is deduplicated by arq while the
    outbox row is still pending.
    """
    if redis is None:
        return 0

    query = admin.table("job_outbox").select("*").eq("status", "pending")
    if entity_id is not None:
        query = query.eq("entity_id", str(entity_id))
    rows = query.order("created_at").limit(limit).execute().data

    dispatched = 0
    for row in rows:
        try:
            await redis.enqueue_job(
                row["job_type"],
                str(row["entity_id"]),
                _job_id=row["job_id"],
            )
        except Exception as exc:
            logger.warning("outbox job %s enqueue failed: %s", row["job_id"], exc)
            admin.table("job_outbox").update({
                "attempts": row.get("attempts", 0) + 1,
                "last_error": str(exc),
                "updated_at": _now(),
            }).eq("id", row["id"]).eq("status", "pending").execute()
            continue

        updated = admin.table("job_outbox").update({
            "status": "enqueued",
            "last_error": None,
            "updated_at": _now(),
        }).eq("id", row["id"]).eq("status", "pending").execute().data
        if updated:
            dispatched += 1
    return dispatched
