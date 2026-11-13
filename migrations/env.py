from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import Settings
from backend.models import agents, github, knowledge, model_settings  # noqa: F401 -- migration metadata
from backend.models.identity import Base
from backend.runtime import run_async


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=Base.metadata, version_table_schema="public"
    )
    with context.begin_transaction():
        context.run_migrations()


async def migrate_online() -> None:
    engine = create_async_engine(Settings().database_url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_async(migrate_online())
