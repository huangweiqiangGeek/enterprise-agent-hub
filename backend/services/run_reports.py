"""M6 uses the same scope/date predicate for list and statistics."""

import hashlib
from typing import cast
from uuid import UUID

from sqlalchemy import func, select, tuple_

from backend.models.agents import AgentVersion
from backend.models.identity import Organization, User, now
from backend.models.runs import Conversation, Run
from backend.report_schemas import RunAgentOption, RunFilters, RunReport, RunSummary, UsageSummary
from backend.repositories.run_reports import duration_stats, durations, run_query, usage_totals
from backend.repositories.runs import TERMINAL, fail
from backend.run_schemas import RunStatus
from backend.schemas import Page
from backend.services.dialogue import DialogueService
from backend.services.identity import PageCursor


class RunReportService(DialogueService):
    async def report_agents(self, org: UUID, filters: RunFilters) -> Page[RunAgentOption]:
        async with self.user_access(org) as (identity, admin):
            if filters.scope == "organization" and not admin:
                raise fail("FORBIDDEN", 403)
            runs = run_query(org, identity.require_user().id, filters).subquery()
            rows = (
                await identity.db.execute(
                    select(AgentVersion.agent_id, AgentVersion.name)
                    .join(runs, runs.c.agent_version_id == AgentVersion.id)
                    .distinct(AgentVersion.agent_id)
                    .order_by(AgentVersion.agent_id, AgentVersion.version_no.desc())
                )
            ).all()
            return Page(
                items=[RunAgentOption(id=row[0], name=row[1]) for row in rows], next_cursor=None
            )

    async def run_list(
        self, org: UUID, filters: RunFilters, cursor: str | None, limit: int
    ) -> Page[RunSummary]:
        async with self.user_access(org) as (identity, admin):
            if filters.scope == "organization" and not admin:
                raise fail("FORBIDDEN", 403)
            organization = await identity.db.get(Organization, org)
            assert organization
            digest = hashlib.sha256(filters.model_dump_json().encode()).hexdigest()
            query = run_query(org, identity.require_user().id, filters)
            if cursor:
                point = identity.decode_cursor(cursor, org, organization.access_revision)
                if point.filter_digest != digest:
                    raise fail("CURSOR_INVALID", 422)
                query = query.where(tuple_(Run.created_at, Run.id) < (point.created_at, point.id))
            rows = list(
                await identity.db.scalars(
                    query.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)
                )
            )
            items = []
            for run in rows[:limit]:
                version = await identity.db.get(AgentVersion, run.agent_version_id)
                actor = await identity.db.get(User, run.initiator_id)
                conversation = await identity.db.get(Conversation, run.conversation_id)
                assert version and actor and conversation
                queue, execution = durations(run)
                totals = await usage_totals(identity.db, org, [run.id])
                items.append(
                    RunSummary(
                        id=run.id,
                        conversation_id=run.conversation_id,
                        agent_id=version.agent_id,
                        agent_name=version.name,
                        version_number=version.version_no,
                        task_type=conversation.task_type,
                        initiator_id=actor.id,
                        initiator_name=actor.display_name,
                        status=cast(RunStatus, run.status),
                        created_at=run.created_at,
                        queue_duration_ms=queue,
                        execution_duration_ms=execution,
                        chat=totals["chat"],
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
                        filter_digest=digest,
                    )
                )
            return Page(items=items, next_cursor=next_cursor)

    async def usage_summary(self, org: UUID, filters: RunFilters) -> UsageSummary:
        async with self.user_access(org) as (identity, admin):
            if filters.scope == "organization" and not admin:
                raise fail("FORBIDDEN", 403)
            query = run_query(org, identity.require_user().id, filters)
            rows = query.subquery()
            counts = {
                s: 0 for s in ("queued", "running", "succeeded", "failed", "cancelled", "timed_out")
            }
            for state, count in (
                await identity.db.execute(
                    select(rows.c.status, func.count()).group_by(rows.c.status)
                )
            ).all():
                counts[state] = count
            totals = await usage_totals(identity.db, org, select(rows.c.id))
            queue, execution = await duration_stats(identity.db, query)
            terminal = sum(counts[s] for s in TERMINAL)
            start, end = filters.bounds()
            return UsageSummary(
                run_count=sum(counts.values()),
                status_counts=counts,
                terminal_count=terminal,
                success_rate=counts["succeeded"] / terminal if terminal else None,
                chat=totals["chat"],
                by_kind=totals,
                queue_duration=queue,
                execution_duration=execution,
                range_utc={"start": start, "endExclusive": end},
                timezone=filters.timezone,
                scope=filters.scope,
                observed_at=now(),
            )

    async def report(self, org: UUID, run_id: UUID) -> RunReport:
        async with self.user_access(org) as (identity, admin):
            run = await self.visible_run(identity, admin, org, run_id)
            version = await identity.db.get(AgentVersion, run.agent_version_id)
            actor = await identity.db.get(User, run.initiator_id)
            assert version and actor
            queue, execution = durations(run)
            retries = list(
                await identity.db.scalars(
                    select(Run.id)
                    .where(
                        Run.organization_id == org,
                        Run.retry_of_run_id == run.id,
                    )
                    .order_by(Run.created_at, Run.id)
                )
            )
            return RunReport(
                agent_name=version.name,
                version_number=version.version_no,
                initiator_name=actor.display_name,
                is_owner=run.initiator_id == identity.require_user().id,
                queue_duration_ms=queue,
                execution_duration_ms=execution,
                by_kind=await usage_totals(identity.db, org, [run.id]),
                retry_ids=retries,
            )
