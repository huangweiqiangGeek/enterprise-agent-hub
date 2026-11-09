from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator

from backend.schemas import DTO, Name, Revision

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "timed_out"]


class ConversationCreate(DTO):
    agent_id: UUID
    task_type: Literal["qa"]
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ] = "文档问答"


class ConversationView(ConversationCreate):
    id: UUID
    updated_at: datetime
    revision: int = 1


class ConversationSummary(ConversationView):
    agent_name: str
    active_run_id: UUID | None = None
    last_run_id: UUID | None = None
    last_run_status: RunStatus | None = None


class ConversationRename(DTO):
    title: Name
    expected_revision: Revision


class QAInput(DTO):
    question: Text
    knowledge_base_ids: list[UUID] = Field(min_length=1, max_length=10)
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator("knowledge_base_ids", "document_ids")
    @classmethod
    def unique_ids(cls, values: list[UUID]) -> list[UUID]:
        if len(values) != len(set(values)):
            raise ValueError("Duplicate selectors are not allowed")
        return values


class RunCreate(DTO):
    agent_id: UUID
    input: QAInput
    retry_of_run_id: UUID | None = None


class RunAccepted(DTO):
    run_id: UUID
    conversation_id: UUID
    status: RunStatus
    events_url: str
    created_at: datetime


class Claim(DTO):
    text: Text
    kind: Literal["fact", "inference", "suggestion", "unknown"]
    source_ids: list[UUID] = Field(default_factory=list, max_length=80)


class QAResult(DTO):
    answer: str
    claims: list[Claim]
    missing_information: list[str]
    conflicts: list[Claim]
    coverage: dict[str, Any]


class ResultBlock(DTO):
    type: Literal["summary", "claim", "conflict", "unknown", "suggestion"]
    text: Text
    kind: Literal["fact", "inference", "suggestion", "unknown"] = "unknown"
    source_ids: list[UUID] = Field(default_factory=list, max_length=80)


class RunSnapshot(DTO):
    id: UUID
    conversation_id: UUID
    task_type: Literal["qa"] = "qa"
    status: RunStatus
    cancel_requested: bool
    input_summary: str
    config_version: dict[str, Any]
    budget_snapshot: dict[str, int]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_seq: int
    partial_message: str | None
    result: QAResult | None
    citations: list[dict[str, Any]]
    coverage: dict[str, Any]
    result_visibility: Literal["visible", "partially_unavailable", "withheld"]
    error_code: str | None
    retry_of_run_id: UUID | None


class StepSummary(DTO):
    id: UUID
    logical_step_key: str
    attempt: int
    kind: str
    name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    public_input_summary: dict[str, Any]
    public_output_summary: dict[str, Any]
    source_ids: list[str]
    usage: list[dict[str, Any]]
    error_code: str | None


class MessageView(DTO):
    id: UUID
    run_id: UUID
    role: Literal["user", "assistant"]
    content: str
    is_partial: bool
    result_visibility: Literal["visible", "partially_unavailable", "withheld"]
    result: QAResult | None
    citations: list[dict[str, Any]]
    created_at: datetime


class SourceDetail(DTO):
    id: UUID
    kind: Literal["document"] = "document"
    availability: Literal["available", "deleted", "unavailable"]
    title: str
    excerpt: str | None
    locator: dict[str, Any]
    document_id: UUID
    attempt_id: UUID
    chunk_id: UUID
    observed_at: datetime
    content_hash: str


class TaskDocument(DTO):
    id: UUID
    knowledge_base_id: UUID
    title: str
    attempt_id: UUID


class TaskKnowledge(DTO):
    id: UUID
    name: str


class TaskOptions(DTO):
    agent_id: UUID
    version_number: int
    knowledge_bases: list[TaskKnowledge]
    documents: list[TaskDocument]
    limits: dict[str, int]
