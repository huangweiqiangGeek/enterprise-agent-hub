from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator

from backend.schemas import DTO, Name, Revision

TaskType = Literal["qa", "compare", "progress"]
ToolKey = Literal[
    "knowledge.search",
    "github.search_issues",
    "github.search_pull_requests",
    "github.read_issue",
    "github.read_pull_request",
]


class AgentWrite(DTO):
    name: Name
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""
    task_type: TaskType
    prompt: Annotated[str, Field(max_length=20000)] = ""
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=100)
    tool_keys: list[ToolKey] = Field(default_factory=list, max_length=5)
    chat_connection_id: UUID | None = None
    enabled: bool = False

    @field_validator("knowledge_base_ids", "tool_keys")
    @classmethod
    def unique_items(cls, values: list[UUID] | list[ToolKey]) -> list[UUID] | list[ToolKey]:
        if len(set(values)) != len(values):
            raise ValueError("Duplicate resource selectors")
        return values


class AgentUpdate(AgentWrite):
    expected_revision: Revision


class AgentStatus(DTO):
    enabled: bool
    expected_revision: Revision


class AgentClone(DTO):
    new_name: Name


class ReadinessIssue(DTO):
    code: str
    message: str
    field: str
    action: str


class AgentSummary(DTO):
    id: UUID
    name: str
    description: str
    task_type: TaskType
    enabled: bool
    current_version_id: UUID
    version_number: int
    revision: int
    readiness: list[ReadinessIssue]
    created_at: datetime
    updated_at: datetime


class AgentConfig(AgentSummary):
    prompt: str
    knowledge_base_ids: list[UUID]
    tool_keys: list[ToolKey]
    chat_connection_id: UUID | None
    removed_references: list[str] = Field(default_factory=list)


class AgentTemplate(DTO):
    task_type: TaskType
    name: str
    description: str
    prompt: str
    tool_keys: list[ToolKey]


class AgentToolOption(DTO):
    key: ToolKey
    name: str
    available: bool
    note: str


class AgentOptions(DTO):
    templates: list[AgentTemplate]
    tools: list[AgentToolOption]
