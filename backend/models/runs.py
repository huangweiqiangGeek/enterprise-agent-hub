"""Durable run state. Database constraints are owned by migration 0007."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.identity import Base, IdentityRow, now


class Conversation(IdentityRow, Base):
    __tablename__ = "conversations"
    organization_id: Mapped[UUID]
    owner_user_id: Mapped[UUID]
    agent_id: Mapped[UUID]
    task_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(IdentityRow, Base):
    __tablename__ = "runs"
    organization_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    initiator_id: Mapped[UUID]
    agent_version_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(Text, default="queued")
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    budget_snapshot: Mapped[dict[str, int]] = mapped_column(JSONB)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    retry_of_run_id: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(Text)
    graph_version: Mapped[str] = mapped_column(Text, default="qa-v1")


class Message(IdentityRow, Base):
    __tablename__ = "messages"
    organization_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    run_id: Mapped[UUID]
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_partial: Mapped[bool] = mapped_column(default=True)


class RunStep(IdentityRow, Base):
    __tablename__ = "run_steps"
    organization_id: Mapped[UUID]
    run_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(BigInteger)
    logical_step_key: Mapped[str] = mapped_column(Text)
    attempt_no: Mapped[int]
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    input_digest: Mapped[str] = mapped_column(Text)
    input_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="running")
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int]


class RunSource(IdentityRow, Base):
    __tablename__ = "run_sources"
    organization_id: Mapped[UUID]
    run_id: Mapped[UUID]
    identity_key: Mapped[str] = mapped_column(Text)
    document_id: Mapped[UUID]
    attempt_id: Mapped[UUID]
    chunk_id: Mapped[UUID]
    title: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RunCitation(IdentityRow, Base):
    __tablename__ = "run_citations"
    organization_id: Mapped[UUID]
    run_id: Mapped[UUID]
    message_id: Mapped[UUID]
    source_id: Mapped[UUID]
    label: Mapped[int]
    claim_path: Mapped[str] = mapped_column(Text)


class RunEvent(Base):
    __tablename__ = "run_events"
    organization_id: Mapped[UUID]
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
