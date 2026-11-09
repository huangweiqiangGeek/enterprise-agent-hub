"""Reuse the D06 algorithm with run-owned authorization and attempt accounting."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.model_settings import ConnectionVersion, ModelProfile
from backend.repositories.runs import RunRepository, fail
from backend.services.knowledge import KnowledgeService
from backend.services.model_probe import Attempt, ModelProbe
from backend.services.retrieval import Model, RetrievalService, Scope
from backend.services.run_state import RunState


class RunKnowledge:
    def __init__(self, state: RunState):
        self.state = state
        self.engine, self.settings = state.engine, state.settings

    @asynccontextmanager
    async def access(self, org: UUID, *, write: bool = False) -> AsyncIterator[Any]:
        if org != self.state.org:
            raise fail("TOOL_DENIED", 403)
        async with self.state.sessions.begin() as db:
            repo = RunRepository(db, org)
            run = await repo.lock(self.state.id)
            await repo.active(run, self.state.generation)
            yield SimpleNamespace(db=db), None, False


class RunRetrieval(RetrievalService):
    def __init__(self, state: RunState, probe: ModelProbe, snapshot: dict[str, Any], step: UUID):
        super().__init__(cast(KnowledgeService, RunKnowledge(state)), probe)
        self.state, self.snapshot, self.step = state, snapshot, step

    async def model(self, db: AsyncSession, org: UUID, kind: str) -> Model:
        frozen = self.snapshot[kind]
        profile = await db.get(ModelProfile, UUID(frozen["profileId"]))
        version = await db.get(ConnectionVersion, UUID(frozen["connectionVersionId"]))
        if (
            not profile
            or not version
            or profile.organization_id != org
            or version.organization_id != org
        ):
            raise fail("MODEL_NOT_CONFIGURED")
        return profile, version

    async def documents(
        self, db: AsyncSession, org: UUID, kb: UUID, selected: list[UUID]
    ) -> dict[UUID, UUID]:
        documents = await super().documents(db, org, kb, selected)
        expected = self.snapshot["documents"]
        if any(
            str(doc) not in expected or str(attempt) != expected[str(doc)]
            for doc, attempt in documents.items()
        ):
            raise fail("RETRIEVAL_SCOPE_CHANGED")
        return documents

    @asynccontextmanager
    async def call(
        self,
        org: UUID,
        kb: UUID,
        scope: Scope,
        operation: UUID,
        profile: ModelProfile,
        usage: Attempt,
    ) -> AsyncIterator[None]:
        await self.state.check()
        kind = "embedding" if profile.id == scope.embedding[0].id else "rerank"
        await self.state.meter(self.step, profile.id, kind, usage)
        started = monotonic()
        try:
            yield
            usage.status = "succeeded"
        finally:
            if usage.status != "succeeded":
                usage.status = "failed"
            await asyncio.shield(
                self.state.settle(
                    usage, int((monotonic() - started) * 1000), usage.provider_request_id
                )
            )
