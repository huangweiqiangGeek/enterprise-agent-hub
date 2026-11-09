import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse

from backend.api.identity import CSRF_COOKIE, SESSION_COOKIE, Key, OrgId
from backend.run_schemas import (
    ConversationCreate,
    ConversationRename,
    ConversationSummary,
    ConversationView,
    MessageView,
    RunAccepted,
    RunCreate,
    RunSnapshot,
    SourceDetail,
    StepSummary,
    TaskOptions,
)
from backend.schemas import Envelope, ErrorResponse, Page
from backend.services.dialogue import DialogueService
from backend.services.errors import AppError
from backend.services.model_settings import SessionProof

router = APIRouter(
    prefix="/api/v1",
    tags=["runs"],
    responses={s: {"model": ErrorResponse} for s in (401, 403, 404, 409, 422, 429, 503)},
)


def run_service(request: Request) -> DialogueService:
    return DialogueService(
        request.app.state.engine,
        request.app.state.settings,
        request.state.request_id,
        SessionProof(
            request.cookies.get(SESSION_COOKIE, ""),
            request.cookies.get(CSRF_COOKIE, ""),
            request.headers.get("origin"),
            request.headers.get("x-csrf-token"),
        ),
    )


Service = Annotated[DialogueService, Depends(run_service)]


@router.get("/conversations", response_model=Envelope[Page[ConversationSummary]])
async def conversations(
    org_id: OrgId,
    service: Service,
    q: Annotated[str, Query(max_length=200)] = "",
    cursor: Annotated[str | None, Query(max_length=2000)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Envelope[Page[ConversationSummary]]:
    return Envelope(
        data=await service.conversations(org_id, q, cursor, limit), request_id=service.request_id
    )


@router.get("/conversations/{conversation_id}", response_model=Envelope[ConversationSummary])
async def conversation_detail(
    conversation_id: UUID, org_id: OrgId, service: Service
) -> Envelope[ConversationSummary]:
    return Envelope(
        data=await service.conversation_detail(org_id, conversation_id),
        request_id=service.request_id,
    )


@router.patch("/conversations/{conversation_id}", response_model=Envelope[ConversationSummary])
async def rename(
    conversation_id: UUID, org_id: OrgId, value: ConversationRename, service: Service
) -> Envelope[ConversationSummary]:
    return Envelope(
        data=await service.conversation_detail(org_id, conversation_id, value),
        request_id=service.request_id,
    )


@router.get("/conversations/{conversation_id}/messages", response_model=Envelope[Page[MessageView]])
async def messages(
    conversation_id: UUID,
    org_id: OrgId,
    service: Service,
    cursor: Annotated[str | None, Query(max_length=2000)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Envelope[Page[MessageView]]:
    return Envelope(
        data=await service.messages(org_id, conversation_id, cursor, limit),
        request_id=service.request_id,
    )


@router.get("/agents/{agent_id}/task-options", response_model=Envelope[TaskOptions])
async def task_options(agent_id: UUID, org_id: OrgId, service: Service) -> Envelope[TaskOptions]:
    return Envelope(
        data=await service.task_options(org_id, agent_id), request_id=service.request_id
    )


@router.get("/runs/{run_id}/sources/{source_id}", response_model=Envelope[SourceDetail])
async def source(
    run_id: UUID, source_id: UUID, org_id: OrgId, service: Service
) -> Envelope[SourceDetail]:
    return Envelope(
        data=await service.source(org_id, run_id, source_id), request_id=service.request_id
    )


@router.get("/runs/{run_id}/export", response_class=Response)
async def export(
    run_id: UUID, org_id: OrgId, service: Service, format: Literal["markdown"] = "markdown"
) -> Response:
    return Response(
        await service.export(org_id, run_id),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="run-{run_id}.md"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/runs/{run_id}/events", response_class=StreamingResponse)
async def events(
    run_id: UUID, org_id: OrgId, service: Service, request: Request
) -> StreamingResponse:
    cursor = service.event_cursor(run_id, request.headers.get("last-event-id"))
    # Validate before sending HTTP 200; subsequent batches reauthorize in short transactions.
    await service.event_batch(org_id, run_id, cursor)

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        idle = 0
        while not await request.is_disconnected():
            try:
                batch, done = await service.event_batch(org_id, run_id, cursor)
            except AppError:
                return
            for event in batch:
                cursor = event["seq"]
                yield (
                    f"id: {run_id}:{cursor}\nevent: {event['type']}\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                )
            if done:
                return
            if batch:
                idle = 0
                continue
            idle += 1
            if idle % 15 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations", response_model=Envelope[ConversationView], status_code=201)
async def conversation(
    org_id: OrgId, key: Key, value: ConversationCreate, service: Service
) -> Envelope[ConversationView]:
    return Envelope(
        data=await service.create_conversation(org_id, value, key), request_id=service.request_id
    )


@router.post(
    "/conversations/{conversation_id}/runs", response_model=Envelope[RunAccepted], status_code=202
)
async def accept(
    conversation_id: UUID, org_id: OrgId, key: Key, value: RunCreate, service: Service
) -> Envelope[RunAccepted]:
    return Envelope(
        data=await service.accept(org_id, conversation_id, value, key),
        request_id=service.request_id,
    )


@router.get("/runs/{run_id}", response_model=Envelope[RunSnapshot])
async def snapshot(run_id: UUID, org_id: OrgId, service: Service) -> Envelope[RunSnapshot]:
    return Envelope(data=await service.snapshot(org_id, run_id), request_id=service.request_id)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=Envelope[RunSnapshot],
    responses={202: {"model": Envelope[RunSnapshot]}},
)
async def cancel(
    run_id: UUID, org_id: OrgId, service: Service, response: Response
) -> Envelope[RunSnapshot]:
    response.status_code, result = await service.cancel(org_id, run_id)
    return Envelope(data=result, request_id=service.request_id)


@router.get("/runs/{run_id}/steps", response_model=Envelope[Page[StepSummary]])
async def steps(
    run_id: UUID,
    org_id: OrgId,
    service: Service,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Envelope[Page[StepSummary]]:
    return Envelope(
        data=await service.steps(org_id, run_id, cursor, limit), request_id=service.request_id
    )
