"""Authorized retrieval orchestration; network calls never hold organization locks."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models.knowledge import Document
from backend.models.model_settings import ConnectionVersion, ModelProfile, UsageEntry
from backend.rag.documents import IngestError, lexemes
from backend.rag.embeddings import Embeddings
from backend.rag.rerank import Rerank
from backend.rag.retrieval import excerpt, fuse
from backend.repositories.knowledge import KnowledgeRepository
from backend.repositories.model_settings import ModelRepository
from backend.repositories.retrieval import RetrievalRepository
from backend.retrieval_schemas import (
    RetrievalCoverage,
    RetrievalItem,
    RetrievalResult,
    SearchPreview,
)
from backend.services.errors import AppError
from backend.services.knowledge import KnowledgeService
from backend.services.model_probe import Attempt, ModelProbe

Model = tuple[ModelProfile, ConnectionVersion]


@dataclass
class Scope:
    documents: dict[UUID, UUID]
    embedding: Model
    rerank: Model


class RetrievalService:
    def __init__(self, knowledge: KnowledgeService, probe: ModelProbe):
        self.knowledge = knowledge
        self.embeddings = Embeddings(knowledge.settings, probe.transport)
        self.rerank = Rerank(knowledge.settings, probe.transport)
        self.sessions = async_sessionmaker(knowledge.engine, expire_on_commit=False)

    async def model(self, db: AsyncSession, org: UUID, kind: str) -> Model:
        repo = ModelRepository(db, org)
        connection = await repo.connection(kind)
        if not connection or not connection.enabled:
            raise AppError(409, "MODEL_NOT_CONFIGURED", "请先配置并测试 Embedding 和 Rerank 模型。")
        return await repo.versions(connection)

    async def documents(
        self, db: AsyncSession, org: UUID, kb: UUID, selected: list[UUID]
    ) -> dict[UUID, UUID]:
        repo = KnowledgeRepository(db, org)
        await repo.knowledge_base(kb)
        query = select(Document).where(
            Document.organization_id == org,
            Document.knowledge_base_id == kb,
            Document.deleted_at.is_(None),
        )
        if selected:
            rows = (await db.scalars(query.where(Document.id.in_(selected)))).all()
            if len(rows) != len(selected):
                raise AppError(404, "NOT_FOUND", "所选文档不存在或不可见。")
            if any(row.status != "ready" for row in rows):
                raise AppError(409, "DOCUMENT_NOT_READY", "请选择已就绪的文档。")
        else:
            rows = (await db.scalars(query.where(Document.status == "ready"))).all()
        return {row.id: row.current_attempt_id for row in rows}

    async def check(self, db: AsyncSession, org: UUID, kb: UUID, scope: Scope) -> None:
        documents = await self.documents(db, org, kb, list(scope.documents))
        if documents != scope.documents:
            raise AppError(409, "RETRIEVAL_SCOPE_CHANGED", "文档处理版本已改变，请重新检索。")
        for kind, previous in (("embedding", scope.embedding), ("rerank", scope.rerank)):
            current = await self.model(db, org, kind)
            if tuple(v.id for v in current) != tuple(v.id for v in previous):
                raise AppError(409, "MODEL_CONFIG_CHANGED", "模型配置已改变，请重新检索。")

    def repository(
        self, db: AsyncSession, org: UUID, kb: UUID, scope: Scope
    ) -> RetrievalRepository:
        profile, _ = scope.embedding
        if not profile.dimension:
            raise AppError(409, "MODEL_CONFIG_INVALID", "Embedding 维度无效，请核对模型配置。")
        return RetrievalRepository(
            db, org, kb, list(scope.documents), profile.id, profile.dimension
        )

    async def finish_usage(self, usage: Attempt, started: float) -> None:
        async with self.sessions.begin() as db:
            row = await db.get(UsageEntry, usage.id, with_for_update=True)
            assert row is not None
            row.status = usage.status if usage.status == "succeeded" else "failed"
            row.input_tokens, row.output_tokens = usage.input_tokens, usage.output_tokens
            row.token_source, row.billed_units = usage.token_source, usage.billed_units
            row.duration_ms = int((monotonic() - started) * 1000)

    @asynccontextmanager
    async def call(
        self,
        org: UUID,
        kb: UUID,
        scope: Scope,
        operation: UUID,
        profile: ModelProfile,
        usage: Attempt,
    ) -> AsyncIterator[None]:
        async with self.knowledge.access(org, write=True) as (identity, _, _):
            await self.check(identity.db, org, kb, scope)
            identity.db.add(
                UsageEntry(
                    id=usage.id,
                    organization_id=org,
                    operation_kind="search_preview",
                    operation_id=operation,
                    attempt_no=usage.number,
                    model_profile_id=profile.id,
                    reserved_tokens=usage.reserved,
                    token_source=usage.token_source,
                    status="pending",
                )
            )
        started = monotonic()
        try:
            yield
            usage.status = "succeeded"
        finally:
            # Cancellation/deadline cannot erase an already dispatched, possibly billed call.
            await asyncio.shield(self.finish_usage(usage, started))

    async def search(self, org: UUID, kb: UUID, value: SearchPreview) -> RetrievalResult:
        try:
            async with asyncio.timeout(15):
                return await self.execute(org, kb, value)
        except TimeoutError:
            raise AppError(
                503, "RETRIEVAL_TIMEOUT", "检索超过15秒，请稍后重试或缩小范围。"
            ) from None
        except IngestError as exc:
            raise AppError(503, exc.code, "检索模型调用失败，请检查模型配置或稍后重试。") from None

    async def execute(self, org: UUID, kb: UUID, value: SearchPreview) -> RetrievalResult:
        operation = uuid4()
        coverage = RetrievalCoverage()
        result = RetrievalResult(
            operation_id=operation,
            query=value.query,
            searched_document_ids=[],
            items=[],
            coverage=coverage,
            usage=[],
        )
        async with self.knowledge.access(org, write=True) as (identity, _, _):
            docs = await self.documents(identity.db, org, kb, value.document_ids)
            if len(value.query.encode()) > 4000:
                raise AppError(422, "QUERY_TOO_LARGE", "查询超过模型保守输入上限，请缩短后重试。")
            if not docs:
                return result
            scope = Scope(
                docs,
                await self.model(identity.db, org, "embedding"),
                await self.model(identity.db, org, "rerank"),
            )
            counts = await self.repository(identity.db, org, kb, scope).counts()
        terms = lexemes(value.query).split()
        coverage.query_terms_truncated = len(terms) > 64
        embedding_usage = Attempt(1, reserved=len(value.query.encode()), token_source="reserved")
        async with self.call(org, kb, scope, operation, scope.embedding[0], embedding_usage):
            vector = (
                await self.embeddings.create(*scope.embedding, [value.query], embedding_usage)
            )[0]
        result.usage.append(embedding_usage.dto())
        async with self.knowledge.access(org, write=True) as (identity, _, _):
            await self.check(identity.db, org, kb, scope)
            rows = await self.repository(identity.db, org, kb, scope).search(vector, terms[:64])
        candidates = fuse(rows)
        coverage.eligible_document_count = len(counts)
        coverage.eligible_chunk_count = sum(counts.values())
        result.searched_document_ids = sorted(counts, key=str)
        coverage.vector_count = sum(r["branch"] == "vector" for r in rows)
        coverage.keyword_count = sum(r["branch"] == "keyword" for r in rows)
        coverage.candidate_count = len(candidates)
        if candidates:
            excerpts = [excerpt(row["text"], value.query) for row in candidates]
            clipped = [
                row for row, text in zip(candidates, excerpts, strict=True) if text != row["text"]
            ]
            coverage.excerpt_truncated_count = len(clipped)
            coverage.excerpt_truncated_document_count = len({r["document_id"] for r in clipped})
            rerank_usage = Attempt(2)
            async with self.call(org, kb, scope, operation, scope.rerank[0], rerank_usage):
                ranking = await self.rerank.rank(*scope.rerank, value.query, excerpts, rerank_usage)
            result.usage.append(rerank_usage.dto())
            for index, score in ranking:
                row = candidates[index]
                # Return a contiguous original prefix only when the evidence budget is exhausted.
                available = max(0, 4000 - coverage.evidence_bytes)
                text = row["text"].encode()[:available].decode("utf-8", errors="ignore")
                if text != row["text"]:
                    coverage.context_truncated_count += 1
                if not text:
                    continue
                result.items.append(
                    RetrievalItem(
                        chunk_id=row["chunk_id"],
                        document_id=row["document_id"],
                        attempt_id=row["attempt_id"],
                        file_name=row["file_name"],
                        text=text,
                        locator=row["locator"],
                        score=score,
                    )
                )
                coverage.evidence_bytes += len(text.encode())
        coverage.returned_count = len(result.items)
        coverage.omitted_chunk_count = max(0, coverage.eligible_chunk_count - len(result.items))
        coverage.omitted_document_count = len(set(counts) - {r.document_id for r in result.items})
        coverage.partial = bool(
            coverage.omitted_chunk_count
            or coverage.excerpt_truncated_count
            or coverage.context_truncated_count
            or coverage.query_terms_truncated
        )
        # Fresh session/role/resource validation also covers a valid empty candidate result.
        async with self.knowledge.access(org, write=True) as (identity, _, _):
            await self.check(identity.db, org, kb, scope)
        return result
