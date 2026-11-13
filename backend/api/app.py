import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from backend import __version__
from backend.api.agents import router as agents_router
from backend.api.github import router as github_router
from backend.api.identity import router as identity_router
from backend.api.knowledge import router as knowledge_router
from backend.api.model_settings import router as settings_router
from backend.api.runs import router as runs_router
from backend.config import Settings
from backend.db import database_ready, make_engine
from backend.schemas import ErrorData, ErrorResponse
from backend.services.errors import AppError
from backend.services.github_mcp import GitHubMCP
from backend.services.model_probe import ModelProbe
from backend.services.storage import Storage


class HealthData(BaseModel):
    status: Literal["ok", "ready"]
    service: str = "enterprise-agent-hub-api"
    version: str = __version__


class HealthResponse(BaseModel):
    data: HealthData
    requestId: str


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = make_engine(config)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(title="Enterprise Agent Hub API", version=__version__, lifespan=lifespan)
    app.state.settings = config
    app.state.model_probe = ModelProbe(config)
    app.state.storage = Storage(config)
    app.state.upload_slots = asyncio.Semaphore(4)
    app.include_router(identity_router)
    app.include_router(settings_router)
    app.include_router(knowledge_router)
    app.include_router(agents_router)
    app.include_router(runs_router)
    app.include_router(github_router)
    app.state.github_mcp = GitHubMCP()

    @app.exception_handler(AppError)
    async def application_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "requestId": request.state.request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo rejected values: input may contain passwords or arbitrary extra secrets.
        fields = [
            {"field": ".".join(str(part) for part in error["loc"][1:]), "code": error["type"]}
            for error in exc.errors()
        ]
        return await application_error(
            request, AppError(422, "VALIDATION_ERROR", "请检查填写内容。", {"fields": fields})
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, _exc: SQLAlchemyError) -> JSONResponse:
        return await application_error(
            request, AppError(503, "SERVICE_NOT_READY", "服务暂时不可用，请稍后重试。")
        )

    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/health/live", response_model=HealthResponse, tags=["health"])
    async def live(request: Request) -> HealthResponse:
        return HealthResponse(data=HealthData(status="ok"), requestId=request.state.request_id)

    @app.get(
        "/api/v1/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["health"],
    )
    async def ready(request: Request) -> HealthResponse | JSONResponse:
        try:
            async with asyncio.timeout(5):
                is_ready = await database_ready(request.app.state.engine)
        except (SQLAlchemyError, TimeoutError, OSError):
            is_ready = False
        if not is_ready:
            error = ErrorResponse(
                error=ErrorData(
                    code="SERVICE_NOT_READY", message="数据库或迁移尚未就绪，请检查初始化步骤。"
                ),
                request_id=request.state.request_id,
            )
            return JSONResponse(status_code=503, content=error.model_dump(by_alias=True))
        return HealthResponse(data=HealthData(status="ready"), requestId=request.state.request_id)

    return app
