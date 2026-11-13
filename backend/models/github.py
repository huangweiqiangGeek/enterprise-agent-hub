"""Organization-scoped GitHub credentials and explicit repository grants."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.identity import Base, IdentityRow


class GitHubConnection(IdentityRow, Base):
    __tablename__ = "github_connections"
    __table_args__ = (UniqueConstraint("organization_id"),)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    name: Mapped[str] = mapped_column(Text)
    credential_value: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    test_result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class GitHubRepository(IdentityRow, Base):
    __tablename__ = "github_repositories"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("app.github_connections.id"))
    github_repo_id: Mapped[int] = mapped_column(BigInteger)
    owner: Mapped[str] = mapped_column(Text)
    repo: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrgToolSetting(Base):
    __tablename__ = "org_tool_settings"
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.organizations.id"), primary_key=True
    )
    tool_key: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
