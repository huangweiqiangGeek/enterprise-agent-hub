from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.agents import Agent, AgentVersion, AgentVersionKB, AgentVersionTool
from backend.models.knowledge import Document, DocumentAttempt, DocumentChunk, KnowledgeBase
from backend.models.model_settings import ModelConnection
from backend.services.errors import AppError


class AgentRepository:
    def __init__(self, db: AsyncSession, org: UUID):
        self.db, self.org = db, org

    async def agent(self, agent_id: UUID) -> Agent:
        row = await self.db.scalar(
            select(Agent).where(Agent.organization_id == self.org, Agent.id == agent_id)
        )
        if not row:
            raise AppError(404, "NOT_FOUND", "Agent不存在或不可见。")
        return row

    async def version(self, row: Agent) -> AgentVersion:
        version = await self.db.scalar(
            select(AgentVersion).where(
                AgentVersion.organization_id == self.org,
                AgentVersion.agent_id == row.id,
                AgentVersion.id == row.current_version_id,
            )
        )
        assert version is not None
        return version

    async def bindings(self, version_id: UUID) -> tuple[list[UUID], list[str]]:
        kbs = list(
            await self.db.scalars(
                select(AgentVersionKB.knowledge_base_id)
                .where(
                    AgentVersionKB.organization_id == self.org,
                    AgentVersionKB.agent_version_id == version_id,
                )
                .order_by(AgentVersionKB.knowledge_base_id)
            )
        )
        tools = list(
            await self.db.scalars(
                select(AgentVersionTool.tool_key)
                .where(
                    AgentVersionTool.organization_id == self.org,
                    AgentVersionTool.agent_version_id == version_id,
                )
                .order_by(AgentVersionTool.tool_key)
            )
        )
        return kbs, tools

    async def valid_kbs(self, ids: list[UUID]) -> list[UUID]:
        return list(
            await self.db.scalars(
                select(KnowledgeBase.id)
                .where(
                    KnowledgeBase.organization_id == self.org,
                    KnowledgeBase.id.in_(ids),
                    KnowledgeBase.deleted_at.is_(None),
                )
                .order_by(KnowledgeBase.id)
            )
        )

    async def chat(self, connection_id: UUID | None) -> ModelConnection | None:
        if connection_id is None:
            return None
        return (
            await self.db.scalars(
                select(ModelConnection).where(
                    ModelConnection.organization_id == self.org,
                    ModelConnection.id == connection_id,
                    ModelConnection.kind == "chat",
                )
            )
        ).one_or_none()

    async def ready_document(self, ids: list[UUID], profile: UUID) -> bool:
        # Current publication only: old/failed attempts cannot make a configuration ready.
        return bool(
            await self.db.scalar(
                select(Document.id)
                .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
                .join(DocumentAttempt, DocumentAttempt.id == Document.current_attempt_id)
                .join(DocumentChunk, DocumentChunk.attempt_id == DocumentAttempt.id)
                .where(
                    Document.organization_id == self.org,
                    KnowledgeBase.organization_id == self.org,
                    DocumentAttempt.organization_id == self.org,
                    DocumentChunk.organization_id == self.org,
                    Document.knowledge_base_id.in_(ids),
                    KnowledgeBase.deleted_at.is_(None),
                    Document.deleted_at.is_(None),
                    Document.status == "ready",
                    DocumentAttempt.status == "ready",
                    DocumentChunk.embedding_profile_id == profile,
                    DocumentChunk.embedding.is_not(None),
                )
                .limit(1)
            )
        )
