"""Attempt leases fence all writes; external calls never hold a business transaction."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.models.identity import Organization, now
from backend.models.knowledge import Document, DocumentAttempt, DocumentChunk, OutboxJob
from backend.models.model_settings import ConnectionVersion, ModelProfile, UsageEntry
from backend.rag import parser
from backend.rag.documents import PIPELINE, Chunk, IngestError, lexemes, split
from backend.rag.embeddings import Embeddings
from backend.services.errors import AppError
from backend.services.model_probe import Attempt
from backend.services.storage import Storage

LEASE_SECONDS = 30
DEADLINE_SECONDS = 600


@dataclass
class Lease:
    document: Document
    attempt: DocumentAttempt
    profile: ModelProfile
    connection: ConnectionVersion


class Ingestion:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        storage: Storage,
        embeddings: Embeddings | None = None,
    ) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.settings, self.storage = settings, storage
        self.embeddings = embeddings or Embeddings(settings)

    @asynccontextmanager
    async def locked(
        self, org: UUID, doc: UUID
    ) -> AsyncIterator[tuple[AsyncSession, Document | None]]:
        async with self.sessions.begin() as db:
            # Same order as API organization authorization and deletion prevents deadlocks.
            await db.get(Organization, org, with_for_update=True)
            row = await db.scalar(
                select(Document)
                .where(Document.organization_id == org, Document.id == doc)
                .with_for_update()
            )
            yield db, row

    async def claim(self, org: UUID, doc: UUID, attempt_id: UUID) -> Lease | None:
        async with self.locked(org, doc) as (db, row):
            if (
                not row
                or row.deleted_at
                or row.current_attempt_id != attempt_id
                or row.status != "queued"
            ):
                return None
            active = await db.scalar(
                select(Document.id)
                .where(
                    Document.organization_id == org,
                    Document.status == "processing",
                    Document.deleted_at.is_(None),
                )
                .limit(1)
            )
            if active:
                # The dispatcher reoffers queued work; PG owns organization slots.
                return None
            attempt = await db.get(DocumentAttempt, attempt_id, with_for_update=True)
            assert attempt is not None
            profile = await db.get(ModelProfile, attempt.embedding_profile_id)
            connection = await db.get(ConnectionVersion, attempt.model_connection_version_id)
            assert profile is not None and connection is not None
            attempt.status = row.status = "processing"
            attempt.started_at = now()
            attempt.pipeline_version = PIPELINE
            attempt.lease_owner = uuid4().hex
            attempt.lease_generation += 1
            attempt.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
            return Lease(row, attempt, profile, connection)

    async def fence(self, db: AsyncSession, row: Document | None, lease: Lease) -> DocumentAttempt:
        a = await db.get(DocumentAttempt, lease.attempt.id, with_for_update=True)
        if (
            not row
            or row.deleted_at
            or row.status != "processing"
            or row.current_attempt_id != lease.attempt.id
            or not a
            or a.status != "processing"
            or a.lease_owner != lease.attempt.lease_owner
            or a.lease_generation != lease.attempt.lease_generation
            or not a.lease_until
            or a.lease_until <= now()
        ):
            raise IngestError("INGEST_LEASE_LOST")
        if not a.started_at or now() >= a.started_at + timedelta(seconds=DEADLINE_SECONDS):
            raise IngestError("INGEST_DEADLINE_EXCEEDED")
        return a

    async def heartbeat(self, lease: Lease) -> None:
        async with self.locked(lease.document.organization_id, lease.document.id) as (db, row):
            attempt = await self.fence(db, row, lease)
            attempt.lease_until = now() + timedelta(seconds=LEASE_SECONDS)

    async def ensure_capacity(self, db: AsyncSession, org: UUID, additional: int) -> None:
        count = await db.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.organization_id == org,
                Document.deleted_at.is_(None),
                DocumentChunk.text.is_not(None),
            )
        )
        if (count or 0) + additional > self.settings.max_chunks_per_organization:
            raise IngestError("INGEST_LIMIT_EXCEEDED")

    async def publish(
        self,
        lease: Lease,
        chunks: list[Chunk],
        vectors: list[list[float]],
        key: str,
        size: int,
        digest: str,
    ) -> None:
        async with self.locked(lease.document.organization_id, lease.document.id) as (db, row):
            attempt = await self.fence(db, row, lease)
            assert row is not None
            await self.ensure_capacity(db, row.organization_id, len(chunks))
            if len(chunks) != len(vectors):
                raise IngestError("MODEL_RESPONSE_INVALID")
            for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                db.add(
                    DocumentChunk(
                        organization_id=row.organization_id,
                        knowledge_base_id=row.knowledge_base_id,
                        document_id=row.id,
                        attempt_id=attempt.id,
                        ordinal=ordinal,
                        text=chunk.text,
                        text_hash=chunk.digest,
                        locator=chunk.locator,
                        lexemes=func.to_tsvector("simple", lexemes(chunk.text)),
                        embedding=vector,
                        embedding_profile_id=attempt.embedding_profile_id,
                        embedding_dimensions=lease.profile.dimension,
                    )
                )
            attempt.extracted_object_key, attempt.extracted_size = key, size
            attempt.content_hash, attempt.chunk_count = digest, len(chunks)
            attempt.status = row.status = "ready"
            attempt.finished_at = row.ready_at = now()
            attempt.lease_until = None

    async def fail(self, lease: Lease, code: str) -> None:
        async with self.locked(lease.document.organization_id, lease.document.id) as (db, row):
            a = await db.get(DocumentAttempt, lease.attempt.id, with_for_update=True)
            if (
                row
                and not row.deleted_at
                and row.current_attempt_id == lease.attempt.id
                and a
                and a.status == "processing"
                and a.lease_owner == lease.attempt.lease_owner
                and a.lease_generation == lease.attempt.lease_generation
            ):
                row.status = a.status = "failed"
                a.error_code, a.finished_at, a.lease_until = code, now(), None

    async def embed(self, lease: Lease, texts: list[str], batch: int) -> list[list[float]]:
        usage = Attempt(
            number=batch, reserved=sum(len(t.encode()) for t in texts), token_source="reserved"
        )
        async with self.locked(lease.document.organization_id, lease.document.id) as (db, row):
            await self.fence(db, row, lease)
            db.add(
                UsageEntry(
                    id=usage.id,
                    organization_id=lease.document.organization_id,
                    operation_kind="document_embedding",
                    operation_id=lease.attempt.id,
                    attempt_no=batch,
                    model_profile_id=lease.profile.id,
                    reserved_tokens=usage.reserved,
                    token_source="reserved",
                    status="pending",
                )
            )
        started = monotonic()
        try:
            result = await self.embeddings.create(lease.profile, lease.connection, texts, usage)
            if usage.input_tokens is not None and usage.input_tokens > usage.reserved:
                raise IngestError("MODEL_USAGE_EXCEEDED")
            usage.status = "succeeded"
            return result
        finally:
            async with self.sessions.begin() as db:
                entry = await db.get(UsageEntry, usage.id, with_for_update=True)
                assert entry is not None
                entry.status = "succeeded" if usage.status == "succeeded" else "failed"
                entry.input_tokens, entry.output_tokens = usage.input_tokens, usage.output_tokens
                entry.token_source = usage.token_source
                entry.duration_ms = int((monotonic() - started) * 1000)

    async def process(self, lease: Lease) -> None:
        body = await self.storage.get(lease.document.object_key, lease.document.size_bytes)
        parsed = await parser.parse(body, lease.document.content_type)
        chunks = split(parsed)
        async with self.locked(lease.document.organization_id, lease.document.id) as (db, row):
            await self.fence(db, row, lease)
            await self.ensure_capacity(db, lease.document.organization_id, len(chunks))
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), 10):
            vectors.extend(
                await self.embed(
                    lease, [c.text for c in chunks[start : start + 10]], start // 10 + 1
                )
            )
        extracted = parsed["text"].encode("utf-8")
        key = (
            f"{self.settings.s3_prefix}{lease.document.organization_id}/derived/"
            f"{lease.attempt.id}/{lease.attempt.lease_owner}"
        )
        await self.storage.put(key, BytesIO(extracted), len(extracted), "text/plain; charset=utf-8")
        # An uncertain DB commit leaves the object for reference-aware orphan cleanup.
        await self.publish(
            lease, chunks, vectors, key, len(extracted), hashlib.sha256(extracted).hexdigest()
        )

    async def run(self, org: UUID, doc: UUID, attempt_id: UUID) -> None:
        lease = await self.claim(org, doc, attempt_id)
        if not lease:
            return
        task = asyncio.create_task(self.process(lease))
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=5)
                    if not done:
                        await self.heartbeat(lease)
                await task
        except TimeoutError:
            await self.fail(lease, "INGEST_DEADLINE_EXCEEDED")
        except IngestError as exc:
            await self.fail(lease, exc.code)
        except AppError as exc:
            await self.fail(lease, exc.code)
        except Exception:
            await self.fail(lease, "INGEST_FAILED")
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def reconcile(self) -> int:
        async with self.sessions() as db:
            candidates = (
                await db.execute(
                    select(Document.organization_id, Document.id)
                    .where(
                        Document.status.in_(["queued", "processing"]), Document.deleted_at.is_(None)
                    )
                    .order_by(Document.created_at)
                    .limit(100)
                )
            ).all()
        changed = 0
        for org, doc in candidates:
            async with self.locked(org, doc) as (db, row):
                if not row or row.deleted_at:
                    continue
                a = await db.get(DocumentAttempt, row.current_attempt_id, with_for_update=True)
                assert a is not None
                if a.status == "processing" and (not a.lease_until or a.lease_until <= now()):
                    a.error_code = "INGEST_INTERRUPTED"
                    a.status = row.status = "failed"
                    a.finished_at, a.lease_until = now(), None
                    a.lease_generation += 1
                    pending = (
                        await db.scalars(
                            select(UsageEntry).where(
                                UsageEntry.operation_id == a.id,
                                UsageEntry.status == "pending",
                                UsageEntry.organization_id == org,
                            )
                        )
                    ).all()
                    for entry in pending:
                        entry.status = (
                            "failed"  # Unknown provider result retains conservative usage.
                        )
                    changed += 1
                elif a.status == "queued":
                    job = await db.scalar(
                        select(OutboxJob)
                        .where(
                            OutboxJob.resource_id == doc,
                            OutboxJob.resource_version == a.id,
                            OutboxJob.organization_id == org,
                            OutboxJob.job_type == "document.ingest",
                        )
                        .with_for_update()
                    )
                    if (
                        job
                        and job.published_at
                        and job.published_at < now() - timedelta(seconds=30)
                    ):
                        job.published_at, job.available_at, job.publish_attempts = None, now(), 0
                        changed += 1
        return changed
