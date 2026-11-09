"""Public DTOs shared by HTTP adapters and business services."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator
from pydantic.alias_generators import to_camel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Password = Annotated[str, StringConstraints(min_length=8, max_length=128)]
Role = Literal["owner", "admin", "member"]
Revision = Annotated[int, Field(strict=True, ge=1)]


class DTO(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class EmailInput(DTO):
    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class LoginInput(EmailInput):
    password: Password


class RegisterInput(LoginInput):
    display_name: Name


class ProfileInput(DTO):
    display_name: Name
    expected_revision: Revision


class PasswordInput(DTO):
    current_password: Password
    new_password: Password


class OrganizationInput(DTO):
    name: Name


class RoleInput(DTO):
    base_role: Literal["admin", "member"]
    expected_revision: Revision


class TransferInput(DTO):
    target_user_id: UUID
    expected_revision: Revision


class UserDTO(DTO):
    id: UUID
    email: str
    display_name: str
    revision: int


class AccountDTO(DTO):
    user_id: UUID
    email: str
    display_name: str


class OrganizationDTO(DTO):
    id: UUID
    name: str
    effective_role: Role
    access_revision: int
    settings_revision: int
    created_at: datetime


class MemberDTO(AccountDTO):
    effective_role: Role
    joined_at: datetime
    revision: int


class Page[T](DTO):
    items: list[T]
    next_cursor: str | None = None


class SessionDTO(DTO):
    user: UserDTO | None
    csrf_token: str
    organizations: list[OrganizationDTO]
    expires_at: datetime | None


class Envelope[T](DTO):
    data: T
    request_id: str


class ErrorData(DTO):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(DTO):
    error: ErrorData
    request_id: str
