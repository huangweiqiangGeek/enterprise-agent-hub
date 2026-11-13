from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StringConstraints, field_validator, model_validator

from backend.github_schemas import RepositoryView
from backend.schemas import DTO, Name, Revision

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "timed_out"]


class ConversationCreate(DTO):
    agent_id: UUID
    task_type: Literal["qa", "compare", "progress"]
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


class ProgressInput(DTO):
    repo_id: UUID
    start_date: date
    end_date: date
    timezone: str = Field(min_length=1, max_length=100)
    knowledge_base_ids: list[UUID] = Field(min_length=1, max_length=10)
    focus: Text | None = None
    question: Text = "总结进展与风险"

    @model_validator(mode="after")
    def valid_range(self) -> "ProgressInput":
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone") from None
        if self.start_date > self.end_date or self.end_date > datetime.now(zone).date():
            raise ValueError("Dates must be ordered and not in the future")
        try:
            self.utc_range()
        except OverflowError:
            raise ValueError("Dates outside supported UTC range") from None
        if len(set(self.knowledge_base_ids)) != len(self.knowledge_base_ids):
            raise ValueError("Duplicate knowledge selectors")
        return self

    def utc_range(self) -> tuple[datetime, datetime]:
        zone = ZoneInfo(self.timezone)
        return (
            datetime.combine(self.start_date, time.min, zone).astimezone(UTC),
            datetime.combine(self.end_date + timedelta(days=1), time.min, zone).astimezone(UTC),
        )


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
    input: QAInput | CompareInput | ProgressInput
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


class ProgressResult(QAResult):
    progress: list[Claim]
    blockers: list[Claim]
    risks: list[Claim]
    to_verify: list[Claim]
    suggestions: list[Claim]
    query_scope: dict[str, Any]


def result_value(value: dict[str, Any]) -> QAResult | CompareResult | ProgressResult:
    if "queryScope" in value or "query_scope" in value:
        return ProgressResult.model_validate(value)
    return (
        CompareResult.model_validate(value) if "items" in value else QAResult.model_validate(value)
    )


class ResultBlock(DTO):
    type: Literal[
        "summary", "claim", "conflict", "unknown", "suggestion", "comparison", "blocker", "risk"
    ]
    item: CompareItem | None = None
    text: Text
    kind: Literal["fact", "inference", "suggestion", "unknown"] = "unknown"
    source_ids: list[UUID] = Field(default_factory=list, max_length=80)


class RunSnapshot(DTO):
    id: UUID
    conversation_id: UUID
    task_type: Literal["qa", "compare", "progress"] = "qa"
    status: RunStatus
    cancel_requested: bool
    input_summary: str
    input: QAInput | CompareInput | ProgressInput
    deadline_at: datetime | None
    config_version: dict[str, Any]
    budget_snapshot: dict[str, int]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_seq: int
    partial_message: str | None
    result: QAResult | CompareResult | ProgressResult | None
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
    result: QAResult | CompareResult | ProgressResult | None
    citations: list[dict[str, Any]]
    created_at: datetime


class SourceDetail(DTO):
    id: UUID
    kind: Literal["document", "github"] = "document"
    availability: Literal["available", "deleted", "unavailable"]
    title: str
    excerpt: str | None
    locator: dict[str, Any]
    document_id: UUID | None = None
    attempt_id: UUID | None = None
    chunk_id: UUID | None = None
    repo_id: UUID | None = None
    item_type: str | None = None
    number: int | None = None
    html_url: str | None = None
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
    repositories: list[RepositoryView] = Field(default_factory=list)
    limits: dict[str, int]
