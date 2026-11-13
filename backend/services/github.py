"""D13 management. External calls occur outside DB locks, followed by reauthorization."""

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select

from backend.agent_catalog import TOOLS
from backend.github_schemas import (
    GitHubConnectionView,
    GitHubSummary,
    GitHubTestInput,
    GitHubTestResult,
    GitHubWrite,
    RepositoryAdd,
    RepositoryView,
    ToolView,
    ToolWrite,
)
from backend.models.agents import Agent, AgentVersionTool
from backend.models.github import GitHubConnection, GitHubRepository, OrgToolSetting
from backend.models.identity import AuditEntry, now
from backend.services.agents import AgentService
from backend.services.errors import AppError
from backend.services.github_mcp import GitHubMCP
from backend.services.identity import IdentityService


class GitHubService(AgentService):
    async def row(self, identity: IdentityService, org: UUID) -> GitHubConnection | None:
        return cast(
            GitHubConnection | None,
            await identity.db.scalar(
                select(GitHubConnection).where(GitHubConnection.organization_id == org)
            ),
        )

    @staticmethod
    def view(row: GitHubConnection | None, admin: bool) -> GitHubConnectionView | GitHubSummary:
        summary = GitHubSummary(
            enabled=bool(row and row.enabled),
            available=bool(row and row.enabled and row.test_result.get("ok")),
        )
        if not admin:
            return summary
        return GitHubConnectionView(
            **summary.model_dump(),
            name=row.name if row else "GitHub",
            has_credential=bool(row and row.credential_value),
            revision=row.revision if row else 0,
            last_tested_at=row.tested_at if row else None,
            test_result=GitHubTestResult.model_validate(row.test_result)
            if row and row.test_result
            else None,
        )

    def record(self, identity: IdentityService, org: UUID, target: UUID, action: str) -> None:
        identity.db.add(
            AuditEntry(
                organization_id=org,
                actor_id=identity.require_user().id,
                action=action,
                target_type="github",
                target_id=target,
                request_id=self.request_id,
                after_summary={"action": action},
            )
        )

    async def connection(self, org: UUID) -> GitHubConnectionView | GitHubSummary:
        async with self.access(org) as (identity, _, admin):
            return self.view(await self.row(identity, org), admin)

    async def test_connection(
        self, org: UUID, value: GitHubTestInput, mcp: GitHubMCP
    ) -> GitHubTestResult:
        async with self.access(org, write=True) as (identity, _, _):
            row = await self.row(identity, org)
            token = (
                value.credential.get_secret_value()
                if value.credential
                else row.credential_value
                if row
                else ""
            )
            revision = row.revision if row else 0
            if not token:
                raise AppError(422, "CREDENTIAL_REQUIRED", "请填写GitHub专用只读凭据。")
        try:
            result = await mcp.test(token)
        except AppError as error:
            result = GitHubTestResult(ok=False, code=error.code, message=error.message)
        async with self.access(org, write=True) as (identity, organization, _):
            current = await self.row(identity, org)
            identity.check_revision(revision, current.revision if current else 0)
            if current and value.credential is None:
                current.tested_at, current.test_result = (
                    now(),
                    result.model_dump(mode="json", by_alias=True),
                )
                current.revision += 1
                organization.access_revision += 1
                self.record(identity, org, current.id, "github.tested")
            return result

    async def save_connection(
        self, org: UUID, value: GitHubWrite, mcp: GitHubMCP
    ) -> GitHubConnectionView | GitHubSummary:
        async with self.access(org, write=True) as (identity, _, _):
            row = await self.row(identity, org)
            identity.check_revision(value.expected_revision, row.revision if row else 0)
            token = (
                value.credential.get_secret_value()
                if value.credential
                else row.credential_value
                if row
                else ""
            )
            old_token = row.credential_value if row else ""
            if not token:
                raise AppError(422, "CREDENTIAL_REQUIRED", "首次保存需填写GitHub专用只读凭据。")
        result = await mcp.test(token) if value.enabled or value.credential is not None else None
        async with self.access(org, write=True) as (identity, organization, _):
            row = await self.row(identity, org)
            identity.check_revision(value.expected_revision, row.revision if row else 0)
            if row is None:
                row = GitHubConnection(
                    id=uuid4(),
                    organization_id=org,
                    name=value.name,
                    credential_value=token,
                    enabled=value.enabled,
                    revision=1,
                )
                identity.db.add(row)
            else:
                row.name, row.enabled, row.credential_value = value.name, value.enabled, token
                row.revision += 1
            if result:
                row.tested_at, row.test_result = (
                    now(),
                    result.model_dump(mode="json", by_alias=True),
                )
            if old_token and old_token != token:
                # Replacing credentials does not silently grant old repositories to a new account.
                grants = await identity.db.scalars(
                    select(GitHubRepository).where(GitHubRepository.organization_id == org)
                )
                for grant in grants:
                    grant.enabled = False
            organization.access_revision += 1
            self.record(identity, org, row.id, "github.saved")
            await identity.db.flush()
            return self.view(row, True)

    @staticmethod
    def repo_view(row: GitHubRepository) -> RepositoryView:
        return RepositoryView(
            id=row.id,
            github_repo_id=row.github_repo_id,
            owner=row.owner,
            repo=row.repo,
            enabled=row.enabled,
            verified_at=row.verified_at,
        )

    async def repositories(self, org: UUID) -> list[RepositoryView]:
        async with self.access(org) as (identity, _, admin):
            connection = await self.row(identity, org)
            if not admin and not (
                connection and connection.enabled and connection.test_result.get("ok")
            ):
                return []
            query = select(GitHubRepository).where(GitHubRepository.organization_id == org)
            if not admin:
                query = query.where(GitHubRepository.enabled.is_(True))
            return [
                self.repo_view(row)
                for row in await identity.db.scalars(
                    query.order_by(GitHubRepository.owner, GitHubRepository.repo)
                )
            ]

    async def add_repository(
        self, org: UUID, value: RepositoryAdd, key: str, mcp: GitHubMCP
    ) -> RepositoryView:
        payload = value.model_dump(mode="json")
        async with self.access(org, write=True) as (identity, organization, _):
            replay = await identity.replay(org, "github.repository.add", key, payload)
            if replay:
                grant = await identity.db.scalar(
                    select(GitHubRepository).where(
                        GitHubRepository.organization_id == org,
                        GitHubRepository.id == replay.target_id,
                    )
                )
                if not grant or not grant.enabled:
                    raise AppError(409, "RESOURCE_REVOKED", "仓库授权已撤销，请明确重新添加。")
                return self.repo_view(grant)
            row = await self.row(identity, org)
            if not row or not row.enabled or not row.test_result.get("ok"):
                raise AppError(409, "GITHUB_UNAVAILABLE", "请先测试并启用GitHub连接。")
            access_revision = organization.access_revision
            token, revision, stamp = (
                row.credential_value,
                row.revision,
                row.test_result["schemaFingerprint"],
            )
        external, owner, repo = await mcp.repository(token, stamp, value)
        async with self.access(org, write=True) as (identity, organization, _):
            replay = await identity.replay(org, "github.repository.add", key, payload)
            if replay:
                grant = await identity.db.scalar(
                    select(GitHubRepository).where(
                        GitHubRepository.organization_id == org,
                        GitHubRepository.id == replay.target_id,
                        GitHubRepository.enabled.is_(True),
                    )
                )
                if not grant:
                    raise AppError(409, "RESOURCE_REVOKED", "仓库授权已撤销，请明确重新添加。")
                return self.repo_view(grant)
            row = await self.row(identity, org)
            identity.check_revision(revision, row.revision if row else 0)
            if access_revision != organization.access_revision:
                raise AppError(409, "ACCESS_CHANGED", "验证期间授权已改变，请重新核对后重试。")
            assert row is not None
            grant = await identity.db.scalar(
                select(GitHubRepository).where(
                    GitHubRepository.organization_id == org,
                    GitHubRepository.github_repo_id == external,
                )
            )
            if grant is None:
                grant = GitHubRepository(
                    id=uuid4(),
                    organization_id=org,
                    connection_id=row.id,
                    github_repo_id=external,
                    owner=owner,
                    repo=repo,
                    enabled=True,
                    verified_at=now(),
                )
                identity.db.add(grant)
            else:
                grant.owner, grant.repo, grant.enabled, grant.verified_at = owner, repo, True, now()
            organization.access_revision += 1
            self.record(identity, org, grant.id, "github.repository.shared")
            await identity.db.flush()
            result = self.repo_view(grant)
            identity.remember(org, "github.repository.add", key, payload, 201, result, grant.id)
            return result

    async def remove_repository(self, org: UUID, repository: UUID) -> None:
        async with self.access(org, write=True) as (identity, organization, _):
            row = await identity.db.scalar(
                select(GitHubRepository).where(
                    GitHubRepository.organization_id == org, GitHubRepository.id == repository
                )
            )
            if not row:
                raise AppError(404, "NOT_FOUND", "仓库不存在。")
            if row.enabled:
                row.enabled = False
                organization.access_revision += 1
                self.record(identity, org, row.id, "github.repository.revoked")

    async def tools(
        self, org: UUID, key: str | None = None, value: ToolWrite | None = None
    ) -> list[ToolView]:
        async with self.access(org, write=value is not None) as (identity, organization, _):
            rows = {
                row.tool_key: row
                for row in await identity.db.scalars(
                    select(OrgToolSetting).where(OrgToolSetting.organization_id == org)
                )
            }
            if value:
                if key not in {tool.key for tool in TOOLS}:
                    raise AppError(404, "NOT_FOUND", "工具不在固定注册表中。")
                if key == "knowledge.search":
                    raise AppError(409, "REQUIRED_TOOL", "知识检索为必需工具，不可关闭。")
                assert key is not None
                row = rows.get(key)
                identity.check_revision(value.expected_revision, row.revision if row else 0)
                if row:
                    row.enabled = value.enabled
                    row.revision += 1
                else:
                    row = OrgToolSetting(
                        organization_id=org, tool_key=key, enabled=value.enabled, revision=1
                    )
                    identity.db.add(row)
                    rows[key] = row
                organization.access_revision += 1
                self.record(identity, org, org, "github.tool.changed")
            return [
                ToolView(
                    key=tool.key,
                    name=tool.name,
                    description="固定只读工具；每次调用重新检查组织、Agent与资源权限。",
                    enabled=rows[tool.key].enabled if tool.key in rows else True,
                    revision=rows[tool.key].revision if tool.key in rows else 0,
                    input_summary="授权知识范围与问题"
                    if tool.key == "knowledge.search"
                    else "允许仓库、UTC时间范围、页码或已验证条目编号",
                )
                for tool in TOOLS
            ]

    async def read_tool(
        self,
        org: UUID,
        agent_id: UUID,
        repository_id: UUID,
        key: str,
        mcp: GitHubMCP,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        page: int = 1,
        number: int | None = None,
    ) -> dict[str, Any]:
        """Internal D14 integration seam, deliberately not exposed as a generic HTTP tool."""

        async def authorized(
            identity: IdentityService,
        ) -> tuple[GitHubConnection, GitHubRepository]:
            connection = await self.row(identity, org)
            grant = await identity.db.scalar(
                select(GitHubRepository).where(
                    GitHubRepository.organization_id == org,
                    GitHubRepository.id == repository_id,
                    GitHubRepository.enabled.is_(True),
                )
            )
            agent = await identity.db.scalar(
                select(Agent).where(
                    Agent.organization_id == org, Agent.id == agent_id, Agent.enabled.is_(True)
                )
            )
            if (
                not connection
                or not connection.enabled
                or not connection.test_result.get("ok")
                or not grant
                or not agent
            ):
                raise AppError(403, "GITHUB_UNAVAILABLE", "当前GitHub范围或Agent不可用。")
            binding = await identity.db.scalar(
                select(AgentVersionTool).where(
                    AgentVersionTool.organization_id == org,
                    AgentVersionTool.agent_version_id == agent.current_version_id,
                    AgentVersionTool.tool_key == key,
                )
            )
            setting = await identity.db.scalar(
                select(OrgToolSetting).where(
                    OrgToolSetting.organization_id == org, OrgToolSetting.tool_key == key
                )
            )
            if not binding or setting and not setting.enabled:
                raise AppError(403, "TOOL_UNAVAILABLE", "工具未授权或已停用。")
            return connection, grant

        async with self.access(org) as (identity, organization, _):
            connection, grant = await authorized(identity)
            token, stamp = connection.credential_value, connection.test_result["schemaFingerprint"]
            owner, repo = grant.owner, grant.repo
            revision = organization.access_revision
        result = await mcp.read(
            token, stamp, key, owner, repo, start=start, end=end, page=page, number=number
        )
        async with self.access(org) as (identity, organization, _):
            await authorized(identity)
            if revision != organization.access_revision:
                raise AppError(409, "ACCESS_CHANGED", "查询期间授权已改变，请重新发起。")
            return result
