import asyncio
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from backend.api.identity import CSRF_COOKIE, SESSION_COOKIE, Key, OrgId
from backend.knowledge_schemas import (
    AcceptanceDTO,
    ChunkPreview,
    DocumentDetail,
    DocumentDTO,
    DocumentStatus,
    KnowledgeDTO,
    KnowledgeUpdate,
    KnowledgeWrite,
    RetryDocument,
    TextPage,
    UploadPolicy,
)
from backend.retrieval_schemas import RetrievalResult, SearchPreview
from backend.schemas import Envelope, ErrorResponse, Page
from backend.services.errors import AppError
from backend.services.knowledge import KnowledgeService
from backend.services.model_settings import SessionProof
from backend.services.retrieval import RetrievalService
from backend.services.upload import Upload

router = APIRouter(
    prefix="/api/v1",
    tags=["knowledge"],
    responses={
        s: {"model": ErrorResponse} for s in (401, 403, 404, 408, 409, 413, 415, 422, 429, 503)
    },
)


def knowledge_service(request: Request) -> KnowledgeService:
    return KnowledgeService(
        request.app.state.engine,
        request.app.state.settings,
        request.state.request_id,
        SessionProof(
            request.cookies.get(SESSION_COOKIE, ""),
            request.cookies.get(CSRF_COOKIE, ""),
            request.headers.get("origin"),
            request.headers.get("x-csrf-token"),
        ),
        request.app.state.storage,
    )


Service = Annotated[KnowledgeService, Depends(knowledge_service)]
Limit = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str, Query(max_length=200)]
Cursor = Annotated[str | None, Query(max_length=2000)]


@router.get("/knowledge-bases", response_model=Envelope[Page[KnowledgeDTO]])
async def bases(
    org_id: OrgId, service: Service, q: Search = "", limit: Limit = 20, cursor: Cursor = None
) -> Envelope[Page[KnowledgeDTO]]:
    return Envelope(
        data=await service.list_bases(org_id, q, limit, cursor), request_id=service.request_id
    )


@router.post("/knowledge-bases", response_model=Envelope[KnowledgeDTO], status_code=201)
async def create_base(
    org_id: OrgId, key: Key, value: KnowledgeWrite, service: Service
) -> Envelope[KnowledgeDTO]:
    return Envelope(
        data=await service.write_base(org_id, value, key=key), request_id=service.request_id
    )


@router.get("/knowledge-bases/{kb_id}", response_model=Envelope[KnowledgeDTO])
async def base(kb_id: UUID, org_id: OrgId, service: Service) -> Envelope[KnowledgeDTO]:
    return Envelope(data=await service.get_base(org_id, kb_id), request_id=service.request_id)


@router.post("/knowledge-bases/{kb_id}/search-preview", response_model=Envelope[RetrievalResult])
async def search_preview(
    kb_id: UUID, org_id: OrgId, value: SearchPreview, service: Service, request: Request
) -> Envelope[RetrievalResult]:
    retrieval = RetrievalService(service, request.app.state.model_probe)
    return Envelope(
        data=await retrieval.search(org_id, kb_id, value), request_id=service.request_id
    )


@router.patch("/knowledge-bases/{kb_id}", response_model=Envelope[KnowledgeDTO])
async def update_base(
    kb_id: UUID, org_id: OrgId, value: KnowledgeUpdate, service: Service
) -> Envelope[KnowledgeDTO]:
    return Envelope(
        data=await service.write_base(org_id, value, kb_id=kb_id), request_id=service.request_id
    )


@router.delete("/knowledge-bases/{kb_id}", status_code=204)
async def delete_base(kb_id: UUID, org_id: OrgId, service: Service) -> Response:
    await service.delete_base(org_id, kb_id)
    return Response(status_code=204)


@router.get("/knowledge-bases/{kb_id}/upload-policy", response_model=Envelope[UploadPolicy])
async def upload_policy(kb_id: UUID, org_id: OrgId, service: Service) -> Envelope[UploadPolicy]:
    return Envelope(data=await service.policy(org_id, kb_id), request_id=service.request_id)


@router.get("/knowledge-bases/{kb_id}/documents", response_model=Envelope[Page[DocumentDTO]])
async def documents(
    kb_id: UUID,
    org_id: OrgId,
    service: Service,
    q: Search = "",
    status: DocumentStatus | None = None,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> Envelope[Page[DocumentDTO]]:
    return Envelope(
        data=await service.documents(org_id, kb_id, q, status, limit, cursor),
        request_id=service.request_id,
    )


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=Envelope[DocumentDTO],
    status_code=202,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "additionalProperties": False,
                    }
                }
            },
        }
    },
)
async def upload(
    kb_id: UUID, org_id: OrgId, key: Key, request: Request, service: Service
) -> Envelope[DocumentDTO]:
    policy = await service.policy(org_id, kb_id, write=True)
    try:
        await asyncio.wait_for(request.app.state.upload_slots.acquire(), timeout=0.1)
    except TimeoutError:
        raise AppError(429, "UPLOAD_BUSY", "上传服务繁忙，请保留当前文件并稍后重试。") from None
    value = Upload(policy.max_bytes)
    try:
        await value.read(request.stream(), request.headers.get("content-type", ""))
        return Envelope(
            data=await service.accept(org_id, kb_id, key, value), request_id=service.request_id
        )
    finally:
        value.body.close()
        request.app.state.upload_slots.release()


@router.get("/documents/{doc_id}", response_model=Envelope[DocumentDetail])
async def document(doc_id: UUID, org_id: OrgId, service: Service) -> Envelope[DocumentDetail]:
    return Envelope(data=await service.detail(org_id, doc_id), request_id=service.request_id)


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: UUID, org_id: OrgId, service: Service) -> Response:
    await service.delete_document(org_id, doc_id)
    return Response(status_code=204)


@router.post("/documents/{doc_id}/retry", response_model=Envelope[DocumentDTO], status_code=202)
async def retry_document(
    doc_id: UUID, org_id: OrgId, key: Key, value: RetryDocument, service: Service
) -> Envelope[DocumentDTO]:
    return Envelope(
        data=await service.retry(org_id, doc_id, value.expected_attempt_id, key),
        request_id=service.request_id,
    )


@router.get("/documents/{doc_id}/chunks", response_model=Envelope[ChunkPreview])
async def chunks(
    doc_id: UUID,
    org_id: OrgId,
    service: Service,
    attempt_id: Annotated[UUID | None, Query(alias="attemptId")] = None,
    cursor: Cursor = None,
) -> Envelope[ChunkPreview]:
    return Envelope(
        data=await service.chunks(org_id, doc_id, attempt_id, cursor), request_id=service.request_id
    )


@router.get(
    "/documents/{doc_id}/content",
    response_model=Envelope[TextPage],
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def content(
    doc_id: UUID,
    org_id: OrgId,
    service: Service,
    view: Literal["original", "text"] = "original",
    attempt_id: Annotated[UUID | None, Query(alias="attemptId")] = None,
    cursor: Cursor = None,
) -> Response | Envelope[TextPage]:
    if view == "text":
        return Envelope(
            data=await service.text_page(org_id, doc_id, attempt_id, cursor),
            request_id=service.request_id,
        )
    body, name, mime = await service.original(org_id, doc_id, attempt_id)
    return Response(
        body,
        media_type=mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
        },
    )


@router.get("/idempotency-requests/{key}", response_model=Envelope[AcceptanceDTO])
async def acceptance(
    key: str,
    org_id: OrgId,
    service: Service,
    operation: Literal["document.upload"],
    resource_id: Annotated[UUID, Query(alias="resourceId")],
) -> Envelope[AcceptanceDTO]:
    return Envelope(
        data=await service.acceptance(org_id, resource_id, key), request_id=service.request_id
    )
