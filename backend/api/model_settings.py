from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.identity import CSRF_COOKIE, SESSION_COOKIE, OrgId
from backend.model_schemas import (
    LimitsView,
    LimitsWrite,
    ModelConfig,
    ModelConfigWrite,
    ModelKind,
    ModelSettingsView,
    SavedModelTest,
    TestResult,
)
from backend.schemas import Envelope, ErrorResponse
from backend.services.model_settings import ModelSettingsService, SessionProof

router = APIRouter(
    prefix="/api/v1/settings",
    tags=["settings"],
    responses={s: {"model": ErrorResponse} for s in (401, 403, 404, 409, 422, 503)},
)


def settings_service(request: Request) -> ModelSettingsService:
    return ModelSettingsService(
        request.app.state.engine,
        request.app.state.settings,
        request.state.request_id,
        SessionProof(
            request.cookies.get(SESSION_COOKIE, ""),
            request.cookies.get(CSRF_COOKIE, ""),
            request.headers.get("origin"),
            request.headers.get("x-csrf-token"),
        ),
        request.app.state.model_probe,
    )


Service = Annotated[ModelSettingsService, Depends(settings_service)]


@router.get("/models", response_model=Envelope[ModelSettingsView])
async def models(org_id: OrgId, service: Service) -> Envelope[ModelSettingsView]:
    return Envelope(data=await service.list_models(org_id), request_id=service.request_id)


@router.put("/models/{kind}", response_model=Envelope[ModelConfig])
async def save_model(
    kind: ModelKind, org_id: OrgId, value: ModelConfigWrite, service: Service
) -> Envelope[ModelConfig]:
    result = await service.test_model(org_id, kind, value, save=True)
    assert isinstance(result, ModelConfig)
    return Envelope(data=result, request_id=service.request_id)


@router.post("/models/{kind}/test", response_model=Envelope[TestResult])
async def test_model(
    kind: ModelKind, org_id: OrgId, value: ModelConfigWrite | SavedModelTest, service: Service
) -> Envelope[TestResult]:
    result = await service.test_model(org_id, kind, value)
    assert isinstance(result, TestResult)
    return Envelope(data=result, request_id=service.request_id)


@router.get("/limits", response_model=Envelope[LimitsView])
async def limits(org_id: OrgId, service: Service) -> Envelope[LimitsView]:
    return Envelope(data=await service.limits(org_id), request_id=service.request_id)


@router.put("/limits", response_model=Envelope[LimitsView])
async def save_limits(org_id: OrgId, value: LimitsWrite, service: Service) -> Envelope[LimitsView]:
    return Envelope(data=await service.limits(org_id, value), request_id=service.request_id)
