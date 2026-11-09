"""Identity tables; ownership has one source of truth on Organization."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(schema="app")


class IdentityRow:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EditableRow:
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)


class User(IdentityRow, EditableRow, Base):
    __tablename__ = "users"
    normalized_email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthSession(IdentityRow, Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_expires", "user_id", "expires_at"),)
    # Anonymous sessions only provide pre-login CSRF; never authenticate a user.
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("app.users.id"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    csrf_hash: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Organization(IdentityRow, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id", "owner_user_id"],
            ["app.org_memberships.organization_id", "app.org_memberships.user_id"],
            name="fk_organizations_owner_membership",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )
    name: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[UUID]
    access_revision: Mapped[int] = mapped_column(BigInteger, default=1)
    settings_revision: Mapped[int] = mapped_column(BigInteger, default=1)


class Membership(IdentityRow, EditableRow, Base):
    __tablename__ = "org_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_org_membership"),
        CheckConstraint("base_role IN ('admin', 'member')", name="ck_membership_role"),
        Index("ix_memberships_user", "user_id"),
        Index("ix_memberships_page", "organization_id", "created_at", "id"),
    )
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app.users.id"))
    base_role: Mapped[str] = mapped_column(Text, default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IdempotencyKey(IdentityRow, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "actor_id",
            "operation",
            "resource_id",
            "key",
            name="uq_idempotency_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("app.organizations.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("app.users.id"))
    operation: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[UUID | None]
    key: Mapped[str] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(Text)
    response_status: Mapped[int]
    response_data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    target_id: Mapped[UUID]


class AuditEntry(IdentityRow, Base):
    __tablename__ = "audit_entries"
    __table_args__ = (Index("ix_audit_org_created", "organization_id", "created_at"),)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("app.users.id"))
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[UUID]
    before_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    after_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    request_id: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    outcome: Mapped[str] = mapped_column(Text, default="succeeded")
