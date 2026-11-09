"""HTTP run acceptance and snapshots; execution never runs inside a request transaction."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select

from backend.knowledge_schemas import AcceptanceDTO
from backend.models.agents import Agent
from backend.models.identity import IdempotencyKey, now
from backend.models.knowledge import Document, OutboxJob
from backend.models.model_settings import OrgSettings, UsageEntry
from backend.models.runs import Conversation, Message, Run, RunCitation, RunSource, RunStep
from backend.repositories.agents import AgentRepository
from backend.repositories.model_settings import ModelRepository
from backend.repositories.runs import TERMINAL, RunRepository, fail
from backend.run_schemas import (
    ConversationCreate,
    ConversationView,
    QAInput,
    QAResult,
    RunAccepted,
    RunCreate,
    RunSnapshot,
    RunStatus,
    StepSummary,
)
from backend.schemas import Page
from backend.services.agents import AgentService
from backend.services.identity import IdentityService
from backend.services.run_state import GRAPH_VERSION, RunState


class RunService(AgentService):
    async def acceptance(
        self, org: UUID, operation: str, resource_id: UUID | None, key: str
    ) -> AcceptanceDTO:
        async with self.user_access(org) as (identity, _):
            if operation == "run.create":
                conversation = (
                    await identity.db.get(Conversation, resource_id) if resource_id else None
                )
                if (
                    not conversation
                    or conversation.organization_id != org
                    or (conversation.owner_user_id != identity.require_user().id)
                ):
                    raise fail("RESOURCE_NOT_FOUND", 404)
            elif resource_id is not None:
                raise fail("RESOURCE_SCOPE_INVALID", 422)
            record = await identity.db.scalar(
                select(IdempotencyKey).where(
                    IdempotencyKey.organization_id == org,
                    IdempotencyKey.actor_id == identity.require_user().id,
                    IdempotencyKey.operation == operation,
                    IdempotencyKey.resource_id == resource_id,
                    IdempotencyKey.key == key,
                )
            )
            if not record:
                return AcceptanceDTO(status="absent")
            if operation == "run.create":
                run = await identity.db.get(Run, record.target_id)
                assert run
                return AcceptanceDTO(status="accepted", target_id=run.id, run=self.accepted(run))
            return AcceptanceDTO(
                status="accepted",
                target_id=record.target_id,
                conversation=ConversationView.model_validate(record.response_data),
            )

    @asynccontextmanager
    async def user_access(
        self, org: UUID, write: bool = False
    ) -> AsyncIterator[tuple[IdentityService, bool]]:
        async with IdentityService.open(self.engine, self.settings, self.request_id) as identity:
            await identity.authenticate(self.proof.token, self.proof.csrf_cookie)
            identity.require_user()
            if write:
                identity.check_csrf(self.proof.origin, self.proof.csrf_header)
            row, member = await identity.organization(org)
            yield identity, identity.role(row, member) in {"owner", "admin"}

    async def usable_agent(
        self, db: Any, org: UUID, agent_id: UUID
    ) -> tuple[Agent, dict[str, Any]]:
        repo = AgentRepository(db, org)
        agent = await repo.agent(agent_id)
        config = await self.config(repo, agent)
        if not agent.enabled or config.readiness:
            raise fail("AGENT_NOT_READY")
        if agent.task_type != "qa":
            raise fail("TASK_NOT_SUPPORTED", 422)
        return agent, config.model_dump(mode="json", by_alias=True)

    async def create_conversation(
        self, org: UUID, value: ConversationCreate, key: str
    ) -> ConversationView:
        async with self.user_access(org, True) as (identity, _):
            payload = value.model_dump(mode="json", by_alias=True)
            replay = await identity.replay(org, "conversation.create", key, payload)
            if replay:
                return ConversationView.model_validate(replay.response_data)
            await self.usable_agent(identity.db, org, value.agent_id)
            row = Conversation(
                organization_id=org,
                owner_user_id=identity.require_user().id,
                agent_id=value.agent_id,
                task_type=value.task_type,
                title=value.title,
            )
            identity.db.add(row)
            await identity.db.flush()
            result = ConversationView(**value.model_dump(), id=row.id, updated_at=row.updated_at)
            identity.remember(org, "conversation.create", key, payload, 201, result, row.id)
            return result

    async def accept(
        self, org: UUID, conversation_id: UUID, value: RunCreate, key: str
    ) -> RunAccepted:
        async with self.user_access(org, True) as (identity, _):
            db = identity.db
            conversation = await db.scalar(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.organization_id == org,
                    Conversation.owner_user_id == identity.require_user().id,
                )
                .with_for_update()
            )
            if not conversation:
                raise fail("RESOURCE_NOT_FOUND", 404)
            payload = {
                "conversationId": str(conversation_id),
                **value.model_dump(mode="json", by_alias=True),
            }
            replay = await identity.replay(
                org, "run.create", key, payload, resource_id=conversation_id
            )
            if replay:
                row = await db.get(Run, replay.target_id)
                assert row
                return self.accepted(row)
            if value.agent_id != conversation.agent_id:
                raise fail("AGENT_MISMATCH", 422)
            agent, config = await self.usable_agent(db, org, value.agent_id)
            if not set(map(str, value.input.knowledge_base_ids)) <= set(config["knowledgeBaseIds"]):
                raise fail("TOOL_DENIED", 403)
            if value.retry_of_run_id:
                prior = await db.get(Run, value.retry_of_run_id)
                if (
                    not prior
                    or prior.conversation_id != conversation_id
                    or prior.status not in {"failed", "cancelled", "timed_out"}
                ):
                    raise fail("RETRY_INVALID", 422)
            if await db.scalar(
                select(Run.id).where(
                    Run.conversation_id == conversation_id, Run.status.in_(["queued", "running"])
                )
            ):
                raise fail("CONVERSATION_BUSY")
            settings = await db.get(OrgSettings, org)
            limits = {
                k: min(v, settings.limits.get(k, v)) if settings else v
                for k, v in self.settings.system_limits.model_dump().items()
            }
            queued = await db.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.organization_id == org, Run.status == "queued")
            )
            mine = await db.scalar(
                select(func.count())
                .select_from(Run)
                .where(
                    Run.organization_id == org,
                    Run.initiator_id == identity.require_user().id,
                    Run.status == "queued",
                )
            )
            if (queued or 0) >= limits["max_queued_runs_per_organization"] or (mine or 0) >= 5:
                raise fail("CAPACITY_EXCEEDED", 429)
            documents = list(
                await db.scalars(
                    select(Document).where(
                        Document.organization_id == org,
                        Document.knowledge_base_id.in_(value.input.knowledge_base_ids),
                        Document.deleted_at.is_(None),
                        Document.status == "ready",
                    )
                )
            )
            if value.input.document_ids and not set(value.input.document_ids) <= {
                d.id for d in documents
            }:
                raise fail("DOCUMENT_NOT_READY")
            selected = [
                d
                for d in documents
                if not value.input.document_ids or d.id in value.input.document_ids
            ]
            if not selected:
                raise fail("DOCUMENT_NOT_READY")
            config_snapshot: dict[str, Any] = {
                "agentId": str(agent.id),
                "agentVersionId": str(agent.current_version_id),
                "versionNumber": config["versionNumber"],
                "prompt": config["prompt"],
                "graphVersion": GRAPH_VERSION,
                "toolKeys": config["toolKeys"],
                "toolSchemaVersion": "knowledge.search-v1",
                "documents": {str(d.id): str(d.current_attempt_id) for d in selected},
            }
            for kind in ("chat", "embedding", "rerank"):
                repo = ModelRepository(db, org)
                connection = await repo.connection(kind)
                assert connection
                profile, version = await repo.versions(connection)
                config_snapshot[kind] = {
                    "connectionId": str(connection.id),
                    "profileId": str(profile.id),
                    "connectionVersionId": str(version.id),
                    "modelName": profile.model_name,
                }
            row = Run(
                id=uuid4(),
                organization_id=org,
                conversation_id=conversation_id,
                initiator_id=identity.require_user().id,
                agent_version_id=agent.current_version_id,
                graph_version=GRAPH_VERSION,
                input_snapshot=value.input.model_dump(mode="json", by_alias=True),
                config_snapshot=config_snapshot,
                budget_snapshot=limits,
                retry_of_run_id=value.retry_of_run_id,
            )
            db.add(row)
            await db.flush()
            db.add(
                Message(
                    organization_id=org,
                    conversation_id=conversation_id,
                    run_id=row.id,
                    role="user",
                    content=value.input.question,
                    is_partial=False,
                )
            )
            db.add(
                OutboxJob(
                    organization_id=org,
                    job_type="run.execute",
                    resource_id=row.id,
                    resource_version=row.id,
                    payload={"organizationId": str(org), "runId": str(row.id)},
                )
            )
            conversation.updated_at = now()
            result = self.accepted(row)
            identity.remember(
                org, "run.create", key, payload, 202, result, row.id, resource_id=conversation_id
            )
            return result

    @staticmethod
    def accepted(row: Run) -> RunAccepted:
        return RunAccepted(
            run_id=row.id,
            conversation_id=row.conversation_id,
            status=cast(RunStatus, row.status),
            events_url=f"/api/v1/runs/{row.id}/events",
            created_at=row.created_at,
        )

    async def visible_run(
        self, identity: IdentityService, admin: bool, org: UUID, run_id: UUID
    ) -> Run:
        run = await RunRepository(identity.db, org).lock(run_id)
        if run.initiator_id != identity.require_user().id and not admin:
            raise fail("RESOURCE_NOT_FOUND", 404)
        return run

    async def snapshot(self, org: UUID, run_id: UUID) -> RunSnapshot:
        async with self.user_access(org) as (identity, admin):
            run = await self.visible_run(identity, admin, org, run_id)
            db = identity.db
            message = await db.scalar(
                select(Message).where(Message.run_id == run.id, Message.role == "assistant")
            )
            citations = (
                await db.execute(
                    select(RunCitation, RunSource, Document)
                    .join(RunSource, RunSource.id == RunCitation.source_id)
                    .join(Document, Document.id == RunSource.document_id)
                    .where(RunCitation.run_id == run.id)
                    .order_by(RunCitation.label)
                )
            ).all()
            missing = any(doc.deleted_at for _, _, doc in citations)
            return RunSnapshot(
                id=run.id,
                conversation_id=run.conversation_id,
                status=cast(RunStatus, run.status),
                cancel_requested=run.cancel_requested,
                input_summary=run.input_snapshot["question"],
                input=QAInput.model_validate(run.input_snapshot),
                deadline_at=run.deadline_at,
                config_version={
                    k: v for k, v in run.config_snapshot.items() if k not in {"prompt", "documents"}
                },
                budget_snapshot=run.budget_snapshot,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                last_seq=run.last_seq,
                partial_message=message.content if message and message.is_partial else None,
                result=QAResult.model_validate(message.result_json)
                if message and message.result_json
                else None,
                citations=[
                    {
                        "sourceId": str(s.id),
                        "label": c.label,
                        "claimPath": c.claim_path,
                        "documentId": str(s.document_id),
                        "attemptId": str(s.attempt_id),
                        "chunkId": str(s.chunk_id),
                        "title": s.title,
                        "locator": s.locator,
                        "availability": "deleted" if d.deleted_at else "available",
                    }
                    for c, s, d in citations
                ],
                coverage=run.coverage,
                result_visibility="partially_unavailable" if missing else "visible",
                error_code=run.error_code,
                retry_of_run_id=run.retry_of_run_id,
            )

    async def steps(self, org: UUID, run_id: UUID, cursor: int, limit: int) -> Page[StepSummary]:
        async with self.user_access(org) as (identity, admin):
            run = await self.visible_run(identity, admin, org, run_id)
            rows = list(
                await identity.db.scalars(
                    select(RunStep)
                    .where(RunStep.run_id == run.id, RunStep.sequence > cursor)
                    .order_by(RunStep.sequence)
                    .limit(limit + 1)
                )
            )
            items = []
            for row in rows[:limit]:
                usage = list(
                    await identity.db.scalars(
                        select(UsageEntry).where(UsageEntry.step_id == row.id)
                    )
                )
                items.append(
                    StepSummary(
                        id=row.id,
                        logical_step_key=row.logical_step_key,
                        attempt=row.attempt_no,
                        kind=row.kind,
                        name=row.name,
                        status=row.status,
                        started_at=row.started_at,
                        finished_at=row.finished_at,
                        duration_ms=int((row.finished_at - row.started_at).total_seconds() * 1000)
                        if row.finished_at
                        else None,
                        public_input_summary=row.input_summary,
                        public_output_summary={
                            k: v for k, v in row.output_summary.items() if not k.startswith("_")
                        },
                        source_ids=row.output_summary.get("sourceIds", []),
                        usage=[
                            {
                                "kind": u.operation_kind,
                                "inputTokens": u.input_tokens,
                                "outputTokens": u.output_tokens,
                                "reservedTokens": u.reserved_tokens,
                                "tokenSource": u.token_source,
                                "status": u.status,
                                "providerRequestId": u.provider_request_id,
                            }
                            for u in usage
                        ],
                        error_code=row.error_code,
                    )
                )
            return Page(
                items=items,
                next_cursor=str(rows[limit - 1].sequence) if len(rows) > limit else None,
            )

    async def cancel(self, org: UUID, run_id: UUID) -> tuple[int, RunSnapshot]:
        async with self.user_access(org, True) as (identity, _):
            repo = RunRepository(identity.db, org)
            run = await repo.lock(run_id)
            if run.initiator_id != identity.require_user().id:
                raise fail("RESOURCE_NOT_FOUND", 404)
            status = 200 if run.status in TERMINAL else 202
            if run.status not in TERMINAL:
                run.cancel_requested = True
                if run.status == "queued":
                    await RunState.close(identity.db, repo, run, error="RUN_CANCELLED")
        return status, await self.snapshot(org, run_id)
