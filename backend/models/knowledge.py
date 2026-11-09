"""Document acceptance, immutable processing attempts and published chunks."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.identity import Base, EditableRow, IdentityRow, now


class KnowledgeBase(IdentityRow, EditableRow, Base):
    __tablename__ = "knowledge_bases"
    organization_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Document(IdentityRow, Base):
    __tablename__ = "documents"
    organization_id: Mapped[UUID]
    knowledge_base_id: Mapped[UUID]
    original_name: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="queued")
    current_attempt_id: Mapped[UUID]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_status: Mapped[str] = mapped_column(Text, default="none")
    cleanup_error: Mapped[str | None] = mapped_column(Text)


class DocumentAttempt(IdentityRow, Base):
    __tablename__ = "document_attempts"
    organization_id: Mapped[UUID]
    document_id: Mapped[UUID]
    attempt_no: Mapped[int] = mapped_column(default=1)
    pipeline_version: Mapped[str] = mapped_column(Text, default="ingest-v1")
    embedding_connection_id: Mapped[UUID]
    embedding_profile_id: Mapped[UUID]
    model_connection_version_id: Mapped[UUID]
    limits_snapshot: Mapped[dict[str, int]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="queued")
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extracted_object_key: Mapped[str | None] = mapped_column(Text)
    extracted_size: Mapped[int | None]
    content_hash: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(default=0)


class DocumentChunk(IdentityRow, Base):
    __tablename__ = "document_chunks"
    organization_id: Mapped[UUID]
    knowledge_base_id: Mapped[UUID]
    document_id: Mapped[UUID]
    attempt_id: Mapped[UUID]
    ordinal: Mapped[int]
    text: Mapped[str | None] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB)
    lexemes: Mapped[str] = mapped_column(TSVECTOR)
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    embedding_profile_id: Mapped[UUID]
    embedding_dimensions: Mapped[int]


class OutboxJob(IdentityRow, Base):
    __tablename__ = "outbox_jobs"
    organization_id: Mapped[UUID]
    job_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[UUID]
    resource_version: Mapped[UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    publish_attempts: Mapped[int] = mapped_column(default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    last_error: Mapped[str | None] = mapped_column(Text)
