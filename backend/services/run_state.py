"""Short transactions for worker ownership, attempts, metering and terminal publication."""

import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.config import Settings
from backend.models.identity import now
from backend.models.model_settings import UsageEntry
from backend.models.runs import Message, Run, RunCitation, RunStep
from backend.repositories.runs import TERMINAL, RunRepository, fail
from backend.run_schemas import QAResult
from backend.services.errors import AppError
from backend.services.model_probe import Attempt


class RunState:
    def __init__(self, engine: AsyncEngine, settings: Settings, org: UUID, run_id: UUID):
        self.engine, self.settings, self.org, self.id = engine, settings, org, run_id
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.generation = 0

    async def claim(self) -> Run | None:
        async with self.sessions.begin() as db:
            repo = RunRepository(db, self.org)
            run = await repo.lock(self.id)
            if run.status != "queued":
                return None
            active = await db.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.organization_id == self.org, Run.status == "running")
            )
            if (active or 0) >= run.budget_snapshot["max_active_runs_per_organization"]:
                return None
            await repo.authorize(run)
            run.status = "running"
            run.started_at = now()
            run.deadline_at = run.started_at + timedelta(
                seconds=run.budget_snapshot["run_deadline_seconds"]
            )
            run.lease_generation += 1
            self.generation = run.lease_generation
            run.lease_until = now() + timedelta(seconds=30)
            run.lease_owner = str(uuid4())
            repo.event(
                run,
                "run.started",
                {
                    "startedAt": run.started_at.isoformat(),
                    "deadlineAt": run.deadline_at.isoformat(),
                    "taskType": "qa",
                    "configVersion": str(run.agent_version_id),
                },
            )
            db.add(
                Message(
                    organization_id=self.org,
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    role="assistant",
                    content="",
                    is_partial=True,
                )
            )
            return run

    async def check(self, heartbeat: bool = False) -> Run:
        async with self.sessions.begin() as db:
            repo = RunRepository(db, self.org)
            run = await repo.lock(self.id)
            await repo.active(run, self.generation)
            if heartbeat:
                run.lease_until = now() + timedelta(seconds=30)
            return run

    async def reserve(
        self,
        key: str,
        kind: str,
        name: str,
        summary: dict[str, Any],
        usage: Attempt | None = None,
        profile: UUID | None = None,
        operation: str = "run.chat",
    ) -> UUID:
        async with self.sessions.begin() as db:
            repo = RunRepository(db, self.org)
            run = await repo.lock(self.id)
            await repo.active(run, self.generation)
            rows = list(await db.scalars(select(RunStep).where(RunStep.run_id == run.id)))
            count = sum(s.kind == kind for s in rows)
            maximum = run.budget_snapshot[
                "max_run_steps" if kind == "model" else "max_tool_attempts"
            ]
            if kind in {"model", "tool"} and count >= maximum:
                raise fail("BUDGET_EXCEEDED")
            if usage and operation == "run.chat":
                entries = list(
                    await db.scalars(
                        select(UsageEntry).where(
                            UsageEntry.run_id == run.id, UsageEntry.operation_kind == "run.chat"
                        )
                    )
                )
                spent = sum(
                    (e.input_tokens or 0) + (e.output_tokens or 0)
                    if e.token_source == "actual"
                    else e.reserved_tokens
                    for e in entries
                )
                if spent + usage.reserved > run.budget_snapshot["max_run_tokens"]:
                    raise fail("BUDGET_EXCEEDED")
            step = RunStep(
                id=uuid4(),
                organization_id=self.org,
                run_id=run.id,
                sequence=len(rows) + 1,
                logical_step_key=key,
                attempt_no=1 + sum(s.logical_step_key == key for s in rows),
                kind=kind,
                name=name,
                input_digest=summary.get("inputDigest")
                or hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest(),
                input_summary=summary,
                lease_generation=self.generation,
            )
            db.add(step)
            await db.flush()
            if usage:
                db.add(
                    UsageEntry(
                        id=usage.id,
                        organization_id=self.org,
                        run_id=run.id,
                        step_id=step.id,
                        operation_kind=operation,
                        operation_id=step.id,
                        attempt_no=usage.number,
                        model_profile_id=profile,
                        reserved_tokens=usage.reserved,
                        token_source=usage.token_source,
                        status="pending",
                    )
                )
            if kind == "tool":
                repo.event(
                    run,
                    "tool.started",
                    {
                        "stepId": str(step.id),
                        "toolKey": name,
                        "attempt": step.attempt_no,
                        "publicInputSummary": summary,
                    },
                )
            return step.id

    async def meter(self, step_id: UUID, profile: UUID, kind: str, usage: Attempt) -> None:
        async with self.sessions.begin() as db:
            repo = RunRepository(db, self.org)
            run = await repo.lock(self.id)
            await repo.active(run, self.generation)
            db.add(
                UsageEntry(
                    id=usage.id,
                    organization_id=self.org,
                    run_id=run.id,
                    step_id=step_id,
                    operation_kind=f"run.{kind}",
                    operation_id=step_id,
                    attempt_no=usage.number,
                    model_profile_id=profile,
                    reserved_tokens=usage.reserved,
                    token_source=usage.token_source,
                    status="pending",
                )
            )

    async def settle(self, usage: Attempt, duration: int, request_id: str | None = None) -> None:
        # Late accounting is allowed; late content/terminal writes are fenced separately.
        async with self.sessions.begin() as db:
            row = await db.get(UsageEntry, usage.id, with_for_update=True)
            assert row
            row.status = usage.status
            row.input_tokens, row.output_tokens = usage.input_tokens, usage.output_tokens
            row.token_source, row.billed_units = usage.token_source, usage.billed_units
            row.duration_ms, row.provider_request_id = duration, request_id

    async def finish_step(
        self, step_id: UUID, output: dict[str, Any], error: str | None = None
    ) -> None:
        async with self.sessions.begin() as db:
            repo = RunRepository(db, self.org)
            run = await repo.lock(self.id)
            if run.status != "running" or run.lease_generation != self.generation:
                return
            step = await db.get(RunStep, step_id)
            assert step and step.run_id == run.id
            step.status, step.error_code, step.finished_at = (
                ("failed" if error else "succeeded"),
                error,
                now(),
            )
            step.output_summary = output
            if step.kind == "tool":
                repo.event(
                    run,
                    "tool.failed" if error else "tool.completed",
                    {
                        "stepId": str(step.id),
                        "publicOutputSummary": output,
                        "sourceIds": output.get("sourceIds", []),
                        "coverage": output.get("coverage"),
                        "durationMs": int(
                            (step.finished_at - step.started_at).total_seconds() * 1000
                        ),
                        "errorCode": error,
                        "message": "工具调用失败，请查看执行记录。" if error else None,
                        "willRetry": False,
                    },
                )

    async def delta(self, text: str, reset: bool = False) -> None:
        async with self.sessions.begin() as db:
            repo = RunRepository(db, self.org)
            run = await repo.lock(self.id)
            await repo.active(run, self.generation)
            if not await repo.sources_valid(run):
                raise fail("REQUIRED_SOURCE_UNAVAILABLE")
            message = await db.scalar(
                select(Message).where(Message.run_id == run.id, Message.role == "assistant")
            )
            assert message
            message.content = text if reset else message.content + text
            # Revisions replace a draft; never append repaired answers to failed drafts.
            repo.event(
                run,
                "message.delta",
                {"messageId": str(message.id), "delta": text, "isPartial": True, "reset": reset},
            )

    async def terminal(
        self,
        result: QAResult | None = None,
        error: str | None = None,
        *,
        expired_only: bool = False,
    ) -> bool:
        async with self.sessions.begin() as db:
            repo = RunRepository(db, self.org)
            run = await repo.lock(self.id)
            if run.status in TERMINAL or run.lease_generation != self.generation:
                return False
            if expired_only and not (
                (run.status == "running" and run.lease_until and run.lease_until <= now())
                or (run.status == "queued" and run.created_at <= now() - timedelta(seconds=300))
            ):
                return False
            if result:
                try:
                    await repo.active(run, self.generation)
                    if not await repo.sources_valid(run):
                        raise fail("REQUIRED_SOURCE_UNAVAILABLE")
                except AppError as exc:
                    error, result = exc.code, None
            if run.cancel_requested:
                error, result = "RUN_CANCELLED", None
            elif run.deadline_at and run.deadline_at <= now():
                error, result = "RUN_DEADLINE_EXCEEDED", None
            run.status = (
                "succeeded"
                if result
                else "cancelled"
                if error == "RUN_CANCELLED"
                else "timed_out"
                if error == "RUN_DEADLINE_EXCEEDED"
                else "failed"
            )
            run.error_code, run.finished_at, run.lease_until = error, now(), None
            message = await db.scalar(
                select(Message).where(Message.run_id == run.id, Message.role == "assistant")
            )
            if result:
                assert message
                message.content, message.result_json, message.is_partial = (
                    result.answer,
                    result.model_dump(mode="json", by_alias=True),
                    False,
                )
                run.coverage = result.coverage
                label = 0
                for group in ("claims", "conflicts"):
                    for index, claim in enumerate(getattr(result, group)):
                        for source in claim.source_ids:
                            label += 1
                            db.add(
                                RunCitation(
                                    organization_id=self.org,
                                    run_id=run.id,
                                    message_id=message.id,
                                    source_id=source,
                                    label=label,
                                    claim_path=f"{group}.{index}",
                                )
                            )
            elif message and error in {
                "PERMISSION_REVOKED",
                "REQUIRED_SOURCE_UNAVAILABLE",
                "CITATION_INVALID",
            }:
                message.content = ""
            pending = list(
                await db.scalars(
                    select(UsageEntry).where(
                        UsageEntry.run_id == run.id, UsageEntry.status == "pending"
                    )
                )
            )
            for usage in pending:
                usage.status = "outcome_unknown"
            for step in await db.scalars(
                select(RunStep).where(RunStep.run_id == run.id, RunStep.status == "running")
            ):
                step.status, step.error_code, step.finished_at = "failed", error, now()
            entries = list(await db.scalars(select(UsageEntry).where(UsageEntry.run_id == run.id)))
            usage_summary = [
                {
                    "kind": u.operation_kind,
                    "status": u.status,
                    "inputTokens": u.input_tokens,
                    "outputTokens": u.output_tokens,
                    "tokenSource": u.token_source,
                    "reservedTokens": u.reserved_tokens,
                    "billedUnits": u.billed_units,
                }
                for u in entries
            ]
            repo.event(
                run,
                "run.completed" if result else f"run.{run.status}",
                {
                    "finishedAt": run.finished_at.isoformat(),
                    "messageId": str(message.id) if message else None,
                    "resultRevision": 1,
                    "errorCode": error,
                    "message": "任务已结束，请查看执行记录。" if error else None,
                    "partialMessageId": str(message.id) if message and not result else None,
                    "usageSummary": usage_summary,
                },
            )
            return True
