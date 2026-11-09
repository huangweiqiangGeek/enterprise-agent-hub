"""Linux run consumer; duplicate deliveries only attempt a database claim."""

import sys
from datetime import timedelta
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.config import Settings
from backend.db import make_engine
from backend.models.identity import now
from backend.models.knowledge import OutboxJob
from backend.models.runs import Run
from backend.repositories.runs import RunRepository
from backend.runtime import run_async
from backend.services.model_probe import ModelProbe
from backend.services.qa import QA
from backend.services.run_state import RunState

settings = Settings()
app = Celery("eah-runs", broker=settings.redis_url)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_transport_options={"visibility_timeout": 180},
)


async def execute(org: UUID, run_id: UUID) -> None:
    engine = make_engine(settings)
    try:
        await QA(RunState(engine, settings, org, run_id), ModelProbe(settings)).run()
    finally:
        await engine.dispose()


@app.task(name="eah.run", soft_time_limit=130, time_limit=140)  # type: ignore[untyped-decorator]
def run(organizationId: str, runId: str) -> None:
    run_async(execute(UUID(organizationId), UUID(runId)))


async def reconcile(engine: AsyncEngine, config: Settings) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        candidates = list(
            await db.scalars(
                select(Run)
                .where(
                    or_(
                        Run.status == "queued",
                        (Run.status == "running") & (Run.lease_until < now()),
                    )
                )
                .order_by(Run.created_at)
                .limit(100)
            )
        )
    for row in candidates:
        state = RunState(engine, config, row.organization_id, row.id)
        state.generation = row.lease_generation
        if row.status == "running" or row.created_at < now() - timedelta(seconds=300):
            await state.terminal(
                error="RUN_INTERRUPTED" if row.status == "running" else "RUN_QUEUE_TIMEOUT",
                expired_only=True,
            )
        elif row.created_at < now() - timedelta(seconds=30):
            async with sessions.begin() as db:
                current = await RunRepository(db, row.organization_id).lock(row.id)
                if current.status != "queued":
                    continue
                job = await db.scalar(
                    select(OutboxJob)
                    .where(OutboxJob.resource_id == row.id, OutboxJob.job_type == "run.execute")
                    .with_for_update()
                )
                if job and job.published_at and job.published_at < now() - timedelta(seconds=30):
                    job.published_at, job.publish_attempts, job.available_at = None, 0, now()


if __name__ == "__main__":
    if sys.platform != "linux":
        raise SystemExit("Use the Linux Compose run worker.")
    app.worker_main(["worker", "--loglevel=WARNING", "--concurrency=4", "-Q", settings.run_queue])
