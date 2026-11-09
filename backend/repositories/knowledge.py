from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.knowledge import Document, DocumentAttempt, KnowledgeBase
from backend.services.errors import AppError


class KnowledgeRepository:
    def __init__(self, db: AsyncSession, org_id: UUID) -> None:
        self.db, self.org_id = db, org_id

    async def knowledge_base(self, kb_id: UUID) -> KnowledgeBase:
        row = await self.db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.organization_id == self.org_id,
                KnowledgeBase.deleted_at.is_(None),
            )
        )
        if not row:
            raise AppError(404, "NOT_FOUND", "知识库不存在或不可见。")
        return row

    async def document(self, doc_id: UUID, *, include_deleted: bool = False) -> Document:
        query = (
            select(Document)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .where(
                Document.id == doc_id,
                Document.organization_id == self.org_id,
                KnowledgeBase.organization_id == self.org_id,
            )
        )
        if not include_deleted:
            query = query.where(Document.deleted_at.is_(None), KnowledgeBase.deleted_at.is_(None))
        row = await self.db.scalar(query)
        if not row:
            raise AppError(404, "NOT_FOUND", "文档不存在或不可见。")
        return row

    async def attempt(self, row: Document) -> DocumentAttempt:
        result = await self.db.scalar(
            select(DocumentAttempt).where(
                DocumentAttempt.id == row.current_attempt_id,
                DocumentAttempt.document_id == row.id,
                DocumentAttempt.organization_id == self.org_id,
            )
        )
        if not result:
            raise AppError(503, "SERVICE_NOT_READY", "文档记录暂时不可用。")
        return result

    async def bindings(self, kb_id: UUID) -> list[dict[str, str]]:
        # D07 owns these tables. Check the designed current-version join without inventing Agents.
        if not await self.db.scalar(
            text("SELECT to_regclass('app.agent_version_kbs') IS NOT NULL")
        ):
            return []
        rows = await self.db.execute(
            text(
                "SELECT a.id,a.name FROM app.agents a JOIN app.agent_version_kbs k "
                "ON k.agent_version_id=a.current_version_id "
                "AND k.organization_id=a.organization_id "
                "WHERE a.organization_id=:org AND k.knowledge_base_id=:kb"
            ),
            {"org": self.org_id, "kb": kb_id},
        )
        return [{"id": str(r.id), "name": r.name} for r in rows]
