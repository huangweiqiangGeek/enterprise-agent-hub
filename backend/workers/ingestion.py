"""Linux Celery consumer. Message fields are IDs; business state is always loaded from PG."""

import sys
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]

from backend.config import Settings
from backend.db import make_engine
from backend.runtime import run_async
from backend.services.ingestion import Ingestion
from backend.services.storage import Storage

settings = Settings()
app = Celery("eah", broker=settings.redis_url)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_transport_options={"visibility_timeout": 660},
    task_routes={"eah.ingest": {"queue": settings.ingestion_queue}},
)


async def execute(org: UUID, doc: UUID, attempt: UUID) -> None:
    engine = make_engine(settings)
    try:
        await Ingestion(engine, settings, Storage(settings)).run(org, doc, attempt)
    finally:
        await engine.dispose()


@app.task(name="eah.ingest", soft_time_limit=615, time_limit=630)  # type: ignore[untyped-decorator]
def ingest(organizationId: str, documentId: str, attemptId: str, requestId: str) -> None:
    run_async(execute(UUID(organizationId), UUID(documentId), UUID(attemptId)))


if __name__ == "__main__":
    if sys.platform != "linux":
        raise SystemExit("Use the Linux Compose ingestion worker for process isolation.")
    app.worker_main(
        ["worker", "--loglevel=WARNING", "--concurrency=2", "-Q", settings.ingestion_queue]
    )
