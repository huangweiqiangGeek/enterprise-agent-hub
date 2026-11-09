from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.conninfo import make_conninfo

from backend.config import Settings
from backend.db import application_schema_ready, make_engine, psycopg_url
from backend.runtime import run_async


@asynccontextmanager
async def checkpoint_saver(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    conninfo = make_conninfo(
        psycopg_url(settings),
        options="-c search_path=checkpoints",
        connect_timeout=5,
    )
    async with AsyncPostgresSaver.from_conn_string(
        conninfo, serde=JsonPlusSerializer(allowed_msgpack_modules=[])
    ) as saver:
        yield saver


async def setup_checkpoints(settings: Settings) -> None:
    engine = make_engine(settings)
    try:
        if not await application_schema_ready(engine):
            raise RuntimeError("Run alembic upgrade head before checkpoint setup")
    finally:
        await engine.dispose()
    async with checkpoint_saver(settings) as saver:
        # Only the explicit bootstrap command owns adapter migrations.
        await saver.setup()


if __name__ == "__main__":
    run_async(setup_checkpoints(Settings()))
    print("PASS: checkpoint schema initialized")
