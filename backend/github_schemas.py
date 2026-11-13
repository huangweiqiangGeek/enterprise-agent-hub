"""D13 public allowlisted projections; secrets are write-only."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretStr, StringConstraints, field_validator

from backend.schemas import DTO, Name

Component = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]
Revision = Annotated[int, Field(strict=True, ge=0)]


class GitHubTestInput(DTO):
    credential: SecretStr | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("credential", mode="before")
    @classmethod
    def credential_not_blank(cls, value: object) -> object:
        if value is None or isinstance(value, str) and not value.strip():
            raise ValueError("Omit credential to use saved connection")
        return value


class GitHubWrite(GitHubTestInput):
    name: Name
    enabled: bool
    expected_revision: Revision


class GitHubTestResult(DTO):
    ok: bool
    code: str
    message: str
    protocol: str | None = None
    schema_fingerprint: str | None = None


class GitHubSummary(DTO):
    enabled: bool
    available: bool


class GitHubConnectionView(GitHubSummary):
    name: str
    has_credential: bool
    revision: int
    last_tested_at: datetime | None = None
    test_result: GitHubTestResult | None = None


class RepositoryAdd(DTO):
    owner: Component
    repo: Component
    share_with_organization: Literal[True]


class RepositoryView(DTO):
    id: UUID
    github_repo_id: int
    owner: str
    repo: str
    enabled: bool
    verified_at: datetime


class ToolWrite(DTO):
    enabled: bool
    expected_revision: Revision


class ToolView(DTO):
    key: str
    name: str
    description: str
    read_only: Literal[True] = True
    enabled: bool
    revision: int
    input_summary: str
