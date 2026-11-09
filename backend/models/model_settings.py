"""Versioned model configuration and per-attempt metering."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.identity import Base, IdentityRow


class OrgSettings(Base):
    __tablename__ = "org_settings"
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.organizations.id"), primary_key=True
    )
    limits: Mapped[dict[str, int]] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)


class ModelConnection(IdentityRow, Base):
    __tablename__ = "model_connections"
    __table_args__ = (UniqueConstraint("organization_id", "kind"),)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    active_profile_id: Mapped[UUID]
    current_connection_version_id: Mapped[UUID]
    enabled: Mapped[bool] = mapped_column(default=True)
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    test_result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)


class ConnectionVersion(IdentityRow, Base):
    __tablename__ = "model_connection_versions"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("app.model_connections.id"))
    version_no: Mapped[int]
    provider_protocol: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    credential_value: Mapped[str] = mapped_column(Text)


class ModelProfile(IdentityRow, Base):
    __tablename__ = "model_profiles"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("app.model_connections.id"))
    provider_protocol: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(Text)
    dimension: Mapped[int | None]
    tokenizer_id: Mapped[str | None] = mapped_column(Text)
    capabilities: Mapped[dict[str, bool]] = mapped_column(JSONB)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int]


class UsageEntry(IdentityRow, Base):
    __tablename__ = "usage_entries"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    run_id: Mapped[UUID | None]
    step_id: Mapped[UUID | None]
    operation_kind: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[UUID]
    attempt_no: Mapped[int]
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    # Unsaved candidates have no profile yet; never attribute their usage to the active profile.
    model_profile_id: Mapped[UUID | None] = mapped_column(ForeignKey("app.model_profiles.id"))
    reserved_tokens: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    token_source: Mapped[str] = mapped_column(Text)
    billed_units: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    duration_ms: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(Text)
