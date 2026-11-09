from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from backend.agent_schemas import (
    AgentClone,
    AgentConfig,
    AgentOptions,
    AgentStatus,
    AgentSummary,
    AgentUpdate,
    AgentWrite,
    TaskType,
)
from backend.api.identity import CSRF_COOKIE, SESSION_COOKIE, Key, OrgId
from backend.schemas import Envelope, ErrorResponse, Page
from backend.services.agents import AgentService
from backend.services.model_settings import SessionProof

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["agents"],
    responses={s: {"model": ErrorResponse} for s in (401, 403, 404, 409, 422, 503)},
)


def agent_service(request: Request) -> AgentService:
    return AgentService(
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


Service = Annotated[AgentService, Depends(agent_service)]


@router.get("", response_model=Envelope[Page[AgentSummary]])
async def agents(
    org_id: OrgId,
    service: Service,
    q: Annotated[str, Query(max_length=200)] = "",
    task_type: Annotated[TaskType | None, Query(alias="taskType")] = None,
    enabled: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2000)] = None,
) -> Envelope[Page[AgentSummary]]:
    return Envelope(
        data=await service.list_agents(org_id, q, task_type, enabled, limit, cursor),
        request_id=service.request_id,
    )


@router.get("/options", response_model=Envelope[AgentOptions])
async def choices(org_id: OrgId, service: Service) -> Envelope[AgentOptions]:
    return Envelope(data=await service.catalog(org_id), request_id=service.request_id)


@router.get("/{agent_id}", response_model=Envelope[AgentConfig | AgentSummary])
async def detail(
    agent_id: UUID, org_id: OrgId, service: Service
) -> Envelope[AgentConfig | AgentSummary]:
    return Envelope(data=await service.detail(org_id, agent_id), request_id=service.request_id)


@router.post("", response_model=Envelope[AgentConfig], status_code=201)
async def create(
    org_id: OrgId, key: Key, value: AgentWrite, service: Service
) -> Envelope[AgentConfig]:
    return Envelope(data=await service.save(org_id, value, key=key), request_id=service.request_id)


@router.put("/{agent_id}", response_model=Envelope[AgentConfig])
async def save(
    agent_id: UUID, org_id: OrgId, value: AgentUpdate, service: Service
) -> Envelope[AgentConfig]:
    return Envelope(
        data=await service.save(org_id, value, agent_id=agent_id), request_id=service.request_id
    )


@router.post("/{agent_id}/clone", response_model=Envelope[AgentConfig], status_code=201)
async def clone(
    agent_id: UUID, org_id: OrgId, key: Key, value: AgentClone, service: Service
) -> Envelope[AgentConfig]:
    return Envelope(
        data=await service.clone(org_id, agent_id, value, key), request_id=service.request_id
    )


@router.patch("/{agent_id}/status", response_model=Envelope[AgentSummary])
async def status(
    agent_id: UUID, org_id: OrgId, value: AgentStatus, service: Service
) -> Envelope[AgentSummary]:
    return Envelope(
        data=await service.status(org_id, agent_id, value), request_id=service.request_id
    )
