from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.config import Settings
from backend.model_schemas import (
    Limits,
    LimitsView,
    LimitsWrite,
    ModelConfig,
    ModelConfigWrite,
    ModelKind,
    ModelOption,
    ModelSettingsView,
    ModelSummary,
    SavedModelTest,
    TestResult,
)
from backend.models.identity import AuditEntry, Organization
from backend.models.model_settings import (
    ConnectionVersion,
    ModelConnection,
    ModelProfile,
    OrgSettings,
    UsageEntry,
)
from backend.repositories.model_settings import ModelRepository
from backend.services.errors import AppError
from backend.services.identity import IdentityService
from backend.services.model_probe import PROTOCOLS, Attempt, ModelProbe, ProbeOutcome


@dataclass(frozen=True)
class SessionProof:
    token: str
    csrf_cookie: str
    origin: str | None
    csrf_header: str | None


class ModelSettingsService:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        request_id: str,
        proof: SessionProof,
        probe: ModelProbe,
    ) -> None:
        self.engine, self.settings, self.request_id = engine, settings, request_id
        self.proof, self.probe = proof, probe

    @asynccontextmanager
    async def access(
        self, org_id: UUID, *, write: bool = False
    ) -> AsyncIterator[tuple[IdentityService, Organization, bool]]:
        # Do not keep identity/organization locks while awaiting external model requests.
        async with IdentityService.open(self.engine, self.settings, self.request_id) as identity:
            await identity.authenticate(self.proof.token, self.proof.csrf_cookie)
            if write:
                identity.check_csrf(self.proof.origin, self.proof.csrf_header)
            org, member = await identity.organization(org_id)
            if write:
                identity.require_role(org, member, {"owner", "admin"})
            yield identity, org, identity.role(org, member) in {"owner", "admin"}

    async def projection(self, repo: ModelRepository, row: ModelConnection) -> ModelConfig:
        profile, _ = await repo.versions(row)
        return ModelConfig.model_validate(
            {
                "id": row.id,
                "kind": row.kind,
                "name": row.name,
                "providerProtocol": profile.provider_protocol,
                "baseUrl": profile.base_url,
                "modelName": profile.model_name,
                "dimension": profile.dimension,
                "enabled": row.enabled,
                "hasCredential": True,
                "lastTestedAt": row.tested_at,
                "testStatus": "passed",
                "revision": row.revision,
                "profileId": profile.id,
                "tokenizerId": profile.tokenizer_id,
                "capabilities": profile.capabilities,
                "embeddingLocked": row.kind == "embedding" and await repo.embedding_locked(),
            }
        )

    async def list_models(self, org_id: UUID) -> ModelSettingsView:
        async with self.access(org_id) as (identity, _, admin):
            repo = ModelRepository(identity.db, org_id)
            rows = (
                await identity.db.scalars(
                    select(ModelConnection)
                    .where(
                        ModelConnection.organization_id == org_id,
                    )
                    .order_by(ModelConnection.kind)
                )
            ).all()
            items: list[ModelConfig | ModelSummary] = []
            for row in rows:
                if admin:
                    items.append(await self.projection(repo, row))
                elif row.enabled:
                    items.append(
                        ModelSummary.model_validate(
                            {"id": row.id, "kind": row.kind, "name": row.name}
                        )
                    )
            options = (
                [
                    ModelOption.model_validate(
                        {
                            "kind": kind,
                            "providerProtocol": protocol,
                            "baseUrls": self.settings.model_base_urls.get(protocol, []),
                            "supportedChatModels": self.probe.chat_models(protocol)
                            if kind == "chat"
                            else [],
                        }
                    )
                    for kind, protocols in PROTOCOLS.items()
                    for protocol in protocols
                    if self.settings.model_base_urls.get(protocol)
                ]
                if admin
                else []
            )
            return ModelSettingsView(items=items, options=options)

    async def limits(self, org_id: UUID, value: LimitsWrite | None = None) -> LimitsView:
        async with self.access(org_id, write=value is not None) as (identity, org, _):
            row = await identity.db.get(OrgSettings, org_id)
            hard = self.settings.system_limits.model_dump()
            if value is not None:
                identity.check_revision(value.expected_revision, row.revision if row else 0)
                proposed = value.limits.model_dump()
                if any(proposed[key] > maximum for key, maximum in hard.items()):
                    raise AppError(422, "LIMIT_INVALID", "组织上限不能超过系统硬上限。")
                if row is None:
                    row = OrgSettings(organization_id=org_id, limits=proposed, revision=1)
                    identity.db.add(row)
                else:
                    row.limits = proposed
                    row.revision += 1
                org.settings_revision += 1
                self.audit(identity, org_id, "limits.updated", org_id, {"revision": row.revision})
            effective = {
                key: min(row.limits.get(key, maximum), maximum) if row else maximum
                for key, maximum in hard.items()
            }
            return LimitsView(
                system=self.settings.system_limits,
                effective=Limits(**effective),
                editable=list(self.settings.system_limits.model_dump(by_alias=True)),
                revision=row.revision if row else 0,
            )

    def audit(
        self,
        identity: IdentityService,
        org_id: UUID,
        action: str,
        target: UUID,
        summary: dict[str, object],
    ) -> None:
        identity.db.add(
            AuditEntry(
                organization_id=org_id,
                actor_id=identity.require_user().id,
                action=action,
                target_type="model" if action.startswith("model") else "settings",
                target_id=target,
                before_summary={},
                after_summary=summary,
                request_id=self.request_id,
            )
        )

    async def candidate(
        self, repo: ModelRepository, kind: ModelKind, value: ModelConfigWrite | SavedModelTest
    ) -> tuple[ModelConfigWrite, UUID | None]:
        row = await repo.connection(kind)
        profile, version = await repo.versions(row) if row else (None, None)
        if isinstance(value, SavedModelTest):
            if not row or row.id != value.connection_id or not profile or not version:
                raise AppError(404, "NOT_FOUND", "模型连接不存在或不可见。")
            return ModelConfigWrite.model_validate(
                {
                    "providerProtocol": profile.provider_protocol,
                    "baseUrl": profile.base_url,
                    "modelName": profile.model_name,
                    "credential": version.credential_value,
                    "embeddingDimension": profile.dimension,
                    "expectedRevision": row.revision,
                }
            ), profile.id
        IdentityService.check_revision(value.expected_revision, row.revision if row else 0)
        self.probe.validate(kind, value)
        if value.credential is None:
            if (
                not version
                or value.base_url != version.base_url
                or value.provider_protocol != version.provider_protocol
            ):
                raise AppError(
                    422, "MODEL_CREDENTIAL_REQUIRED", "首次配置或更换地址时请重新填写凭据。"
                )
            value = value.model_copy(update={"credential": SecretStr(version.credential_value)})
        if kind == "embedding" and profile and await repo.embedding_locked():
            if (profile.provider_protocol, profile.base_url, profile.model_name) != (
                value.provider_protocol,
                value.base_url,
                value.model_name,
            ) or value.embedding_dimension not in {None, profile.dimension}:
                raise AppError(
                    409,
                    "EMBEDDING_LOCKED",
                    "组织已有文档或处理中任务；更换 Embedding 语义需要维护重建。",
                )
            # Omitting dimensions when rotating credentials must not silently switch to the default.
            value = value.model_copy(update={"embedding_dimension": profile.dimension})
        return value, None

    async def test_model(
        self,
        org_id: UUID,
        kind: ModelKind,
        value: ModelConfigWrite | SavedModelTest,
        *,
        save: bool = False,
    ) -> TestResult | ModelConfig:
        async with self.access(org_id, write=True) as (identity, _, _):
            candidate, tested_profile = await self.candidate(
                ModelRepository(identity.db, org_id), kind, value
            )
        operation = uuid4()

        async def record(attempt: Attempt) -> None:
            if attempt.status == "pending":
                async with self.access(org_id, write=True) as (identity, _, _):
                    current = await ModelRepository(identity.db, org_id).connection(kind)
                    identity.check_revision(
                        candidate.expected_revision, current.revision if current else 0
                    )
                    identity.db.add(
                        UsageEntry(
                            id=attempt.id,
                            organization_id=org_id,
                            operation_kind=f"model_test_{kind}",
                            operation_id=operation,
                            attempt_no=attempt.number,
                            model_profile_id=tested_profile,
                            reserved_tokens=attempt.reserved,
                            token_source=attempt.token_source,
                            status="pending",
                            billed_units={},
                        )
                    )
            else:
                # Metering survives logout/revocation and never authorizes a configuration write.
                async with async_sessionmaker(self.engine, expire_on_commit=False).begin() as db:
                    usage = await db.get(UsageEntry, attempt.id)
                    if usage:
                        usage.input_tokens, usage.output_tokens = (
                            attempt.input_tokens,
                            attempt.output_tokens,
                        )
                        usage.token_source, usage.billed_units = (
                            attempt.token_source,
                            attempt.billed_units,
                        )
                        usage.duration_ms, usage.status = attempt.duration_ms, attempt.status

        outcome = await self.probe.test(kind, candidate, record)
        async with self.access(org_id, write=True) as (identity, org, _):
            repo = ModelRepository(identity.db, org_id)
            current = await repo.connection(kind)
            identity.check_revision(candidate.expected_revision, current.revision if current else 0)
            if not save:
                self.audit(
                    identity,
                    org_id,
                    "model.tested",
                    current.id if current else operation,
                    {"kind": kind, "ok": outcome.result.ok, "errorCode": outcome.result.error_code},
                )
                return outcome.result
            if not outcome.result.ok:
                # Raising inside a transaction would discard the failure audit, so usage is
                # already persisted independently and the active connection remains untouched.
                raise AppError(
                    422, outcome.result.error_code or "MODEL_TEST_FAILED", outcome.result.message
                )
            if kind == "embedding" and current and await repo.embedding_locked():
                old, _ = await repo.versions(current)
                if (old.provider_protocol, old.base_url, old.model_name, old.dimension) != (
                    candidate.provider_protocol,
                    candidate.base_url,
                    candidate.model_name,
                    outcome.dimension,
                ):
                    raise AppError(
                        409,
                        "EMBEDDING_LOCKED",
                        "组织已有文档或处理中任务；不能更换 Embedding 语义。",
                    )
            row = await self.publish(identity, repo, kind, candidate, outcome, current)
            org.settings_revision += 1
            self.audit(
                identity,
                org_id,
                "model.saved",
                row.id,
                {"kind": kind, "revision": row.revision, "profileId": str(row.active_profile_id)},
            )
            await identity.db.flush()
            return await self.projection(repo, row)

    async def publish(
        self,
        identity: IdentityService,
        repo: ModelRepository,
        kind: ModelKind,
        value: ModelConfigWrite,
        outcome: ProbeOutcome,
        current: ModelConnection | None,
    ) -> ModelConnection:
        row_id = current.id if current else uuid4()
        profile, previous = await repo.versions(current) if current else (None, None)
        semantic = (
            value.provider_protocol,
            value.base_url,
            value.model_name,
            outcome.dimension,
            outcome.tokenizer,
        )
        if not profile or semantic != (
            profile.provider_protocol,
            profile.base_url,
            profile.model_name,
            profile.dimension,
            profile.tokenizer_id,
        ):
            profile = ModelProfile(
                id=uuid4(),
                organization_id=repo.org_id,
                connection_id=row_id,
                provider_protocol=value.provider_protocol,
                base_url=value.base_url,
                model_name=value.model_name,
                dimension=outcome.dimension,
                tokenizer_id=outcome.tokenizer,
                capabilities=outcome.capabilities,
                parameters=(
                    {"maxOutputTokens": 2000, "enableThinking": False}
                    if value.provider_protocol == "dashscope-chat"
                    else {"maxOutputTokens": 2000}
                    if kind == "chat"
                    else {}
                ),
                version=profile.version + 1 if profile else 1,
            )
            identity.db.add(profile)
        version = ConnectionVersion(
            id=uuid4(),
            organization_id=repo.org_id,
            connection_id=row_id,
            version_no=previous.version_no + 1 if previous else 1,
            provider_protocol=value.provider_protocol,
            base_url=value.base_url,
            credential_value=value.credential.get_secret_value() if value.credential else "",
        )
        identity.db.add(version)
        result = outcome.result.model_dump(mode="json", by_alias=True)
        if current:
            current.active_profile_id, current.current_connection_version_id = (
                profile.id,
                version.id,
            )
            current.name, current.tested_at, current.test_result = (
                value.model_name,
                outcome.result.tested_at,
                result,
            )
            current.revision += 1
            return current
        row = ModelConnection(
            id=row_id,
            organization_id=repo.org_id,
            kind=kind,
            name=value.model_name,
            active_profile_id=profile.id,
            current_connection_version_id=version.id,
            tested_at=outcome.result.tested_at,
            test_result=result,
            enabled=True,
            revision=1,
        )
        identity.db.add(row)
        return row
