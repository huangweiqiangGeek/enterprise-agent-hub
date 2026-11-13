"""Run locks and authorization shared by HTTP and worker consumers."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.agents import Agent, AgentVersion
from backend.models.identity import Membership, Organization, now
from backend.models.knowledge import Document, KnowledgeBase
from backend.models.model_settings import ModelConnection
from backend.models.runs import Message, Run, RunEvent, RunSource
from backend.services.errors import AppError

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


def fail(code: str, status: int = 409) -> AppError:
    return AppError(status, code, "任务无法继续，请核对权限、配置和执行记录。")


class RunRepository:
    def __init__(self, db: AsyncSession, org: UUID):
        self.db, self.org = db, org

    async def lock(self, run_id: UUID) -> Run:
        # Same lock order as settings, membership removal and acceptance.
        await self.db.scalar(
            select(Organization).where(Organization.id == self.org).with_for_update()
        )
        run = await self.db.scalar(
            select(Run).where(Run.id == run_id, Run.organization_id == self.org).with_for_update()
        )
        if not run:
            raise fail("RESOURCE_NOT_FOUND", 404)
        return run

    async def authorize(self, run: Run) -> None:
        member = await self.db.scalar(
            select(Membership).where(
                Membership.organization_id == self.org, Membership.user_id == run.initiator_id
            )
        )
        version = await self.db.get(AgentVersion, run.agent_version_id)
        agent = await self.db.get(Agent, version.agent_id) if version else None
        if not member or not agent or agent.organization_id != self.org or not agent.enabled:
            raise fail("PERMISSION_REVOKED", 403)
        for kind in ("chat", "embedding", "rerank"):
            connection = await self.db.get(
                ModelConnection, UUID(run.config_snapshot[kind]["connectionId"])
            )
            if not connection or connection.organization_id != self.org or not connection.enabled:
                raise fail("PERMISSION_REVOKED", 403)
        ids = [
            UUID(value)
            for value in run.config_snapshot.get(
                "knowledgeBaseIds", run.input_snapshot.get("knowledgeBaseIds", [])
            )
        ]
        available = list(
            await self.db.scalars(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.organization_id == self.org,
                    KnowledgeBase.id.in_(ids),
                    KnowledgeBase.deleted_at.is_(None),
                )
            )
        )
        if set(available) != set(ids):
            raise fail("REQUIRED_SOURCE_UNAVAILABLE")
        if run.config_snapshot.get("taskType") == "compare":
            expected = run.config_snapshot["documents"]
            documents = list(
                await self.db.scalars(
                    select(Document).where(
                        Document.organization_id == self.org,
                        Document.id.in_([UUID(d) for d in expected]),
                        Document.deleted_at.is_(None),
                        Document.status == "ready",
                    )
                )
            )
            if {str(d.id): str(d.current_attempt_id) for d in documents} != expected:
                raise fail("REQUIRED_SOURCE_UNAVAILABLE")

    async def active(self, run: Run, generation: int) -> None:
        if (
            run.status != "running"
            or run.lease_generation != generation
            or not run.lease_until
            or run.lease_until <= now()
        ):
            raise fail("RUN_INTERRUPTED")
        if run.cancel_requested:
            raise fail("RUN_CANCELLED")
        if not run.deadline_at or run.deadline_at <= now():
            raise fail("RUN_DEADLINE_EXCEEDED")
        await self.authorize(run)
        history_ids = [UUID(value) for value in run.config_snapshot.get("historyMessageIds", [])]
        if history_ids:
            previous = await self.db.scalars(
                select(Run)
                .join(Message, Message.run_id == Run.id)
                .where(Message.organization_id == self.org, Message.id.in_(history_ids))
                .distinct()
            )
            for row in previous:
                if not await self.sources_valid(row):
                    raise fail("REQUIRED_SOURCE_UNAVAILABLE")

    async def sources_valid(self, run: Run) -> bool:
        rows = (
            await self.db.execute(
                select(RunSource, Document)
                .join(Document, Document.id == RunSource.document_id)
                .where(RunSource.organization_id == self.org, RunSource.run_id == run.id)
            )
        ).all()
        return all(
            not doc.deleted_at
            and doc.status == "ready"
            and doc.current_attempt_id == source.attempt_id
            for source, doc in rows
        )

    def event(self, run: Run, kind: str, data: dict[str, Any]) -> None:
        run.last_seq += 1
        self.db.add(
            RunEvent(
                organization_id=self.org,
                run_id=run.id,
                seq=run.last_seq,
                type=kind,
                data={"schemaVersion": 1, **data},
            )
        )
