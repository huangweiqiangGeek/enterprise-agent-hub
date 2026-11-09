from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from backend.run_schemas import ConversationView, RunAccepted
from backend.schemas import DTO, Name, Revision

DocumentStatus = Literal["queued", "processing", "ready", "failed"]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class KnowledgeWrite(DTO):
    name: Name
    description: Description = ""


class KnowledgeUpdate(KnowledgeWrite):
    expected_revision: Revision


class KnowledgeDTO(DTO):
    id: UUID
    name: str
    description: str
    document_count: int
    ready_count: int
    processing_count: int
    failed_count: int
    revision: int
    created_at: datetime
    updated_at: datetime


class DocumentDTO(DTO):
    id: UUID
    knowledge_base_id: UUID
    file_name: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    current_attempt_id: UUID
    attempt_number: int
    chunk_count: int
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    ready_at: datetime | None = None
    deleted_at: datetime | None = None
    cleanup_status: str | None = None


class AttemptDTO(DTO):
    id: UUID
    attempt_number: int
    status: str
    created_at: datetime
    error_code: str | None


class DocumentDetail(DTO):
    document: DocumentDTO
    attempts: list[AttemptDTO]


class AcceptanceDTO(DTO):
    status: Literal["accepted", "absent"]
    target_id: UUID | None = None
    document: DocumentDTO | None = None
    run: RunAccepted | None = None
    conversation: ConversationView | None = None


class UploadPolicy(DTO):
    max_bytes: int
    extensions: list[str] = Field(default_factory=lambda: ["pdf", "txt", "md"])
    embedding_ready: bool
    max_documents: int


class RetryDocument(DTO):
    expected_attempt_id: UUID


class TextPage(DTO):
    document_id: UUID
    attempt_id: UUID
    text: str
    next_cursor: str | None


class ChunkDTO(DTO):
    chunk_id: UUID
    ordinal: int
    text: str
    locator: dict[str, Any]


class ChunkPreview(DTO):
    document_id: UUID
    attempt_id: UUID
    items: list[ChunkDTO]
    next_cursor: str | None
