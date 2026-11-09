"""Model settings public contracts; credentials only exist in request schemas."""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from backend.schemas import DTO, Name

ModelKind = Literal["chat", "embedding", "rerank"]
Protocol = Literal[
    "openai-chat", "dashscope-chat", "openai-embeddings", "cohere-rerank-v2", "dashscope-rerank"
]
Positive = Annotated[int, Field(strict=True, ge=1)]


class Limits(DTO):
    max_run_steps: Positive = 10
    max_tool_attempts: Positive = 10
    run_deadline_seconds: Positive = 120
    max_run_tokens: Positive = 16000
    max_upload_bytes: Positive = 20 * 1024 * 1024
    max_active_runs_per_organization: Positive = 2
    max_queued_runs_per_organization: Positive = 20


class LimitsWrite(DTO):
    limits: Limits
    expected_revision: Annotated[int, Field(strict=True, ge=0)]


class LimitsView(DTO):
    system: Limits
    effective: Limits
    editable: list[str]
    revision: int


class ModelConfigWrite(DTO):
    provider_protocol: Protocol
    base_url: Annotated[str, Field(min_length=1, max_length=500)]
    model_name: Name
    credential: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    embedding_dimension: Annotated[int, Field(strict=True, ge=1, le=65536)] | None = None
    expected_revision: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("base_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("An HTTPS provider address without credentials is required")
        if url.query or url.fragment or url.port not in {None, 443}:
            raise ValueError("Provider URL cannot contain query, fragment or a custom port")
        return value

    @field_validator("credential", mode="before")
    @classmethod
    def reject_null_credential(cls, value: object) -> object:
        if value is None or isinstance(value, str) and not value.strip():
            raise ValueError("Omit credential to retain it; an empty value is not a credential")
        return value


class SavedModelTest(DTO):
    connection_id: UUID


class ModelSummary(DTO):
    id: UUID
    kind: ModelKind
    name: str


class ModelConfig(ModelSummary):
    provider_protocol: Protocol
    base_url: str
    model_name: str
    dimension: int | None
    enabled: bool
    has_credential: bool
    last_tested_at: datetime
    test_status: Literal["passed"]
    revision: int
    profile_id: UUID
    tokenizer_id: str | None
    capabilities: dict[str, bool]
    embedding_locked: bool = False


class ModelOption(DTO):
    kind: ModelKind
    provider_protocol: Protocol
    base_urls: list[str]
    supported_chat_models: list[str] = []


class ModelSettingsView(DTO):
    items: list[ModelConfig | ModelSummary]
    options: list[ModelOption] = []


class TestUsage(DTO):
    attempt: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    reserved_tokens: int = 0
    token_source: Literal["actual", "reserved", "unavailable"] = "unavailable"
    billed_units: dict[str, int] = {}
    status: str = "pending"


class TestResult(DTO):
    ok: bool
    tested_at: datetime
    provider: Protocol
    model_name: str
    latency_ms: int
    usage: list[TestUsage]
    error_code: str | None = None
    message: str
