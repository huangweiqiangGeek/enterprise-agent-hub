from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from backend.schemas import DTO, Name, Revision

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "timed_out"]


class ConversationCreate(DTO):
    agent_id: UUID
    task_type: Literal["qa", "compare"]
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


class CompareInput(DTO):
    requirement_document_id: UUID
    solution_document_id: UUID
    focus: Text | None = None
    question: Text = "对比需求与方案"

    @model_validator(mode="after")
    def different(self) -> "CompareInput":
        if self.requirement_document_id == self.solution_document_id:
            raise ValueError("Select two different documents")
        return self


class CompareItem(DTO):
    requirement: Text
    requirement_source_ids: list[UUID] = Field(min_length=1, max_length=8)
    solution_evidence: Text
    solution_source_ids: list[UUID] = Field(default_factory=list, max_length=8)
    status: Literal["covered", "partial", "not_found", "conflict"]
    impact: Text
    suggestion: Text


class RunCreate(DTO):
    agent_id: UUID
    input: QAInput | CompareInput
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


class CompareResult(QAResult):
    summary: str
    items: list[CompareItem]


def result_value(value: dict[str, Any]) -> QAResult | CompareResult:
    return (
        CompareResult.model_validate(value) if "items" in value else QAResult.model_validate(value)
    )


class ResultBlock(DTO):
    type: Literal["summary", "claim", "conflict", "unknown", "suggestion", "comparison"]
    item: CompareItem | None = None
    text: Text
    kind: Literal["fact", "inference", "suggestion", "unknown"] = "unknown"
    source_ids: list[UUID] = Field(default_factory=list, max_length=80)


class RunSnapshot(DTO):
    id: UUID
    conversation_id: UUID
    task_type: Literal["qa", "compare"] = "qa"
    status: RunStatus
    cancel_requested: bool
    input_summary: str
    input: QAInput | CompareInput
    deadline_at: datetime | None
    config_version: dict[str, Any]
    budget_snapshot: dict[str, int]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_seq: int
    partial_message: str | None
    result: QAResult | CompareResult | None
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
    result: QAResult | CompareResult | None
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
