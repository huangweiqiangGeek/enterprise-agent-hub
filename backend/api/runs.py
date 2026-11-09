from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from backend.api.identity import CSRF_COOKIE, SESSION_COOKIE, Key, OrgId
from backend.run_schemas import (
    ConversationCreate,
    ConversationView,
    RunAccepted,
    RunCreate,
    RunSnapshot,
    StepSummary,
)
from backend.schemas import Envelope, ErrorResponse, Page
from backend.services.model_settings import SessionProof
from backend.services.runs import RunService

router = APIRouter(
    prefix="/api/v1",
    tags=["runs"],
    responses={s: {"model": ErrorResponse} for s in (401, 403, 404, 409, 422, 429, 503)},
)


def run_service(request: Request) -> RunService:
    return RunService(
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


Service = Annotated[RunService, Depends(run_service)]


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
