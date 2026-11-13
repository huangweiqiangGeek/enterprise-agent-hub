from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.identity import CSRF_COOKIE, SESSION_COOKIE, OrgId
from backend.dashboard_schemas import DashboardView
from backend.schemas import Envelope, ErrorResponse
from backend.services.dashboard import DashboardService
from backend.services.model_settings import SessionProof

router = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["dashboard"],
    responses={s: {"model": ErrorResponse} for s in (401, 403, 404, 422, 503)},
)


def dashboard_service(request: Request) -> DashboardService:
    return DashboardService(
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


@router.get("", response_model=Envelope[DashboardView])
async def dashboard(
    org_id: OrgId,
    service: Annotated[DashboardService, Depends(dashboard_service)],
) -> Envelope[DashboardView]:
    return Envelope(data=await service.dashboard(org_id), request_id=service.request_id)
