from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from backend.schemas import (
    AccountDTO,
    EmailInput,
    Envelope,
    ErrorResponse,
    LoginInput,
    MemberDTO,
    OrganizationDTO,
    OrganizationInput,
    Page,
    PasswordInput,
    ProfileInput,
    RegisterInput,
    RoleInput,
    SessionDTO,
    TransferInput,
    UserDTO,
)
from backend.services.identity import IdentityService

SESSION_COOKIE = "eah_session"
CSRF_COOKIE = "eah_csrf"
router = APIRouter(
    prefix="/api/v1",
    tags=["identity"],
    responses={status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 503)},
)


async def identity_service(request: Request) -> AsyncIterator[IdentityService]:
    async with IdentityService.open(
        request.app.state.engine, request.app.state.settings, request.state.request_id
    ) as service:
        await service.authenticate(
            request.cookies.get(SESSION_COOKIE, ""),
            request.cookies.get(CSRF_COOKIE, ""),
            logout_only=request.method == "POST" and request.url.path == "/api/v1/auth/logout",
        )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            service.check_csrf(request.headers.get("origin"), request.headers.get("x-csrf-token"))
        yield service


Service = Annotated[IdentityService, Depends(identity_service, scope="function")]
OrgId = Annotated[UUID, Header(alias="X-Organization-Id")]
Key = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
]


def envelope[T](service: IdentityService, data: T) -> Envelope[T]:
    return Envelope(data=data, request_id=service.request_id)


def cookies(service: IdentityService, response: Response) -> None:
    if service.cookies is None:
        return
    session_token, csrf, max_age = service.cookies
    for name, value in ((SESSION_COOKIE, session_token), (CSRF_COOKIE, csrf)):
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/",
            httponly=True,
            secure=service.settings.cookie_secure,
            samesite="lax",
        )


@router.get("/auth/session", response_model=Envelope[SessionDTO])
async def session(service: Service, response: Response) -> Envelope[SessionDTO]:
    result = await service.session_info()
    cookies(service, response)
    return envelope(service, result)


@router.post("/auth/register", response_model=Envelope[UserDTO], status_code=201)
async def register(data: RegisterInput, service: Service, response: Response) -> Envelope[UserDTO]:
    result = await service.register(data)
    cookies(service, response)
    return envelope(service, result)


@router.post("/auth/login", response_model=Envelope[UserDTO])
async def login(data: LoginInput, service: Service, response: Response) -> Envelope[UserDTO]:
    result = await service.login(str(data.email), data.password)
    cookies(service, response)
    return envelope(service, result)


@router.post("/auth/logout", status_code=204)
async def logout(service: Service, response: Response) -> None:
    await service.logout()
    cookies(service, response)


@router.patch("/me", response_model=Envelope[UserDTO])
async def profile(data: ProfileInput, service: Service) -> Envelope[UserDTO]:
    return envelope(service, await service.profile(data))


@router.post("/me/password", status_code=204)
async def password(data: PasswordInput, service: Service, response: Response) -> None:
    await service.password(data)
    cookies(service, response)


@router.get("/organizations", response_model=Envelope[Page[OrganizationDTO]])
async def organizations(
    service: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> Envelope[Page[OrganizationDTO]]:
    return envelope(service, await service.organization_page(limit, cursor))


@router.post("/organizations", response_model=Envelope[OrganizationDTO], status_code=201)
async def create_organization(
    data: OrganizationInput, service: Service, key: Key
) -> Envelope[OrganizationDTO]:
    return envelope(service, await service.create_organization(data.name, key))


@router.get("/organizations/current", response_model=Envelope[OrganizationDTO])
async def current_organization(
    service: Service, organization_id: OrgId
) -> Envelope[OrganizationDTO]:
    org, member = await service.organization(organization_id)
    return envelope(service, service.org_dto(org, member))


@router.get("/members", response_model=Envelope[Page[MemberDTO]])
async def members(
    service: Service,
    organization_id: OrgId,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> Envelope[Page[MemberDTO]]:
    return envelope(service, await service.members(organization_id, limit, cursor))


@router.post("/members/resolve", response_model=Envelope[AccountDTO])
async def resolve_account(
    data: EmailInput, service: Service, organization_id: OrgId
) -> Envelope[AccountDTO]:
    return envelope(service, await service.resolve(organization_id, str(data.email)))


@router.post(
    "/members",
    response_model=Envelope[MemberDTO],
    status_code=201,
    responses={200: {"model": Envelope[MemberDTO]}},
)
async def add_member(
    data: EmailInput, service: Service, organization_id: OrgId, key: Key, response: Response
) -> Envelope[MemberDTO]:
    result, response.status_code = await service.add_member(organization_id, data, key)
    return envelope(service, result)


@router.patch("/members/{user_id}", response_model=Envelope[MemberDTO])
async def change_role(
    user_id: UUID, data: RoleInput, service: Service, organization_id: OrgId
) -> Envelope[MemberDTO]:
    return envelope(service, await service.change_role(organization_id, user_id, data))


@router.delete("/members/{user_id}", status_code=204)
async def remove_member(user_id: UUID, service: Service, organization_id: OrgId) -> None:
    await service.remove_member(organization_id, user_id)


@router.post("/organizations/current/transfer-owner", response_model=Envelope[OrganizationDTO])
async def transfer_owner(
    data: TransferInput, service: Service, organization_id: OrgId, key: Key
) -> Envelope[OrganizationDTO]:
    return envelope(service, await service.transfer(organization_id, data, key))
