"""Bounded retention maintenance. Business history and idempotency keys never expire here."""

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Text, cast, delete, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.checkpoints import checkpoint_saver
from backend.config import Settings
from backend.models.identity import AuditEntry, AuthSession, Organization, now
from backend.models.knowledge import DocumentAttempt, DocumentChunk, OutboxJob
from backend.models.runs import Run, RunEvent, RunSource
from backend.repositories.runs import TERMINAL, RunRepository


async def active_reference(
    db: AsyncSession, org: UUID, doc: UUID, attempt: UUID | None = None
) -> bool:
    """Both accepted snapshots and persisted evidence protect data during execution/recovery."""
    snapshot = (
        Run.config_snapshot["documents"].op("?")(str(doc))
        if attempt is None
        else Run.config_snapshot["documents"][str(doc)].astext == str(attempt)
    )
    source = exists(
        select(RunSource.id).where(
            RunSource.run_id == Run.id,
            RunSource.organization_id == org,
            RunSource.document_id == doc,
            *([RunSource.attempt_id == attempt] if attempt else []),
        )
    )
    return bool(
        await db.scalar(
            select(Run.id)
            .where(
                Run.organization_id == org,
                Run.status.in_(["queued", "running"]),
                or_(snapshot, source),
            )
            .limit(1)
        )
    )


class Lifecycle:
    def __init__(self, engine: AsyncEngine, settings: Settings):
        self.settings = settings
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def expired_identity(self, limit: int) -> dict[str, int]:
        async with self.sessions.begin() as db:
            sessions = list(
                await db.scalars(
                    select(AuthSession.id)
                    .where(
                        or_(
                            AuthSession.expires_at < now() - timedelta(days=7),
                            AuthSession.revoked_at < now() - timedelta(days=7),
                        )
                    )
                    .order_by(AuthSession.expires_at, AuthSession.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            audits = list(
                await db.scalars(
                    select(AuditEntry.id)
                    .where(AuditEntry.occurred_at < now() - timedelta(days=30))
                    .order_by(AuditEntry.occurred_at, AuditEntry.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            if sessions:
                await db.execute(delete(AuthSession).where(AuthSession.id.in_(sessions)))
            if audits:
                await db.execute(delete(AuditEntry).where(AuditEntry.id.in_(audits)))
            return {"sessions": len(sessions), "audits": len(audits)}

    async def failed_attempts(self, limit: int) -> int:
        async with self.sessions() as db:
            ids = list(
                (
                    await db.execute(
                        select(
                            DocumentAttempt.organization_id,
                            DocumentAttempt.document_id,
                            DocumentAttempt.id,
                        )
                        .where(
                            DocumentAttempt.status.in_(["failed", "cancelled"]),
                            ~exists(
                                select(Run.id).where(
                                    Run.organization_id == DocumentAttempt.organization_id,
                                    Run.status.in_(["queued", "running"]),
                                    func.jsonb_extract_path_text(
                                        Run.config_snapshot,
                                        "documents",
                                        cast(DocumentAttempt.document_id, Text),
                                    )
                                    == cast(DocumentAttempt.id, Text),
                                )
                            ),
                            ~exists(
                                select(RunSource.id)
                                .join(Run, Run.id == RunSource.run_id)
                                .where(
                                    RunSource.attempt_id == DocumentAttempt.id,
                                    RunSource.organization_id == DocumentAttempt.organization_id,
                                    Run.status.in_(["queued", "running"]),
                                )
                            ),
                            DocumentAttempt.finished_at < now() - timedelta(hours=24),
                            or_(
                                DocumentAttempt.lease_until.is_(None),
                                DocumentAttempt.lease_until < now(),
                            ),
                            or_(
                                DocumentAttempt.extracted_object_key.is_not(None),
                                exists(
                                    select(DocumentChunk.id).where(
                                        DocumentChunk.attempt_id == DocumentAttempt.id,
                                        DocumentChunk.text.is_not(None),
                                    )
                                ),
                            ),
                            ~exists(
                                select(OutboxJob.id).where(
                                    OutboxJob.job_type == "attempt.cleanup",
                                    OutboxJob.resource_version == DocumentAttempt.id,
                                )
                            ),
                        )
                        .order_by(DocumentAttempt.finished_at, DocumentAttempt.id)
                        .limit(limit)
                    )
                ).all()
            )
        enqueued = 0
        for org, doc, aid in ids:
            async with self.sessions.begin() as db:
                await db.get(Organization, org, with_for_update=True)
                attempt = await db.get(DocumentAttempt, aid, with_for_update=True)
                if (
                    not attempt
                    or attempt.status not in {"failed", "cancelled"}
                    or not attempt.finished_at
                    or attempt.finished_at >= now() - timedelta(hours=24)
                    or (attempt.lease_until and attempt.lease_until >= now())
                    or await active_reference(db, org, doc, aid)
                ):
                    continue
                prior = await db.scalar(
                    select(OutboxJob.id).where(
                        OutboxJob.job_type == "attempt.cleanup", OutboxJob.resource_version == aid
                    )
                )
                if not prior:
                    db.add(
                        OutboxJob(
                            id=uuid4(),
                            organization_id=org,
                            job_type="attempt.cleanup",
                            resource_id=doc,
                            resource_version=aid,
                            payload={
                                "organizationId": str(org),
                                "documentId": str(doc),
                                "attemptId": str(aid),
                            },
                        )
                    )
                    enqueued += 1
        return enqueued

    async def expired_runs(
        self, limit: int, cursor: str | None = None, request_id: str | None = None
    ) -> dict[str, Any]:
        # A UUID cursor bounds examination too: already-cleaned histories cannot starve later runs.
        async with self.sessions() as db:
            query = select(Run.organization_id, Run.id).where(
                Run.status.in_(TERMINAL),
                Run.finished_at < now() - timedelta(days=7),
                or_(Run.lease_until.is_(None), Run.lease_until < now()),
            )
            if cursor:
                query = query.where(Run.id > UUID(cursor))
            candidates = list((await db.execute(query.order_by(Run.id).limit(limit + 1))).all())
            checkpoint_ready = bool(
                await db.scalar(text("SELECT to_regclass('checkpoints.checkpoints')"))
            )
        cleaned = failed = 0
        for org, rid in candidates[:limit]:
            async with self.sessions.begin() as db:
                run = await RunRepository(db, org).lock(rid)
                if (
                    run.status not in TERMINAL
                    or not run.finished_at
                    or run.finished_at >= now() - timedelta(days=7)
                    or (run.lease_until and run.lease_until >= now())
                ):
                    continue
                prefix = f"{org}:{run.conversation_id}:{run.id}"
                generation = run.lease_generation
            try:
                # Terminal runs cannot be reactivated. No network calls under the business lock.
                if checkpoint_ready:
                    async with checkpoint_saver(self.settings) as saver:
                        await saver.adelete_thread(prefix)  # pre-generation qa-v1/v2 histories
                        for number in range(1, min(generation, 3) + 1):
                            # RunState.reconcile fences recovery at generation >= 3.
                            await saver.adelete_thread(f"{prefix}:g{number}")
                async with self.sessions.begin() as db:
                    run = await RunRepository(db, org).lock(rid)
                    if run.status not in TERMINAL or (run.lease_until and run.lease_until >= now()):
                        continue
                    await db.execute(
                        delete(RunEvent).where(
                            RunEvent.organization_id == org, RunEvent.run_id == rid
                        )
                    )
                cleaned += 1
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "RETENTION_RUN_FAILED request_id=%s run_id=%s exception_type=%s",
                    request_id,
                    rid,
                    type(exc).__name__,
                )
                # The next pass retries; diagnostics never include checkpoint content or secrets.
                failed += 1
        return {
            "runsExamined": min(len(candidates), limit),
            "runsCleaned": cleaned,
            "runCleanupFailed": failed,
            "nextCursor": str(candidates[limit - 1][1]) if len(candidates) > limit else None,
        }

    async def sweep(self, limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("Maintenance batch must be between 1 and 100")
        request_id = str(uuid4())
        expired = await self.expired_runs(limit, cursor, request_id)
        failed = await self.failed_attempts(limit)
        identity = await self.expired_identity(limit)
        async with self.sessions() as db:
            exhausted = await db.scalar(
                select(func.count())
                .select_from(OutboxJob)
                .where(
                    OutboxJob.job_type.in_(["document.cleanup", "attempt.cleanup"]),
                    OutboxJob.published_at.is_(None),
                    OutboxJob.publish_attempts >= 5,
                )
            )
        return {
            "requestId": request_id,
            **expired,
            **identity,
            "attemptCleanupEnqueued": failed,
            "cleanupExhausted": exhausted or 0,
        }
