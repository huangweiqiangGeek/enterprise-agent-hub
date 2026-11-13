"""Dashboard readiness is a current DB observation, never a provider availability promise."""

from uuid import UUID

from sqlalchemy import select

from backend.agent_catalog import REQUIRED, options
from backend.agent_schemas import AgentWrite, ReadinessIssue, TaskType
from backend.dashboard_schemas import DashboardIssue, DashboardTemplate, DashboardView
from backend.models.agents import Agent
from backend.models.identity import now
from backend.models.knowledge import KnowledgeBase
from backend.models.runs import Conversation
from backend.repositories.agents import AgentRepository
from backend.repositories.model_settings import ModelRepository
from backend.services.agents import AgentService
from backend.services.dialogue import DialogueService

DESCRIPTIONS: dict[TaskType, tuple[str, str]] = {
    "qa": ("问题及至少一个含就绪资料的知识库，可限定文档。", "附来源的回答、冲突和资料不足说明。"),
    "compare": (
        "同一 Agent 范围内两份不同的就绪文档，可补充关注点。",
        "逐项需求与方案覆盖、差异、风险建议及双侧引用。",
    ),
    "progress": (
        "一个已共享仓库、日期与时区，以及就绪计划资料。",
        "进展、阻塞、风险、待核实和建议，附查询范围与来源。",
    ),
}


class DashboardService(AgentService):
    @staticmethod
    def issue(org: UUID, admin: bool, issue: ReadinessIssue, task: TaskType) -> DashboardIssue:
        # Fixed internal paths only; no resource identifiers, names or external URLs from config.
        if issue.code == "AGENT_REQUIRED":
            path = f"agents/new?taskType={task}"
        elif issue.code in {"CHAT_REQUIRED", "EMBEDDING_REQUIRED", "RERANK_REQUIRED"}:
            path = "settings/models"
        elif issue.code in {"DOCUMENT_REQUIRED", "COMPARE_DOCUMENTS_REQUIRED"}:
            path = "knowledge"
        elif issue.code in {"GITHUB_UNAVAILABLE", "TOOL_DISABLED"}:
            path = "connections"
        else:
            path = "agents"
        return DashboardIssue(
            code=issue.code,
            message=issue.message,
            action_label=issue.action if admin else "联系管理员",
            action_path=f"/o/{org}/{path}" if admin else None,
        )

    async def prerequisites(self, repo: AgentRepository, value: AgentWrite) -> list[ReadinessIssue]:
        issues = await self.readiness(repo, value)
        embedding = await ModelRepository(repo.db, repo.org).connection("embedding")
        enough = bool(
            embedding
            and embedding.enabled
            and await repo.ready_document(
                value.knowledge_base_ids,
                embedding.active_profile_id,
                minimum=2 if value.task_type == "compare" else 1,
            )
        )
        if not enough:
            issues = [i for i in issues if i.code != "DOCUMENT_REQUIRED"]
            issues.append(
                ReadinessIssue(
                    code="COMPARE_DOCUMENTS_REQUIRED"
                    if value.task_type == "compare"
                    else "DOCUMENT_REQUIRED",
                    message="同一 Agent 知识范围内需要两份不同、当前模型下已就绪的资料。"
                    if value.task_type == "compare"
                    else "需要至少一份当前模型下已就绪的资料。",
                    field="knowledgeBaseIds",
                    action="上传并完成文档处理",
                )
            )
        return issues

    async def dashboard(self, org: UUID) -> DashboardView:
        async with self.access(org) as (identity, organization, admin):
            _, member = await identity.organization(org)
            repo = AgentRepository(identity.db, org)
            models = ModelRepository(identity.db, org)
            chat = await models.connection("chat")
            # Only used to describe organization prerequisites when there is no enabled candidate.
            kb_ids = list(
                await identity.db.scalars(
                    select(KnowledgeBase.id)
                    .where(KnowledgeBase.organization_id == org, KnowledgeBase.deleted_at.is_(None))
                    .order_by(KnowledgeBase.id)
                )
            )
            rows = list(
                await identity.db.scalars(
                    select(Agent)
                    .where(Agent.organization_id == org, Agent.enabled.is_(True))
                    .order_by(Agent.id)
                )
            )
            templates = []
            for template in options().templates:
                candidates = [a for a in rows if a.task_type == template.task_type]
                issues: list[ReadinessIssue] = []
                ready_count = 0
                for row in candidates:
                    config = await self.config(repo, row)
                    value = AgentWrite.model_validate(
                        {key: getattr(config, key) for key in AgentWrite.model_fields}
                    )
                    current = await self.prerequisites(repo, value)
                    if not current:
                        ready_count += 1
                    issues.extend(current)
                if not candidates:
                    probe = AgentWrite(
                        name=template.name,
                        task_type=template.task_type,
                        prompt=template.prompt,
                        knowledge_base_ids=kb_ids[:100],
                        tool_keys=REQUIRED[template.task_type],
                        chat_connection_id=chat.id if chat else None,
                    )
                    issues = await self.prerequisites(repo, probe)
                    embedding = await models.connection("embedding")
                    if (
                        embedding
                        and embedding.enabled
                        and await repo.ready_document(
                            kb_ids,
                            embedding.active_profile_id,
                            minimum=2 if template.task_type == "compare" else 1,
                        )
                    ):
                        issues = [
                            i
                            for i in issues
                            if i.code not in {"DOCUMENT_REQUIRED", "COMPARE_DOCUMENTS_REQUIRED"}
                        ]
                    issues.append(
                        ReadinessIssue(
                            code="AGENT_REQUIRED",
                            message="此任务尚无已启用的 Agent。",
                            field="agent",
                            action="配置并启用 Agent",
                        )
                    )
                elif not ready_count:
                    issues.append(
                        ReadinessIssue(
                            code="AGENT_NOT_READY",
                            message="此任务尚无满足全部准备条件的已启用 Agent。",
                            field="agent",
                            action="检查 Agent 配置",
                        )
                    )
                # One usable candidate suffices; unrelated damaged candidates do not block it.
                unique = {i.code: i for i in issues} if not ready_count else {}
                inputs, outputs = DESCRIPTIONS[template.task_type]
                templates.append(
                    DashboardTemplate(
                        task_type=template.task_type,
                        name=template.name,
                        description=template.description,
                        input_description=inputs,
                        output_description=outputs,
                        ready=ready_count > 0,
                        ready_agent_count=ready_count,
                        issues=[
                            self.issue(org, admin, i, template.task_type) for i in unique.values()
                        ],
                    )
                )
            conversations = list(
                await identity.db.scalars(
                    select(Conversation)
                    .where(
                        Conversation.organization_id == org,
                        Conversation.owner_user_id == identity.require_user().id,
                    )
                    .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                    .limit(5)
                )
            )
            dialogue = DialogueService(self.engine, self.settings, self.request_id, self.proof)
            summaries = []
            for conversation in conversations:
                summary = await dialogue.conversation_summary(identity.db, conversation)
                agent = await identity.db.get(Agent, conversation.agent_id)
                if not admin and (not agent or not agent.enabled):
                    summary.agent_name = "已停用 Agent"
                summaries.append(summary)
            return DashboardView(
                organization_id=org,
                organization_name=organization.name,
                effective_role=identity.role(organization, member),
                observed_at=now(),
                templates=templates,
                recent_conversations=summaries,
            )
