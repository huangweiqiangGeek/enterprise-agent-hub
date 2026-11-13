"""Configuration writes are serialized with resource changes by the organization lock."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.agent_catalog import REQUIRED, TOOLS, options
from backend.agent_schemas import (
    AgentClone,
    AgentConfig,
    AgentOptions,
    AgentStatus,
    AgentSummary,
    AgentUpdate,
    AgentWrite,
    ReadinessIssue,
    TaskType,
)
from backend.config import Settings
from backend.models.agents import Agent, AgentVersion, AgentVersionKB, AgentVersionTool
from backend.models.github import GitHubConnection, GitHubRepository, OrgToolSetting
from backend.models.identity import AuditEntry, Organization, now
from backend.repositories.agents import AgentRepository
from backend.repositories.model_settings import ModelRepository
from backend.schemas import Page
from backend.services.errors import AppError
from backend.services.identity import IdentityService, PageCursor
from backend.services.model_settings import SessionProof


class AgentService:
    def __init__(
        self, engine: AsyncEngine, settings: Settings, request_id: str, proof: SessionProof
    ):
        self.engine, self.settings, self.request_id, self.proof = (
            engine,
            settings,
            request_id,
            proof,
        )

    @asynccontextmanager
    async def access(
        self, org: UUID, *, write: bool = False
    ) -> AsyncIterator[tuple[IdentityService, Organization, bool]]:
        async with IdentityService.open(self.engine, self.settings, self.request_id) as identity:
            await identity.authenticate(self.proof.token, self.proof.csrf_cookie)
            if write:
                identity.check_csrf(self.proof.origin, self.proof.csrf_header)
            row, member = await identity.organization(org)
            admin = identity.role(row, member) in {"owner", "admin"}
            if write:
                identity.require_role(row, member, {"owner", "admin"})
            yield identity, row, admin

    async def catalog(self, org: UUID) -> AgentOptions:
        async with self.access(org) as (_, _, admin):
            if not admin:
                raise AppError(403, "FORBIDDEN", "只有管理员可以配置Agent。")
            return options()

    async def references(self, repo: AgentRepository, value: AgentWrite) -> None:
        if set(await repo.valid_kbs(value.knowledge_base_ids)) != set(value.knowledge_base_ids):
            raise AppError(404, "NOT_FOUND", "所选知识库不存在或不可见。")
        if value.chat_connection_id and not await repo.chat(value.chat_connection_id):
            raise AppError(404, "NOT_FOUND", "所选聊天模型不存在或不可见。")

    async def readiness(self, repo: AgentRepository, value: AgentWrite) -> list[ReadinessIssue]:
        issues: list[ReadinessIssue] = []

        def missing(code: str, message: str, field: str, action: str) -> None:
            issues.append(ReadinessIssue(code=code, message=message, field=field, action=action))

        if not value.prompt.strip():
            missing("PROMPT_REQUIRED", "请填写提示词。", "prompt", "填写提示词")
        if not value.knowledge_base_ids:
            missing(
                "KNOWLEDGE_REQUIRED", "请至少选择一个知识库。", "knowledgeBaseIds", "选择知识库"
            )
        valid = await repo.valid_kbs(value.knowledge_base_ids)
        if set(valid) != set(value.knowledge_base_ids):
            missing(
                "KNOWLEDGE_UNAVAILABLE",
                "绑定知识库已不可用，请重新选择。",
                "knowledgeBaseIds",
                "调整知识范围",
            )
        chat = await repo.chat(value.chat_connection_id)
        if not chat or not chat.enabled:
            missing(
                "CHAT_REQUIRED",
                "请选择已测试并启用的聊天模型。",
                "chatConnectionId",
                "配置聊天模型",
            )
        models = ModelRepository(repo.db, repo.org)
        embedding = await models.connection("embedding")
        rerank = await models.connection("rerank")
        if not embedding or not embedding.enabled:
            missing(
                "EMBEDDING_REQUIRED",
                "组织尚无可用Embedding模型。",
                "knowledgeBaseIds",
                "配置Embedding",
            )
        elif valid and not await repo.ready_document(valid, embedding.active_profile_id):
            missing(
                "DOCUMENT_REQUIRED",
                "所选知识库需要至少一份当前已就绪资料。",
                "knowledgeBaseIds",
                "上传并完成文档处理",
            )
        if not rerank or not rerank.enabled:
            missing("RERANK_REQUIRED", "组织尚无可用Rerank模型。", "knowledgeBaseIds", "配置Rerank")
        if any(key not in value.tool_keys for key in REQUIRED[value.task_type]):
            missing(
                "TOOLS_REQUIRED", "缺少此任务必需的只读工具。", "toolKeys", "选择标注为必需的工具"
            )
        github_keys = [key for key in value.tool_keys if key.startswith("github.")]
        if github_keys or value.task_type == "progress":
            connection = await repo.db.scalar(
                select(GitHubConnection).where(GitHubConnection.organization_id == repo.org)
            )
            grant = await repo.db.scalar(
                select(GitHubRepository.id)
                .where(
                    GitHubRepository.organization_id == repo.org, GitHubRepository.enabled.is_(True)
                )
                .limit(1)
            )
            disabled = list(
                await repo.db.scalars(
                    select(OrgToolSetting.tool_key).where(
                        OrgToolSetting.organization_id == repo.org,
                        OrgToolSetting.enabled.is_(False),
                    )
                )
            )
            if (
                not connection
                or not connection.enabled
                or not connection.test_result.get("ok")
                or not grant
            ):
                missing(
                    "GITHUB_UNAVAILABLE",
                    "请测试启用GitHub只读连接并明确共享至少一个仓库。",
                    "toolKeys",
                    "配置GitHub连接与仓库",
                )
            if any(key in disabled for key in github_keys):
                missing(
                    "TOOL_DISABLED",
                    "所选GitHub工具已被组织停用。",
                    "toolKeys",
                    "调整工具或联系管理员",
                )
        return issues

    async def config(self, repo: AgentRepository, row: Agent) -> AgentConfig:
        version = await repo.version(row)
        kbs, tools = await repo.bindings(version.id)
        value = AgentWrite.model_validate(
            {
                "name": row.name,
                "description": row.description,
                "taskType": row.task_type,
                "prompt": version.prompt_text,
                "knowledgeBaseIds": kbs,
                "toolKeys": tools,
                "chatConnectionId": version.chat_connection_id,
                "enabled": row.enabled,
            }
        )
        return AgentConfig(
            **value.model_dump(),
            id=row.id,
            current_version_id=version.id,
            version_number=version.version_no,
            revision=row.revision,
            readiness=await self.readiness(repo, value),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def summary(config: AgentConfig) -> AgentSummary:
        # Explicit projection: members never receive the prompt, bindings or model connection.
        return AgentSummary(**{key: getattr(config, key) for key in AgentSummary.model_fields})

    async def detail(self, org: UUID, agent_id: UUID) -> AgentConfig | AgentSummary:
        async with self.access(org) as (identity, _, admin):
            repo = AgentRepository(identity.db, org)
            row = await repo.agent(agent_id)
            if not admin and not row.enabled:
                raise AppError(404, "AGENT_UNAVAILABLE", "此Agent已停用或不可用。")
            value = await self.config(repo, row)
            return value if admin else self.summary(value)

    async def list_agents(
        self,
        org: UUID,
        q: str,
        task: TaskType | None,
        enabled: bool | None,
        limit: int,
        cursor: str | None,
    ) -> Page[AgentSummary]:
        async with self.access(org) as (identity, organization, admin):
            query = select(Agent).where(
                Agent.organization_id == org, Agent.name.icontains(q, autoescape=True)
            )
            if not admin:
                query = query.where(Agent.enabled.is_(True))
            elif enabled is not None:
                query = query.where(Agent.enabled == enabled)
            if task:
                query = query.where(Agent.task_type == task)
            if cursor:
                point = identity.decode_cursor(cursor, org, organization.access_revision)
                query = query.where(
                    tuple_(Agent.created_at, Agent.id) < (point.created_at, point.id)
                )
            rows = list(
                await identity.db.scalars(
                    query.order_by(Agent.created_at.desc(), Agent.id.desc()).limit(limit + 1)
                )
            )
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = identity.encode_cursor(
                    PageCursor(
                        organization_id=org,
                        actor_id=identity.require_user().id,
                        access_revision=organization.access_revision,
                        created_at=last.created_at,
                        id=last.id,
                    )
                )
            repo = AgentRepository(identity.db, org)
            return Page(
                items=[self.summary(await self.config(repo, row)) for row in rows[:limit]],
                next_cursor=next_cursor,
            )

    def audit(self, identity: IdentityService, org: UUID, row: Agent, action: str) -> None:
        identity.db.add(
            AuditEntry(
                organization_id=org,
                actor_id=identity.require_user().id,
                action=action,
                target_type="agent",
                target_id=row.id,
                request_id=self.request_id,
                after_summary={
                    "versionId": str(row.current_version_id),
                    "revision": row.revision,
                    "enabled": row.enabled,
                },
            )
        )

    async def persist(
        self,
        identity: IdentityService,
        organization: Organization,
        value: AgentWrite,
        row: Agent | None = None,
    ) -> AgentConfig:
        repo = AgentRepository(identity.db, organization.id)
        await self.references(repo, value)
        issues = await self.readiness(repo, value)
        if value.enabled and issues:
            raise AppError(
                409,
                "AGENT_NOT_READY",
                "请先完成启用条件：" + " ".join(i.message for i in issues),
                {"readiness": [i.model_dump(by_alias=True) for i in issues]},
            )
        number = (await repo.version(row)).version_no + 1 if row else 1
        version_id = uuid4()
        if row is None:
            row = Agent(
                id=uuid4(),
                organization_id=organization.id,
                name=value.name,
                description=value.description,
                task_type=value.task_type,
                enabled=value.enabled,
                current_version_id=version_id,
                disabled_at=None if value.enabled else now(),
            )
            identity.db.add(row)
        else:
            row.name, row.description = value.name, value.description
            row.current_version_id = version_id
            row.revision += 1
            row.updated_at = now()
            if row.enabled != value.enabled:
                row.disabled_at = None if value.enabled else now()
            row.enabled = value.enabled
        await identity.db.flush()
        identity.db.add(
            AgentVersion(
                id=version_id,
                organization_id=organization.id,
                agent_id=row.id,
                version_no=number,
                name=value.name,
                description=value.description,
                prompt_text=value.prompt,
                chat_connection_id=value.chat_connection_id,
                created_by=identity.require_user().id,
            )
        )
        await identity.db.flush()
        identity.db.add_all(
            [
                AgentVersionKB(
                    organization_id=organization.id,
                    agent_version_id=version_id,
                    knowledge_base_id=key,
                )
                for key in value.knowledge_base_ids
            ]
        )
        identity.db.add_all(
            [
                AgentVersionTool(
                    organization_id=organization.id, agent_version_id=version_id, tool_key=key
                )
                for key in value.tool_keys
            ]
        )
        await identity.db.flush()
        organization.access_revision += 1
        self.audit(identity, organization.id, row, "agent.saved")
        return await self.config(repo, row)

    async def save(
        self, org: UUID, value: AgentWrite, *, key: str = "", agent_id: UUID | None = None
    ) -> AgentConfig:
        async with self.access(org, write=True) as (identity, organization, _):
            repo = AgentRepository(identity.db, org)
            if agent_id:
                row = await repo.agent(agent_id)
                assert isinstance(value, AgentUpdate)
                identity.check_revision(value.expected_revision, row.revision)
                if row.task_type != value.task_type:
                    raise AppError(
                        409, "TASK_TYPE_IMMUTABLE", "创建后不能修改任务类型，请新建Agent。"
                    )
            else:
                replay = await identity.replay(
                    org, "agent.create", key, value.model_dump(mode="json")
                )
                if replay:
                    return await self.config(repo, await repo.agent(replay.target_id))
                row = None
            result = await self.persist(identity, organization, value, row)
            if agent_id is None:
                identity.remember(
                    org, "agent.create", key, value.model_dump(mode="json"), 201, result, result.id
                )
            return result

    async def status(self, org: UUID, agent_id: UUID, value: AgentStatus) -> AgentSummary:
        async with self.access(org, write=True) as (identity, organization, _):
            repo = AgentRepository(identity.db, org)
            row = await repo.agent(agent_id)
            identity.check_revision(value.expected_revision, row.revision)
            config = await self.config(repo, row)
            if value.enabled and config.readiness:
                raise AppError(
                    409,
                    "AGENT_NOT_READY",
                    "请先完成启用条件：" + " ".join(i.message for i in config.readiness),
                    {"readiness": [i.model_dump(by_alias=True) for i in config.readiness]},
                )
            if row.enabled != value.enabled:
                row.enabled, row.disabled_at = value.enabled, None if value.enabled else now()
                row.revision += 1
                row.updated_at = now()
                organization.access_revision += 1
                self.audit(
                    identity, org, row, "agent.enabled" if value.enabled else "agent.disabled"
                )
                await identity.db.flush()
            return self.summary(await self.config(repo, row))

    async def clone(self, org: UUID, source_id: UUID, value: AgentClone, key: str) -> AgentConfig:
        async with self.access(org, write=True) as (identity, organization, _):
            repo = AgentRepository(identity.db, org)
            payload = {"sourceId": str(source_id), **value.model_dump(mode="json")}
            replay = await identity.replay(org, "agent.clone", key, payload)
            if replay:
                result = await self.config(repo, await repo.agent(replay.target_id))
                result.removed_references = replay.response_data.get("removedReferences", [])
                return result
            source = await self.config(repo, await repo.agent(source_id))
            kbs = await repo.valid_kbs(source.knowledge_base_ids)
            chat = await repo.chat(source.chat_connection_id)
            tools = [tool.key for tool in TOOLS if tool.available and tool.key in source.tool_keys]
            github_unavailable = any(
                issue.code == "GITHUB_UNAVAILABLE" for issue in source.readiness
            )
            disabled = list(
                await repo.db.scalars(
                    select(OrgToolSetting.tool_key).where(
                        OrgToolSetting.organization_id == org, OrgToolSetting.enabled.is_(False)
                    )
                )
            )
            tools = [
                key
                for key in tools
                if key not in disabled and not (github_unavailable and key.startswith("github."))
            ]
            result = await self.persist(
                identity,
                organization,
                AgentWrite(
                    name=value.new_name,
                    description=source.description,
                    task_type=source.task_type,
                    prompt=source.prompt,
                    knowledge_base_ids=kbs,
                    tool_keys=tools,
                    chat_connection_id=chat.id if chat and chat.enabled else None,
                ),
            )
            if len(kbs) != len(source.knowledge_base_ids):
                result.removed_references.append("已剔除不可用知识库，请核对知识范围。")
            if len(tools) != len(source.tool_keys):
                result.removed_references.append("已剔除尚不可用的工具，请重新配置。")
            if source.chat_connection_id and not result.chat_connection_id:
                result.removed_references.append("已剔除不可用聊天模型。")
            identity.remember(org, "agent.clone", key, payload, 201, result, result.id)
            return result
