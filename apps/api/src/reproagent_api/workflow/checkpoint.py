"""Checkpointer factories.

Production uses AsyncPostgresSaver on Supabase Postgres so checkpoints survive
worker restarts and support human-in-the-loop resume. Tests use MemorySaver.
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg_pool import AsyncConnectionPool

# Checkpoint state holds our own Pydantic schema models. Explicitly allow them
# through msgpack so deserialization stays stable across worker restarts even if
# LangGraph flips its default to strict msgpack.
_SERDE = JsonPlusSerializer(allowed_msgpack_modules=True)


def make_memory_checkpointer() -> MemorySaver:
    return MemorySaver(serde=_SERDE)


async def make_postgres_checkpointer(db_url: str) -> tuple[AsyncPostgresSaver, AsyncConnectionPool]:
    pool = AsyncConnectionPool(
        conninfo=db_url,
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool, serde=_SERDE)
    await saver.setup()
    return saver, pool
