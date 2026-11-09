"""Authorized projections over durable runs; reads never enqueue work."""

import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.agents import Agent
from backend.models.identity import Organization, now
from backend.models.knowledge import Document, KnowledgeBase
from backend.models.model_settings import OrgSettings
from backend.models.runs import Conversation, Message, Run, RunCitation, RunEvent, RunSource
from backend.repositories.runs import TERMINAL, fail
from backend.run_schemas import (
    ConversationRename,
    ConversationSummary,
    MessageView,
    QAResult,
    RunStatus,
    SourceDetail,
    TaskDocument,
    TaskKnowledge,
    TaskOptions,
)
from backend.schemas import Page
from backend.services.identity import IdentityService, PageCursor
from backend.services.runs import RunService


class DialogueService(RunService):
    async def own_conversation(
        self, identity: IdentityService, org: UUID, cid: UUID
    ) -> Conversation:
        row = await identity.db.scalar(
            select(Conversation)
            .where(
                Conversation.id == cid,
                Conversation.organization_id == org,
                Conversation.owner_user_id == identity.require_user().id,
            )
            .with_for_update()
        )
        if not row:
            raise fail("RESOURCE_NOT_FOUND", 404)
        return row

    async def conversation_summary(
        self, db: AsyncSession, row: Conversation
    ) -> ConversationSummary:
        agent = await db.get(Agent, row.agent_id)
        latest = await db.scalar(
            select(Run)
            .where(Run.conversation_id == row.id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(1)
        )
        return ConversationSummary(
            id=row.id,
            agent_id=row.agent_id,
            task_type="qa",
            title=row.title,
            updated_at=row.updated_at,
            revision=row.revision,
            agent_name=agent.name if agent else "不可用 Agent",
            active_run_id=latest.id if latest and latest.status not in TERMINAL else None,
            last_run_id=latest.id if latest else None,
            last_run_status=cast(RunStatus, latest.status) if latest else None,
        )

    async def conversations(
        self, org: UUID, q: str, cursor: str | None, limit: int
    ) -> Page[ConversationSummary]:
        async with self.user_access(org) as (identity, _):
            organization = await identity.db.get(Organization, org)
            assert organization
            query = select(Conversation).where(
                Conversation.organization_id == org,
                Conversation.owner_user_id == identity.require_user().id,
                Conversation.title.icontains(q, autoescape=True),
            )
            if cursor:
                point = identity.decode_cursor(cursor, org, organization.access_revision)
                query = query.where(
                    tuple_(Conversation.updated_at, Conversation.id) < (point.created_at, point.id)
                )
            rows = list(
                await identity.db.scalars(
                    query.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = identity.encode_cursor(
                    PageCursor(
                        organization_id=org,
                        actor_id=identity.require_user().id,
                        access_revision=organization.access_revision,
                        created_at=last.updated_at,
                        id=last.id,
                    )
                )
            return Page(
                items=[await self.conversation_summary(identity.db, r) for r in rows[:limit]],
                next_cursor=next_cursor,
            )

    async def conversation_detail(
        self, org: UUID, cid: UUID, value: ConversationRename | None = None
    ) -> ConversationSummary:
        async with self.user_access(org, value is not None) as (identity, _):
            row = await self.own_conversation(identity, org, cid)
            if value:
                if row.revision != value.expected_revision:
                    raise fail("VERSION_CONFLICT")
                row.title, row.updated_at = value.title, now()
                row.revision += 1
            return await self.conversation_summary(identity.db, row)

    async def citations(self, db: AsyncSession, run_id: UUID) -> list[dict[str, Any]]:
        rows = (
            await db.execute(
                select(RunCitation, RunSource, Document)
                .join(RunSource, RunSource.id == RunCitation.source_id)
                .join(Document, Document.id == RunSource.document_id)
                .where(RunCitation.run_id == run_id)
                .order_by(RunCitation.label)
            )
        ).all()
        return [
            {
                "sourceId": str(s.id),
                "label": c.label,
                "claimPath": c.claim_path,
                "title": s.title,
                "documentId": str(s.document_id),
                "attemptId": str(s.attempt_id),
                "chunkId": str(s.chunk_id),
                "locator": s.locator,
                "availability": "deleted" if d.deleted_at else "available",
            }
            for c, s, d in rows
        ]

    async def messages(
        self, org: UUID, cid: UUID, cursor: str | None, limit: int
    ) -> Page[MessageView]:
        async with self.user_access(org) as (identity, _):
            await self.own_conversation(identity, org, cid)
            organization = await identity.db.get(Organization, org)
            assert organization
            query = select(Message).where(
                Message.conversation_id == cid, Message.organization_id == org
            )
            if cursor:
                point = identity.decode_cursor(cursor, org, organization.access_revision)
                query = query.where(
                    tuple_(Message.created_at, Message.id) < (point.created_at, point.id)
                )
            rows = list(
                await identity.db.scalars(
                    query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
                )
            )
            items = []
            for row in rows[:limit]:
                citations = (
                    await self.citations(identity.db, row.run_id) if row.role == "assistant" else []
                )
                items.append(
                    MessageView(
                        id=row.id,
                        run_id=row.run_id,
                        role=cast(Any, row.role),
                        content=row.content,
                        is_partial=row.is_partial,
                        result=QAResult.model_validate(row.result_json)
                        if row.result_json
                        else None,
                        citations=citations,
                        created_at=row.created_at,
                        result_visibility="partially_unavailable"
                        if any(c["availability"] == "deleted" for c in citations)
                        else "visible",
                    )
                )
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = identity.encode_cursor(
                    PageCursor(
                        organization_id=org,
                        actor_id=identity.require_user().id,
                        access_revision=organization.access_revision,
                        created_at=last.created_at,
                        id=last.id,
                    )
                )
            return Page(items=items, next_cursor=next_cursor)

    async def source(self, org: UUID, run_id: UUID, source_id: UUID) -> SourceDetail:
        async with self.user_access(org) as (identity, admin):
            await self.visible_run(identity, admin, org, run_id)
            row = (
                await identity.db.execute(
                    select(RunSource, Document)
                    .join(Document, Document.id == RunSource.document_id)
                    .where(
                        RunSource.run_id == run_id,
                        RunSource.id == source_id,
                        RunSource.organization_id == org,
                    )
                )
            ).first()
            if not row:
                raise fail("RESOURCE_NOT_FOUND", 404)
            s, document = row
            return SourceDetail(
                id=s.id,
                title=s.title,
                availability="deleted" if document.deleted_at else "available",
                excerpt=None if document.deleted_at else s.excerpt,
                locator=s.locator,
                document_id=s.document_id,
                attempt_id=s.attempt_id,
                chunk_id=s.chunk_id,
                observed_at=s.observed_at,
                content_hash=s.content_hash,
            )

    async def task_options(self, org: UUID, agent_id: UUID) -> TaskOptions:
        async with self.user_access(org) as (identity, _):
            _, config = await self.usable_agent(identity.db, org, agent_id)
            kbs = list(
                await identity.db.scalars(
                    select(KnowledgeBase)
                    .where(
                        KnowledgeBase.organization_id == org,
                        KnowledgeBase.id.in_([UUID(i) for i in config["knowledgeBaseIds"]]),
                        KnowledgeBase.deleted_at.is_(None),
                    )
                    .order_by(KnowledgeBase.name, KnowledgeBase.id)
                )
            )
            docs = list(
                await identity.db.scalars(
                    select(Document)
                    .where(
                        Document.organization_id == org,
                        Document.knowledge_base_id.in_([k.id for k in kbs]),
                        Document.deleted_at.is_(None),
                        Document.status == "ready",
                    )
                    .order_by(Document.original_name, Document.id)
                )
            )
            settings = await identity.db.get(OrgSettings, org)
            limits = {
                k: min(v, settings.limits.get(k, v)) if settings else v
                for k, v in self.settings.system_limits.model_dump().items()
            }
            return TaskOptions(
                agent_id=agent_id,
                version_number=config["versionNumber"],
                limits=limits,
                knowledge_bases=[TaskKnowledge(id=k.id, name=k.name) for k in kbs],
                documents=[
                    TaskDocument(
                        id=d.id,
                        knowledge_base_id=d.knowledge_base_id,
                        title=d.original_name,
                        attempt_id=d.current_attempt_id,
                    )
                    for d in docs
                ],
            )

    @staticmethod
    def event_cursor(run_id: UUID, value: str | None) -> int:
        if value is None:
            return 0
        prefix, sep, seq = value.rpartition(":")
        if (
            not sep
            or prefix != str(run_id)
            or not seq.isascii()
            or not seq.isdecimal()
            or len(seq) > 18
        ):
            raise fail("EVENT_CURSOR_INVALID", 422)
        return int(seq)

    async def event_batch(
        self, org: UUID, run_id: UUID, cursor: int
    ) -> tuple[list[dict[str, Any]], bool]:
        async with self.user_access(org) as (identity, admin):
            run = await self.visible_run(identity, admin, org, run_id)
            if cursor > run.last_seq:
                raise fail("EVENT_CURSOR_INVALID", 422)
            first = await identity.db.scalar(
                select(func.min(RunEvent.seq)).where(RunEvent.run_id == run_id)
            )
            if cursor < run.last_seq and (first is None or cursor < first - 1):
                raise fail("EVENT_CURSOR_EXPIRED")
            rows = list(
                await identity.db.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                    .order_by(RunEvent.seq)
                    .limit(100)
                )
            )
            if any(r.seq != cursor + i + 1 for i, r in enumerate(rows)):
                raise fail("EVENT_CURSOR_EXPIRED")
            values = [
                {
                    "runId": str(run_id),
                    "seq": r.seq,
                    "type": r.type,
                    "occurredAt": r.occurred_at.isoformat(),
                    "data": r.data,
                }
                for r in rows
            ]
            return values, run.status in TERMINAL and (
                rows[-1].seq if rows else cursor
            ) == run.last_seq

    async def export(self, org: UUID, run_id: UUID) -> str:
        async with self.user_access(org) as (identity, admin):
            run = await self.visible_run(identity, admin, org, run_id)
            message = await identity.db.scalar(
                select(Message).where(Message.run_id == run_id, Message.role == "assistant")
            )
            if run.status != "succeeded" or not message or not message.result_json:
                raise fail("EXPORT_NOT_AVAILABLE")
            result = QAResult.model_validate(message.result_json)
            citations = await self.citations(identity.db, run_id)
            lines = [
                "# 文档问答",
                f"生成时间：{run.finished_at.isoformat() if run.finished_at else ''}（UTC）",
                f"运行：{run.id}",
                "## 问题",
                safe_markdown(run.input_snapshot["question"]),
                "## 范围",
                f"Agent 版本：{run.config_snapshot['versionNumber']}",
                "知识库：" + ", ".join(run.input_snapshot["knowledgeBaseIds"]),
            ]
            for doc, attempt in run.config_snapshot["documents"].items():
                lines.append(f"- 文档 {doc} / 处理版本 {attempt}")
            lines += ["## 答案", safe_markdown(result.answer), "## 证据对应"]
            for group in ("claims", "conflicts"):
                for index, claim in enumerate(getattr(result, group)):
                    labels = " ".join(
                        f"[{c['label']}]" for c in citations if c["claimPath"] == f"{group}.{index}"
                    )
                    lines.append(f"- {safe_markdown(claim.text)} {labels}（{claim.kind}）")
            lines += [
                "## 资料不足",
                *[safe_markdown(t) for t in result.missing_information],
                "## 来源",
            ]
            for c in citations:
                lines.append(
                    f"[{c['label']}] {safe_markdown(c['title'])} / 文档 {c['documentId']}"
                    f" / 版本 {c['attemptId']} / 片段 {c['chunkId']}"
                    f" / {safe_markdown(json.dumps(c['locator'], ensure_ascii=False))}"
                    + ("（来源已删除，原文不可用）" if c["availability"] == "deleted" else "")
                )
            lines += [
                "## 覆盖与限制",
                safe_markdown(json.dumps(run.coverage, ensure_ascii=False, indent=2)),
                "结果仅基于上述范围内实际检索到的证据，不代表全部资料；推断与建议需人工确认。",
            ]
            return "\n\n".join(lines) + "\n"


def safe_markdown(value: str) -> str:
    # Escape all untrusted Markdown syntax, HTML and auto-link punctuation.
    return "".join("\\" + ch if ch in "\\`*_{}[]<>()#+-.!|:~" else ch for ch in value)
