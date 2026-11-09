from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def psycopg_url(settings: Settings) -> str:
    return (
        make_url(settings.database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def migration_config() -> Config:
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return config


async def application_schema_ready(engine: AsyncEngine) -> bool:
    expected = ScriptDirectory.from_config(migration_config()).get_current_head()
    async with engine.connect() as connection:
        table = await connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
        if table is None:
            return False
        revision = await connection.scalar(text("SELECT version_num FROM public.alembic_version"))
        schemas = await connection.scalar(
            text(
                "SELECT to_regnamespace('app') IS NOT NULL "
                "AND to_regnamespace('checkpoints') IS NOT NULL"
            )
        )
        return expected is not None and revision == expected and bool(schemas)


async def database_ready(engine: AsyncEngine) -> bool:
    if not await application_schema_ready(engine):
        return False
    async with engine.connect() as connection:
        vector = await connection.scalar(
            text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        )
        tables = await connection.scalar(
            text(
                "SELECT to_regclass('checkpoints.checkpoints') IS NOT NULL "
                "AND to_regclass('checkpoints.checkpoint_blobs') IS NOT NULL "
                "AND to_regclass('checkpoints.checkpoint_writes') IS NOT NULL "
                "AND to_regclass('checkpoints.checkpoint_migrations') IS NOT NULL"
            )
        )
        if not vector or not tables:
            return False
        checkpoint_version = await connection.scalar(
            text("SELECT max(v) FROM checkpoints.checkpoint_migrations")
        )
        return bool(checkpoint_version == len(AsyncPostgresSaver.MIGRATIONS) - 1)
