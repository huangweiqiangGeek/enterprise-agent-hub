"""Current Agent identity and append-only configuration versions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.identity import Base, EditableRow, IdentityRow


class Agent(IdentityRow, EditableRow, Base):
    __tablename__ = "agents"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("app.organizations.id"))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool]
    current_version_id: Mapped[UUID]
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentVersion(IdentityRow, Base):
    __tablename__ = "agent_versions"
    organization_id: Mapped[UUID]
    agent_id: Mapped[UUID]
    version_no: Mapped[int]
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    prompt_text: Mapped[str] = mapped_column(Text)
    chat_connection_id: Mapped[UUID | None]
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("app.users.id"))


class AgentVersionKB(Base):
    __tablename__ = "agent_version_kbs"
    organization_id: Mapped[UUID]
    agent_version_id: Mapped[UUID] = mapped_column(primary_key=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(primary_key=True)


class AgentVersionTool(Base):
    __tablename__ = "agent_version_tools"
    organization_id: Mapped[UUID]
    agent_version_id: Mapped[UUID] = mapped_column(primary_key=True)
    tool_key: Mapped[str] = mapped_column(Text, primary_key=True)
