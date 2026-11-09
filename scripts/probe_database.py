"""在新建临时库上执行迁移、向量查询、checkpoint 恢复，最后删除本次临时库。"""

import json
import os
from typing import TypedDict
from uuid import uuid4

import psycopg
from alembic import command
from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pgvector.sqlalchemy import VECTOR
from psycopg import sql
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.checkpoints import checkpoint_saver, setup_checkpoints
from backend.config import Settings
from backend.db import database_ready, make_engine, migration_config
from backend.runtime import run_async


class ProbeBase(DeclarativeBase):
    pass


class ProbeVector(ProbeBase):
    __tablename__ = "d01_vector_probe"
    __table_args__ = {"schema": "app"}

    id: Mapped[int] = mapped_column(primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(VECTOR(3))


class ProbeState(TypedDict):
    value: int


def increment(state: ProbeState) -> ProbeState:
    return {"value": state["value"] + 1}


def probe_graph() -> StateGraph[ProbeState]:
    graph = StateGraph(ProbeState)
    graph.add_node("first", increment)
    graph.add_node("second", increment)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph


async def exercise_database(settings: Settings) -> dict[str, object]:
    engine = make_engine(settings)
    try:
        # A migrated app is still not ready until adapter-owned tables exist.
        assert not await database_ready(engine)
        async with engine.begin() as connection:
            await connection.run_sync(ProbeBase.metadata.create_all)
            await connection.execute(
                insert(ProbeVector),
                [
                    {"id": 1, "embedding": [1.0, 2.0, 3.0]},
                    {"id": 2, "embedding": [9.0, 8.0, 7.0]},
                ],
            )
            nearest = await connection.scalar(
                select(ProbeVector.id)
                .order_by(ProbeVector.embedding.l2_distance([1, 2, 3]))
                .limit(1)
            )
            assert nearest == 1
            version = await connection.scalar(text("SHOW server_version"))
            vector_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        await setup_checkpoints(settings)
        await setup_checkpoints(settings)
        config: RunnableConfig = {"configurable": {"thread_id": f"d01:{uuid4()}"}}
        async with checkpoint_saver(settings) as saver:
            graph = probe_graph().compile(checkpointer=saver, interrupt_after=["first"])
            await graph.ainvoke({"value": 0}, config)
            snapshot = await graph.aget_state(config)
            assert snapshot.values["value"] == 1 and snapshot.next == ("second",)
        # New connection + newly compiled graph must resume without repeating the first node.
        async with checkpoint_saver(settings) as saver:
            restored = probe_graph().compile(checkpointer=saver, interrupt_after=["first"])
            result = await restored.ainvoke(None, config)
            assert result["value"] == 2
            assert not (await restored.aget_state(config)).next
        assert await database_ready(engine)
        return {"postgres": version, "pgvector": vector_version, "checkpointResume": "PASS"}
    finally:
        await engine.dispose()


def run_smoke(admin_url: str) -> dict[str, object]:
    name = f"eah_d01_smoke_{uuid4().hex}"
    url = make_url(admin_url)
    admin_connection = url.set(drivername="postgresql").render_as_string(hide_password=False)
    target = url.set(database=name, drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )
    previous = os.environ.get("EAH_DATABASE_URL")
    created = False
    try:
        with psycopg.connect(admin_connection, autocommit=True, connect_timeout=5) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
        os.environ["EAH_DATABASE_URL"] = target
        try:
            run_async(setup_checkpoints(Settings(database_url=target)))
        except RuntimeError as error:
            assert "alembic upgrade head" in str(error)
        else:
            raise AssertionError("Checkpoint setup must reject an unmigrated database")
        config = migration_config()
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        return run_async(exercise_database(Settings(database_url=target)))
    finally:
        if previous is None:
            os.environ.pop("EAH_DATABASE_URL", None)
        else:
            os.environ["EAH_DATABASE_URL"] = previous
        if created:
            with psycopg.connect(
                admin_connection, autocommit=True, connect_timeout=5
            ) as connection:
                connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def main() -> int:
    load_dotenv()
    admin_url = os.getenv("EAH_TEST_ADMIN_URL")
    if not admin_url:
        print(json.dumps({"status": "UNVERIFIED", "missing": "EAH_TEST_ADMIN_URL"}))
        return 2
    result = run_smoke(admin_url)
    print(json.dumps({"status": "PASS", "temporaryDatabaseRemoved": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
