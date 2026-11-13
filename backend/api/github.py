from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from backend.api.identity import CSRF_COOKIE, SESSION_COOKIE, OrgId
from backend.github_schemas import (
    GitHubConnectionView,
    GitHubSummary,
    GitHubTestInput,
    GitHubTestResult,
    GitHubWrite,
    RepositoryAdd,
    RepositoryView,
    ToolView,
    ToolWrite,
)
from backend.schemas import Envelope, ErrorResponse
from backend.services.github import GitHubService
from backend.services.model_settings import SessionProof

router = APIRouter(
    prefix="/api/v1",
    tags=["github"],
    responses={s: {"model": ErrorResponse} for s in (401, 403, 404, 409, 422, 502)},
)


def service(request: Request) -> GitHubService:
    return GitHubService(
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


Service = Annotated[GitHubService, Depends(service)]


@router.get("/connections/github", response_model=Envelope[GitHubConnectionView | GitHubSummary])
async def get_connection(
    org_id: OrgId, service: Service
) -> Envelope[GitHubConnectionView | GitHubSummary]:
    return Envelope(data=await service.connection(org_id), request_id=service.request_id)


@router.put("/connections/github", response_model=Envelope[GitHubConnectionView | GitHubSummary])
async def save_connection(
    org_id: OrgId, value: GitHubWrite, service: Service, request: Request
) -> Envelope[GitHubConnectionView | GitHubSummary]:
    return Envelope(
        data=await service.save_connection(org_id, value, request.app.state.github_mcp),
        request_id=service.request_id,
    )


@router.post("/connections/github/test", response_model=Envelope[GitHubTestResult])
async def test_connection(
    org_id: OrgId, value: GitHubTestInput, service: Service, request: Request
) -> Envelope[GitHubTestResult]:
    return Envelope(
        data=await service.test_connection(org_id, value, request.app.state.github_mcp),
        request_id=service.request_id,
    )


@router.get("/connections/github/repositories", response_model=Envelope[list[RepositoryView]])
async def repositories(org_id: OrgId, service: Service) -> Envelope[list[RepositoryView]]:
    return Envelope(data=await service.repositories(org_id), request_id=service.request_id)


@router.post(
    "/connections/github/repositories", response_model=Envelope[RepositoryView], status_code=201
)
async def add_repository(
    org_id: OrgId, value: RepositoryAdd, service: Service, request: Request
) -> Envelope[RepositoryView]:
    return Envelope(
        data=await service.add_repository(
            org_id, value, request.headers.get("idempotency-key", ""), request.app.state.github_mcp
        ),
        request_id=service.request_id,
    )


@router.delete("/connections/github/repositories/{repository}", status_code=204)
async def remove_repository(org_id: OrgId, repository: UUID, service: Service) -> Response:
    await service.remove_repository(org_id, repository)
    return Response(status_code=204)


@router.get("/tools", response_model=Envelope[list[ToolView]])
async def tools(org_id: OrgId, service: Service) -> Envelope[list[ToolView]]:
    return Envelope(data=await service.tools(org_id), request_id=service.request_id)


@router.patch("/tools/{key}", response_model=Envelope[ToolView])
async def change_tool(
    org_id: OrgId, key: str, value: ToolWrite, service: Service
) -> Envelope[ToolView]:
    rows = await service.tools(org_id, key, value)
    return Envelope(data=next(row for row in rows if row.key == key), request_id=service.request_id)
