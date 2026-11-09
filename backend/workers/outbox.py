"""Bounded outbox delivery and reference-aware original/derived content cleanup."""

import argparse
import json
from datetime import timedelta
from functools import partial
from typing import Any
from uuid import uuid4

from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.config import Settings
from backend.db import make_engine
from backend.models.identity import now
from backend.models.knowledge import Document, DocumentAttempt, DocumentChunk, OutboxJob
from backend.runtime import run_async
from backend.services.storage import Storage


class Outbox:
    def __init__(self, engine: AsyncEngine, settings: Settings, storage: Storage) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.storage, self.settings = storage, settings
        self.broker = Celery("eah", broker=settings.redis_url)
        self.broker.conf.update(
            task_serializer="json",
            accept_content=["json"],
            broker_connection_timeout=5,
            broker_transport_options={"socket_timeout": 5, "socket_connect_timeout": 5},
        )

    async def dispatch(self, limit: int = 100) -> dict[str, int]:
        succeeded = failed = 0
        for _ in range(limit):
            token = uuid4()
            async with self.sessions.begin() as db:
                job = await db.scalar(
                    select(OutboxJob)
                    .where(
                        OutboxJob.published_at.is_(None),
                        OutboxJob.available_at <= now(),
                        OutboxJob.publish_attempts < 5,
                        or_(OutboxJob.lease_until.is_(None), OutboxJob.lease_until < now()),
                    )
                    .order_by(OutboxJob.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if not job:
                    break
                job.lease_until, job.lease_token = now() + timedelta(seconds=60), token
                job.publish_attempts += 1
                document = await db.scalar(
                    select(Document).where(
                        Document.id == job.resource_id,
                        Document.organization_id == job.organization_id,
                    )
                )
                should_publish = bool(
                    document
                    and not document.deleted_at
                    and document.current_attempt_id == job.resource_version
                    and document.status == "queued"
                )
                object_key = document.object_key if document and document.deleted_at else None
                derived_keys = (
                    list(
                        (
                            await db.scalars(
                                select(DocumentAttempt.extracted_object_key).where(
                                    DocumentAttempt.organization_id == job.organization_id,
                                    DocumentAttempt.document_id == job.resource_id,
                                    DocumentAttempt.extracted_object_key.is_not(None),
                                )
                            )
                        ).all()
                    )
                    if object_key
                    else []
                )
            error = None
            try:
                if job.job_type == "document.ingest":
                    if should_publish:
                        await self.storage.call(
                            partial(
                                self.broker.send_task,
                                "eah.ingest",
                                kwargs=job.payload,
                                task_id=str(job.id),
                                queue=self.settings.ingestion_queue,
                                retry=False,
                            )
                        )
                elif job.job_type == "document.cleanup":
                    if object_key:
                        await self.storage.delete(object_key)
                        for key in derived_keys:
                            if key:
                                await self.storage.delete(key)
                else:
                    raise ValueError("Unknown job type")
            except Exception:
                # Stable diagnostics only; broker/S3 exceptions can contain credentials or payloads.
                error = "OUTBOX_DELIVERY_FAILED"
            async with self.sessions.begin() as db:
                current = await db.scalar(
                    select(OutboxJob)
                    .where(OutboxJob.id == job.id, OutboxJob.lease_token == token)
                    .with_for_update()
                )
                if not current:
                    continue
                current.lease_until = current.lease_token = None
                current.last_error = error
                if error:
                    current.available_at = now() + timedelta(
                        seconds=min(300, 2**current.publish_attempts)
                    )
                    failed += 1
                else:
                    current.published_at = now()
                    succeeded += 1
                if current.job_type == "document.cleanup":
                    row = await db.scalar(
                        select(Document).where(
                            Document.id == current.resource_id,
                            Document.organization_id == current.organization_id,
                            Document.deleted_at.is_not(None),
                        )
                    )
                    if row:
                        if not error:
                            chunks = (
                                await db.scalars(
                                    select(DocumentChunk).where(
                                        DocumentChunk.organization_id == row.organization_id,
                                        DocumentChunk.document_id == row.id,
                                    )
                                )
                            ).all()
                            for chunk in chunks:
                                chunk.text, chunk.embedding, chunk.lexemes = None, None, ""
                        row.cleanup_status, row.cleanup_error = (
                            ("failed", error) if error else ("done", None)
                        )
        return {"published": succeeded, "failed": failed}

    async def retry_failed(self) -> int:
        async with self.sessions.begin() as db:
            rows = (
                await db.scalars(
                    select(OutboxJob)
                    .where(
                        OutboxJob.published_at.is_(None),
                        OutboxJob.publish_attempts >= 5,
                        or_(OutboxJob.lease_until.is_(None), OutboxJob.lease_until < now()),
                    )
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                row.publish_attempts, row.available_at = 0, now()
            return len(rows)

    async def clean_orphans(self, cursor: str | None = None) -> dict[str, Any]:
        # The 24h cutoff exceeds the bounded 180s upload + SDK call window. Check DB references
        # immediately before deletion: a successfully committed document always owns its object.
        options: dict[str, Any] = {
            "Bucket": self.settings.s3_bucket,
            "Prefix": self.settings.s3_prefix,
            "MaxKeys": 100,
        }
        if cursor:
            options["ContinuationToken"] = cursor
        page = await self.storage.call(partial(self.storage.client.list_objects_v2, **options))
        deleted = 0
        for obj in page.get("Contents", []):
            if obj["LastModified"] >= now() - timedelta(hours=24):
                continue
            async with self.sessions.begin() as db:
                referenced = await db.scalar(
                    select(Document.id).where(Document.object_key == obj["Key"])
                )
                if not referenced:
                    referenced = await db.scalar(
                        select(DocumentAttempt.id).where(
                            DocumentAttempt.extracted_object_key == obj["Key"]
                        )
                    )
            if not referenced:
                await self.storage.delete(obj["Key"])
                deleted += 1
        return {"deleted": deleted, "nextCursor": page.get("NextContinuationToken")}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--orphans", action="store_true")
    parser.add_argument("--cursor")
    args = parser.parse_args()
    settings = Settings()
    engine = make_engine(settings)
    worker = Outbox(engine, settings, Storage(settings))
    try:
        if args.retry_failed:
            print(json.dumps({"retried": await worker.retry_failed()}))
        print(json.dumps(await worker.dispatch()))
        if args.orphans:
            print(json.dumps(await worker.clean_orphans(args.cursor)))
    finally:
        worker.broker.close()
        await engine.dispose()


if __name__ == "__main__":
    run_async(main())
