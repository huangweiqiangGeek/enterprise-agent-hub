import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import func, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.config import Settings
from backend.knowledge_schemas import (
    AcceptanceDTO,
    AttemptDTO,
    ChunkDTO,
    ChunkPreview,
    DocumentDetail,
    DocumentDTO,
    DocumentStatus,
    KnowledgeDTO,
    KnowledgeUpdate,
    KnowledgeWrite,
    TextPage,
    UploadPolicy,
)
from backend.models.identity import AuditEntry, IdempotencyKey, Organization, now
from backend.models.knowledge import (
    Document,
    DocumentAttempt,
    DocumentChunk,
    KnowledgeBase,
    OutboxJob,
)
from backend.models.model_settings import OrgSettings
from backend.rag.documents import PIPELINE
from backend.repositories.knowledge import KnowledgeRepository
from backend.repositories.model_settings import ModelRepository
from backend.schemas import Page
from backend.services.errors import AppError
from backend.services.identity import IdentityService, PageCursor
from backend.services.model_settings import SessionProof
from backend.services.storage import Storage
from backend.services.upload import Upload

INGEST_ERRORS = {
    "PDF_ENCRYPTED": "PDF 已加密，请上传未加密版本。",
    "PDF_INVALID": "PDF 已损坏或结构无效，请重新导出。",
    "DOCUMENT_NO_TEXT": "文件中没有可提取文本；扫描 PDF 暂不支持 OCR。",
    "DOCUMENT_ENCODING_INVALID": "文本编码不是 UTF-8，请转换后重新上传。",
    "INGEST_LIMIT_EXCEEDED": "文档超过解析资源或片段数量限制，请拆分后上传。",
    "INGEST_PARSE_TIMEOUT": "文档解析超时，请拆分或重新导出。",
    "INGEST_INTERRUPTED": "处理进程中断，请重试以创建新的处理版本。",
    "INGEST_DEADLINE_EXCEEDED": "文档处理超过十分钟，请拆分或重试。",
    "MODEL_AUTH_FAILED": "Embedding 凭据失效，请更新模型配置后重试。",
    "MODEL_RATE_LIMITED": "Embedding 服务限流，请稍后重试。",
    "MODEL_TIMEOUT": "Embedding 服务响应超时，请稍后重试。",
    "MODEL_UNAVAILABLE": "Embedding 服务暂时不可用，请稍后重试。",
    "EMBEDDING_DIMENSION_INVALID": "Embedding 返回维度或数值不正确，请核对模型配置。",
}


class KnowledgeService:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        request_id: str,
        proof: SessionProof,
        storage: Storage,
    ) -> None:
        self.engine, self.settings, self.request_id = engine, settings, request_id
        self.proof, self.storage = proof, storage

    @asynccontextmanager
    async def access(
        self, org_id: UUID, *, write: bool = False
    ) -> AsyncIterator[tuple[IdentityService, Organization, bool]]:
        async with IdentityService.open(self.engine, self.settings, self.request_id) as identity:
            await identity.authenticate(self.proof.token, self.proof.csrf_cookie)
            if write:
                identity.check_csrf(self.proof.origin, self.proof.csrf_header)
            org, member = await identity.organization(org_id)
            if write:
                identity.require_role(org, member, {"owner", "admin"})
            yield identity, org, identity.role(org, member) in {"owner", "admin"}

    async def limits(self, identity: IdentityService, org_id: UUID) -> dict[str, int]:
        row = await identity.db.get(OrgSettings, org_id)
        return {
            key: min(row.limits.get(key, value), value) if row else value
            for key, value in self.settings.system_limits.model_dump().items()
        }

    async def policy(self, org_id: UUID, kb_id: UUID, *, write: bool = False) -> UploadPolicy:
        async with self.access(org_id, write=write) as (identity, _, _):
            await KnowledgeRepository(identity.db, org_id).knowledge_base(kb_id)
            model = await ModelRepository(identity.db, org_id).connection("embedding")
            return UploadPolicy(
                max_bytes=(await self.limits(identity, org_id))["max_upload_bytes"],
                embedding_ready=bool(model and model.enabled),
                max_documents=self.settings.max_documents_per_organization,
            )

    async def kb_dto(self, identity: IdentityService, row: KnowledgeBase) -> KnowledgeDTO:
        counts = {
            status: count
            for status, count in (
                await identity.db.execute(
                    select(Document.status, func.count())
                    .where(
                        Document.organization_id == row.organization_id,
                        Document.knowledge_base_id == row.id,
                        Document.deleted_at.is_(None),
                    )
                    .group_by(Document.status)
                )
            ).all()
        }
        return KnowledgeDTO(
            id=row.id,
            name=row.name,
            description=row.description,
            document_count=sum(counts.values()),
            ready_count=counts.get("ready", 0),
            processing_count=counts.get("queued", 0) + counts.get("processing", 0),
            failed_count=counts.get("failed", 0),
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def document_dto(
        self, repo: KnowledgeRepository, row: Document, admin: bool
    ) -> DocumentDTO:
        attempt = await repo.attempt(row)
        return DocumentDTO.model_validate(
            {
                "id": row.id,
                "knowledgeBaseId": row.knowledge_base_id,
                "fileName": row.original_name,
                "mimeType": row.content_type,
                "sizeBytes": row.size_bytes,
                "status": row.status,
                "currentAttemptId": row.current_attempt_id,
                "attemptNumber": attempt.attempt_no,
                "chunkCount": attempt.chunk_count,
                "errorCode": attempt.error_code,
                "errorMessage": (
                    INGEST_ERRORS.get(attempt.error_code, "处理失败，请核对文件与模型配置后重试。")
                    if attempt.error_code
                    else None
                ),
                "createdAt": row.created_at,
                "readyAt": row.ready_at,
                "deletedAt": row.deleted_at,
                "cleanupStatus": row.cleanup_status if admin else None,
            }
        )

    def audit(self, identity: IdentityService, org_id: UUID, action: str, target: UUID) -> None:
        identity.db.add(
            AuditEntry(
                organization_id=org_id,
                actor_id=identity.require_user().id,
                action=action,
                target_type="knowledge",
                target_id=target,
                request_id=self.request_id,
            )
        )

    async def list_bases(
        self, org_id: UUID, q: str, limit: int, cursor: str | None
    ) -> Page[KnowledgeDTO]:
        async with self.access(org_id) as (identity, org, _):
            query = select(KnowledgeBase).where(
                KnowledgeBase.organization_id == org_id,
                KnowledgeBase.deleted_at.is_(None),
                KnowledgeBase.name.icontains(q, autoescape=True),
            )
            if cursor:
                point = identity.decode_cursor(cursor, org_id, org.access_revision)
                query = query.where(
                    tuple_(KnowledgeBase.created_at, KnowledgeBase.id)
                    < (point.created_at, point.id)
                )
            rows = list(
                (
                    await identity.db.scalars(
                        query.order_by(
                            KnowledgeBase.created_at.desc(), KnowledgeBase.id.desc()
                        ).limit(limit + 1)
                    )
                ).all()
            )
            next_cursor = (
                self.next_cursor(identity, org, rows[limit - 1]) if len(rows) > limit else None
            )
            return Page(
                items=[await self.kb_dto(identity, row) for row in rows[:limit]],
                next_cursor=next_cursor,
            )

    @staticmethod
    def next_cursor(
        identity: IdentityService, org: Organization, row: KnowledgeBase | Document
    ) -> str:
        return identity.encode_cursor(
            PageCursor(
                organization_id=org.id,
                actor_id=identity.require_user().id,
                access_revision=org.access_revision,
                created_at=row.created_at,
                id=row.id,
            )
        )

    async def get_base(self, org_id: UUID, kb_id: UUID) -> KnowledgeDTO:
        async with self.access(org_id) as (identity, _, _):
            return await self.kb_dto(
                identity, await KnowledgeRepository(identity.db, org_id).knowledge_base(kb_id)
            )

    async def write_base(
        self, org_id: UUID, value: KnowledgeWrite, *, key: str = "", kb_id: UUID | None = None
    ) -> KnowledgeDTO:
        async with self.access(org_id, write=True) as (identity, _, _):
            repo = KnowledgeRepository(identity.db, org_id)
            if kb_id is None:
                replay = await identity.replay(org_id, "knowledge.create", key, value.model_dump())
                if replay:
                    return await self.kb_dto(identity, await repo.knowledge_base(replay.target_id))
            name = unicodedata.normalize("NFC", value.name)
            normalized = name.casefold()
            duplicate = await identity.db.scalar(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.organization_id == org_id,
                    KnowledgeBase.normalized_name == normalized,
                    KnowledgeBase.deleted_at.is_(None),
                    KnowledgeBase.id != kb_id if kb_id else true(),
                )
            )
            if duplicate:
                raise AppError(
                    409, "NAME_EXISTS", "此组织已存在同名知识库。", {"fields": [{"field": "name"}]}
                )
            if kb_id:
                row = await repo.knowledge_base(kb_id)
                assert isinstance(value, KnowledgeUpdate)
                identity.check_revision(value.expected_revision, row.revision)
                row.name, row.normalized_name, row.description = name, normalized, value.description
                row.revision += 1
                row.updated_at = now()
            else:
                row = KnowledgeBase(
                    organization_id=org_id,
                    name=name,
                    normalized_name=normalized,
                    description=value.description,
                )
                identity.db.add(row)
            await identity.db.flush()
            result = await self.kb_dto(identity, row)
            if not kb_id:
                identity.remember(
                    org_id, "knowledge.create", key, value.model_dump(), 201, result, row.id
                )
            self.audit(
                identity, org_id, "knowledge.updated" if kb_id else "knowledge.created", row.id
            )
            return result

    async def documents(
        self,
        org_id: UUID,
        kb_id: UUID,
        q: str,
        status: DocumentStatus | None,
        limit: int,
        cursor: str | None,
    ) -> Page[DocumentDTO]:
        async with self.access(org_id) as (identity, org, admin):
            repo = KnowledgeRepository(identity.db, org_id)
            await repo.knowledge_base(kb_id)
            query = select(Document).where(
                Document.organization_id == org_id,
                Document.knowledge_base_id == kb_id,
                Document.deleted_at.is_(None),
                Document.original_name.icontains(q, autoescape=True),
            )
            if status:
                query = query.where(Document.status == status)
            if cursor:
                point = identity.decode_cursor(cursor, org_id, org.access_revision)
                query = query.where(
                    tuple_(Document.created_at, Document.id) < (point.created_at, point.id)
                )
            rows = list(
                (
                    await identity.db.scalars(
                        query.order_by(Document.created_at.desc(), Document.id.desc()).limit(
                            limit + 1
                        )
                    )
                ).all()
            )
            next_cursor = (
                self.next_cursor(identity, org, rows[limit - 1]) if len(rows) > limit else None
            )
            return Page(
                items=[await self.document_dto(repo, row, admin) for row in rows[:limit]],
                next_cursor=next_cursor,
            )

    async def detail(self, org_id: UUID, doc_id: UUID) -> DocumentDetail:
        async with self.access(org_id) as (identity, _, admin):
            repo = KnowledgeRepository(identity.db, org_id)
            row = await repo.document(doc_id)
            attempts = (
                await identity.db.scalars(
                    select(DocumentAttempt)
                    .where(
                        DocumentAttempt.organization_id == org_id,
                        DocumentAttempt.document_id == row.id,
                    )
                    .order_by(DocumentAttempt.attempt_no.desc())
                    .limit(20)
                )
            ).all()
            return DocumentDetail(
                document=await self.document_dto(repo, row, admin),
                attempts=[
                    AttemptDTO(
                        id=a.id,
                        attempt_number=a.attempt_no,
                        status=a.status,
                        created_at=a.created_at,
                        error_code=a.error_code,
                    )
                    for a in attempts
                ],
            )

    async def replay(
        self, identity: IdentityService, org_id: UUID, kb_id: UUID, key: str
    ) -> IdempotencyKey | None:
        return (
            await identity.db.scalars(
                select(IdempotencyKey).where(
                    IdempotencyKey.organization_id == org_id,
                    IdempotencyKey.actor_id == identity.require_user().id,
                    IdempotencyKey.resource_id == kb_id,
                    IdempotencyKey.operation == "document.upload",
                    IdempotencyKey.key == key,
                )
            )
        ).one_or_none()

    async def accept(self, org_id: UUID, kb_id: UUID, key: str, upload: Upload) -> DocumentDTO:
        payload = {
            "knowledgeBaseId": str(kb_id),
            "name": upload.name,
            "mime": upload.mime,
            "sha256": upload.sha.hexdigest(),
            "size": upload.size,
        }
        digest = IdentityService.payload_digest(payload)
        async with self.access(org_id, write=True) as (identity, _, _):
            repo = KnowledgeRepository(identity.db, org_id)
            await repo.knowledge_base(kb_id)
            replay = await self.replay(identity, org_id, kb_id, key)
            if replay:
                if replay.request_digest != digest:
                    raise AppError(
                        409, "IDEMPOTENCY_CONFLICT", "此提交标识已用于不同文件，请核对原请求。"
                    )
                return await self.document_dto(
                    repo, await repo.document(replay.target_id, include_deleted=True), True
                )
            await self.check_acceptance(identity, org_id, upload.size)
        object_key = f"{self.settings.s3_prefix}{org_id}/{uuid4().hex}"
        await self.storage.put(object_key, upload.body, upload.size, upload.mime)
        # An ambiguous commit leaves the object for DB-aware orphan cleanup, never blind deletion.
        async with self.access(org_id, write=True) as (identity, _, _):
            repo = KnowledgeRepository(identity.db, org_id)
            kb = await repo.knowledge_base(kb_id)
            replay = await self.replay(identity, org_id, kb_id, key)
            if replay:
                if replay.request_digest != digest:
                    raise AppError(409, "IDEMPOTENCY_CONFLICT", "此提交标识已用于不同文件。")
                result = await self.document_dto(
                    repo, await repo.document(replay.target_id, include_deleted=True), True
                )
            else:
                await self.check_acceptance(identity, org_id, upload.size)
                model = await ModelRepository(identity.db, org_id).connection("embedding")
                assert model is not None
                document_id, attempt_id = uuid4(), uuid4()
                row = Document(
                    id=document_id,
                    organization_id=org_id,
                    knowledge_base_id=kb_id,
                    original_name=upload.name,
                    content_type=upload.mime,
                    size_bytes=upload.size,
                    sha256=upload.sha.hexdigest(),
                    object_key=object_key,
                    current_attempt_id=attempt_id,
                )
                identity.db.add(row)
                identity.db.add(
                    DocumentAttempt(
                        id=attempt_id,
                        organization_id=org_id,
                        document_id=document_id,
                        embedding_connection_id=model.id,
                        embedding_profile_id=model.active_profile_id,
                        model_connection_version_id=model.current_connection_version_id,
                        limits_snapshot=await self.limits(identity, org_id),
                    )
                )
                self.enqueue(identity, row, "document.ingest")
                kb.updated_at = now()
                await identity.db.flush()
                result = await self.document_dto(repo, row, True)
                identity.db.add(
                    IdempotencyKey(
                        organization_id=org_id,
                        actor_id=identity.require_user().id,
                        operation="document.upload",
                        resource_id=kb_id,
                        key=key,
                        request_digest=digest,
                        response_status=202,
                        response_data=result.model_dump(mode="json", by_alias=True),
                        target_id=row.id,
                    )
                )
                self.audit(identity, org_id, "document.accepted", row.id)
        if replay:
            try:
                await self.storage.delete(object_key)
            except AppError:
                pass  # Unreferenced duplicate is also covered by orphan cleanup.
        return result

    async def check_acceptance(self, identity: IdentityService, org_id: UUID, size: int) -> None:
        if size > (await self.limits(identity, org_id))["max_upload_bytes"]:
            raise AppError(413, "FILE_TOO_LARGE", "文件超过当前组织限制，请刷新限制后重试。")
        count = await identity.db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.organization_id == org_id, Document.deleted_at.is_(None))
        )
        if (count or 0) >= self.settings.max_documents_per_organization:
            raise AppError(409, "DOCUMENT_LIMIT_REACHED", "组织文档数已达上限，请先维护已有资料。")
        model = await ModelRepository(identity.db, org_id).connection("embedding")
        if not model or not model.enabled:
            raise AppError(
                409, "EMBEDDING_NOT_CONFIGURED", "请先在模型配置中保存并测试 Embedding。"
            )

    @staticmethod
    def enqueue(identity: IdentityService, row: Document, kind: str) -> None:
        identity.db.add(
            OutboxJob(
                organization_id=row.organization_id,
                job_type=kind,
                resource_id=row.id,
                resource_version=row.current_attempt_id,
                payload={
                    "organizationId": str(row.organization_id),
                    "documentId": str(row.id),
                    "attemptId": str(row.current_attempt_id),
                    "requestId": identity.request_id,
                },
            )
        )

    async def acceptance(self, org_id: UUID, kb_id: UUID, key: str) -> AcceptanceDTO:
        async with self.access(org_id) as (identity, _, admin):
            replay = await self.replay(identity, org_id, kb_id, key)
            if not replay:
                await KnowledgeRepository(identity.db, org_id).knowledge_base(kb_id)
                return AcceptanceDTO(status="absent")
            repo = KnowledgeRepository(identity.db, org_id)
            row = await repo.document(replay.target_id, include_deleted=True)
            return AcceptanceDTO(
                status="accepted",
                target_id=row.id,
                document=await self.document_dto(repo, row, admin),
            )

    async def tombstone(
        self, identity: IdentityService, repo: KnowledgeRepository, row: Document
    ) -> None:
        if row.deleted_at:
            return
        row.deleted_at, row.cleanup_status = now(), "pending"
        attempt = await repo.attempt(row)
        if attempt.status in {"queued", "processing"}:
            attempt.status, attempt.finished_at = "cancelled", now()
        self.enqueue(identity, row, "document.cleanup")
        self.audit(identity, row.organization_id, "document.deleted", row.id)

    async def delete_document(self, org_id: UUID, doc_id: UUID) -> None:
        async with self.access(org_id, write=True) as (identity, org, _):
            repo = KnowledgeRepository(identity.db, org_id)
            row = await repo.document(doc_id, include_deleted=True)
            await self.tombstone(identity, repo, row)
            org.access_revision += 1

    async def delete_base(self, org_id: UUID, kb_id: UUID) -> None:
        async with self.access(org_id, write=True) as (identity, org, _):
            repo = KnowledgeRepository(identity.db, org_id)
            kb = await repo.knowledge_base(kb_id)
            bindings = await repo.bindings(kb_id)
            if bindings:
                raise AppError(
                    409, "KB_IN_USE", "知识库仍被 Agent 使用，请先解除绑定。", {"agents": bindings}
                )
            rows = (
                await identity.db.scalars(
                    select(Document).where(
                        Document.organization_id == org_id,
                        Document.knowledge_base_id == kb_id,
                        Document.deleted_at.is_(None),
                    )
                )
            ).all()
            for row in rows:
                await self.tombstone(identity, repo, row)
            kb.deleted_at, kb.updated_at = now(), now()
            org.access_revision += 1
            self.audit(identity, org_id, "knowledge.deleted", kb_id)

    async def retry(self, org_id: UUID, doc_id: UUID, expected: UUID, key: str) -> DocumentDTO:
        async with self.access(org_id, write=True) as (identity, _, _):
            repo = KnowledgeRepository(identity.db, org_id)
            row = await repo.document(doc_id)
            await identity.db.refresh(row, with_for_update=True)
            digest = identity.payload_digest({"expectedAttemptId": str(expected)})
            replay = await identity.db.scalar(
                select(IdempotencyKey).where(
                    IdempotencyKey.organization_id == org_id,
                    IdempotencyKey.actor_id == identity.require_user().id,
                    IdempotencyKey.resource_id == doc_id,
                    IdempotencyKey.operation == "document.retry",
                    IdempotencyKey.key == key,
                )
            )
            if replay:
                if replay.request_digest != digest:
                    raise AppError(409, "IDEMPOTENCY_CONFLICT", "此重试标识已用于不同版本。")
                return DocumentDTO.model_validate(replay.response_data)
            if row.current_attempt_id != expected or row.status != "failed":
                raise AppError(
                    409, "DOCUMENT_STATE_CONFLICT", "当前版本不允许重试，请刷新处理记录。"
                )
            old = await repo.attempt(row)
            model = await ModelRepository(identity.db, org_id).connection("embedding")
            if (
                not model
                or not model.enabled
                or model.active_profile_id != old.embedding_profile_id
            ):
                raise AppError(
                    409, "EMBEDDING_NOT_CONFIGURED", "请恢复并测试原 Embedding 模型配置。"
                )
            attempt = DocumentAttempt(
                id=uuid4(),
                organization_id=org_id,
                document_id=doc_id,
                attempt_no=old.attempt_no + 1,
                pipeline_version=PIPELINE,
                embedding_connection_id=model.id,
                embedding_profile_id=model.active_profile_id,
                model_connection_version_id=model.current_connection_version_id,
                limits_snapshot=await self.limits(identity, org_id),
            )
            identity.db.add(attempt)
            row.current_attempt_id, row.status, row.ready_at = attempt.id, "queued", None
            self.enqueue(identity, row, "document.ingest")
            await identity.db.flush()
            result = await self.document_dto(repo, row, True)
            identity.db.add(
                IdempotencyKey(
                    organization_id=org_id,
                    actor_id=identity.require_user().id,
                    operation="document.retry",
                    resource_id=doc_id,
                    key=key,
                    request_digest=digest,
                    response_status=202,
                    response_data=result.model_dump(mode="json", by_alias=True),
                    target_id=doc_id,
                )
            )
            self.audit(identity, org_id, "document.retried", doc_id)
            return result

    @staticmethod
    def preview_offset(cursor: str | None) -> int:
        if cursor is None:
            return 0
        if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 8:
            raise AppError(422, "CURSOR_INVALID", "分页位置无效。")
        return int(cursor)

    async def published(
        self, repo: KnowledgeRepository, doc_id: UUID, attempt_id: UUID | None
    ) -> tuple[Document, DocumentAttempt]:
        row = await repo.document(doc_id)
        attempt = await repo.attempt(row)
        if row.status != "ready" or attempt.status != "ready":
            raise AppError(409, "DOCUMENT_NOT_READY", "文档尚未就绪，当前没有可用文本或片段。")
        if attempt_id and attempt.id != attempt_id:
            raise AppError(409, "DOCUMENT_STATE_CONFLICT", "文档版本已改变，请刷新。")
        return row, attempt

    async def text_page(
        self, org_id: UUID, doc_id: UUID, attempt_id: UUID | None, cursor: str | None
    ) -> TextPage:
        offset = self.preview_offset(cursor)
        async with self.access(org_id) as (identity, _, _):
            _, attempt = await self.published(
                KnowledgeRepository(identity.db, org_id), doc_id, attempt_id
            )
            key, size, version = attempt.extracted_object_key, attempt.extracted_size, attempt.id
            if not key or size is None:
                raise AppError(409, "DOCUMENT_NOT_READY", "当前版本尚无提取文本。")
        body = (await self.storage.get(key, size)).decode("utf-8")
        async with self.access(org_id) as (identity, _, _):
            await self.published(KnowledgeRepository(identity.db, org_id), doc_id, version)
        return TextPage(
            document_id=doc_id,
            attempt_id=version,
            text=body[offset : offset + 4000],
            next_cursor=str(offset + 4000) if offset + 4000 < len(body) else None,
        )

    async def chunks(
        self, org_id: UUID, doc_id: UUID, attempt_id: UUID | None, cursor: str | None
    ) -> ChunkPreview:
        offset = self.preview_offset(cursor)
        async with self.access(org_id) as (identity, _, admin):
            if not admin:
                raise AppError(403, "FORBIDDEN", "只有管理员可预览全部片段。")
            _, attempt = await self.published(
                KnowledgeRepository(identity.db, org_id), doc_id, attempt_id
            )
            rows = (
                await identity.db.scalars(
                    select(DocumentChunk)
                    .where(
                        DocumentChunk.organization_id == org_id,
                        DocumentChunk.document_id == doc_id,
                        DocumentChunk.attempt_id == attempt.id,
                        DocumentChunk.ordinal >= offset,
                        DocumentChunk.text.is_not(None),
                        DocumentChunk.embedding.is_not(None),
                    )
                    .order_by(DocumentChunk.ordinal)
                    .limit(21)
                )
            ).all()
            return ChunkPreview(
                document_id=doc_id,
                attempt_id=attempt.id,
                items=[
                    ChunkDTO(chunk_id=r.id, ordinal=r.ordinal, text=r.text or "", locator=r.locator)
                    for r in rows[:20]
                ],
                next_cursor=str(rows[20].ordinal) if len(rows) > 20 else None,
            )

    async def original(
        self, org_id: UUID, doc_id: UUID, attempt_id: UUID | None
    ) -> tuple[bytes, str, str]:
        async with self.access(org_id) as (identity, _, _):
            row = await KnowledgeRepository(identity.db, org_id).document(doc_id)
            if attempt_id and attempt_id != row.current_attempt_id:
                raise AppError(409, "DOCUMENT_STATE_CONFLICT", "文档版本已改变，请刷新。")
            key, size, name, mime = (
                row.object_key,
                row.size_bytes,
                row.original_name,
                row.content_type,
            )
        body = await self.storage.get(key, size)
        async with self.access(org_id) as (identity, _, _):
            await KnowledgeRepository(identity.db, org_id).document(doc_id)
        return body, name, mime
