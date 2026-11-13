"""Shared SQL projection; aggregate usage rows, never replayed events."""

from typing import Any
from uuid import UUID

from sqlalchemy import Integer, Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.model_settings import UsageEntry
from backend.models.runs import Conversation, Run
from backend.report_schemas import DurationStats, RunFilters, UsageTotals


def run_query(org: UUID, actor: UUID, filters: RunFilters) -> Select[tuple[Run]]:
    query = (
        select(Run)
        .join(Conversation, Conversation.id == Run.conversation_id)
        .where(Run.organization_id == org, Conversation.organization_id == org)
    )
    if filters.scope == "mine":
        query = query.where(Run.initiator_id == actor)
    if filters.status:
        query = query.where(Run.status == filters.status)
    if filters.agent_id:
        query = query.where(Conversation.agent_id == filters.agent_id)
    start, end = filters.bounds()
    if start:
        query = query.where(Run.created_at >= start, Run.created_at < end)
    return query


async def usage_totals(db: AsyncSession, org: UUID, ids: Any) -> dict[str, UsageTotals]:
    actual = (
        (UsageEntry.token_source == "actual")
        & UsageEntry.input_tokens.is_not(None)
        & ((UsageEntry.operation_kind != "run.chat") | UsageEntry.output_tokens.is_not(None))
    )
    estimated = UsageEntry.token_source.in_(["reserved", "estimated"])

    def count(condition: Any) -> Any:
        return func.sum(case((condition, 1), else_=0))

    unit_keys = ("total_tokens", "search_units", "input_tokens")
    unit_columns = []
    for key in unit_keys:
        value = UsageEntry.billed_units[key].astext.cast(Integer)
        unit_columns.extend([func.sum(value), func.count(value)])

    rows = (
        await db.execute(
            select(
                UsageEntry.operation_kind,
                func.count(),
                count(actual),
                func.sum(case((actual, UsageEntry.input_tokens), else_=0)),
                func.sum(case((actual, func.coalesce(UsageEntry.output_tokens, 0)), else_=0)),
                func.sum(case((estimated, UsageEntry.reserved_tokens), else_=0)),
                count(estimated),
                count(~actual & ~estimated),
                count(UsageEntry.status == "outcome_unknown"),
                count(UsageEntry.status == "pending"),
                count(UsageEntry.status == "failed"),
                *unit_columns,
            )
            .where(UsageEntry.organization_id == org, UsageEntry.run_id.in_(ids))
            .group_by(UsageEntry.operation_kind)
        )
    ).all()
    result = {kind: UsageTotals() for kind in ("chat", "embedding", "rerank")}
    for row in rows:
        kind = row[0].removeprefix("run.")
        if kind in result:
            result[kind] = UsageTotals.model_validate(
                dict(
                    zip(
                        [f for f in UsageTotals.model_fields if f != "billed_units"],
                        (int(v or 0) for v in row[1:11]),
                        strict=True,
                    )
                )
            )
            result[kind].billed_units = {
                key: int(row[11 + index * 2])
                for index, key in enumerate(unit_keys)
                if row[12 + index * 2]
            }
    return result


def durations(run: Run) -> tuple[int | None, int | None]:
    queue_end = run.started_at or run.finished_at
    return (
        max(0, int((queue_end - run.created_at).total_seconds() * 1000)) if queue_end else None,
        max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))
        if run.started_at and run.finished_at
        else None,
    )


async def duration_stats(
    db: AsyncSession, query: Select[tuple[Run]]
) -> tuple[DurationStats, DurationStats]:
    rows = query.subquery()
    values = [
        func.extract(
            "epoch", func.coalesce(rows.c.started_at, rows.c.finished_at) - rows.c.created_at
        )
        * 1000,
        func.extract("epoch", rows.c.finished_at - rows.c.started_at) * 1000,
    ]
    result = []
    for value in values:
        row = (
            await db.execute(
                select(
                    func.count(value),
                    func.avg(value),
                    func.percentile_cont(0.5).within_group(value),
                    func.percentile_cont(0.95).within_group(value),
                )
            )
        ).one()
        result.append(DurationStats(count=row[0], avg=row[1], p50=row[2], p95=row[3]))
    return result[0], result[1]
