"""Read-only, explicitly projected workbench contract."""

from datetime import datetime
from uuid import UUID

from backend.agent_schemas import TaskType
from backend.run_schemas import ConversationSummary
from backend.schemas import DTO, Role


class DashboardIssue(DTO):
    code: str
    message: str
    action_label: str
    action_path: str | None


class DashboardTemplate(DTO):
    task_type: TaskType
    name: str
    description: str
    input_description: str
    output_description: str
    ready: bool
    ready_agent_count: int
    issues: list[DashboardIssue]


class DashboardView(DTO):
    organization_id: UUID
    organization_name: str
    effective_role: Role
    observed_at: datetime
    templates: list[DashboardTemplate]
    recent_conversations: list[ConversationSummary]
