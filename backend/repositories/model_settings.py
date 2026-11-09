from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.model_settings import ConnectionVersion, ModelConnection, ModelProfile


class ModelRepository:
    def __init__(self, db: AsyncSession, org_id: UUID) -> None:
        self.db, self.org_id = db, org_id

    async def connection(self, kind: str) -> ModelConnection | None:
        return (
            await self.db.scalars(
                select(ModelConnection).where(
                    ModelConnection.organization_id == self.org_id,
                    ModelConnection.kind == kind,
                )
            )
        ).one_or_none()

    async def versions(self, row: ModelConnection) -> tuple[ModelProfile, ConnectionVersion]:
        profile = (
            await self.db.scalars(
                select(ModelProfile).where(
                    ModelProfile.organization_id == self.org_id,
                    ModelProfile.connection_id == row.id,
                    ModelProfile.id == row.active_profile_id,
                )
            )
        ).one()
        version = (
            await self.db.scalars(
                select(ConnectionVersion).where(
                    ConnectionVersion.organization_id == self.org_id,
                    ConnectionVersion.connection_id == row.id,
                    ConnectionVersion.id == row.current_connection_version_id,
                )
            )
        ).one()
        return profile, version

    async def embedding_locked(self) -> bool:
        # D04/D05 own these tables. Their acceptance transaction must share the org lock.
        # Fixed SQL only: a request can never supply a table or column name.
        has_documents = await self.db.scalar(
            text("SELECT to_regclass('app.documents') IS NOT NULL")
        )
        if has_documents and await self.db.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM app.documents "
                "WHERE organization_id=:org AND deleted_at IS NULL)"
            ),
            {"org": self.org_id},
        ):
            return True
        has_attempts = await self.db.scalar(
            text("SELECT to_regclass('app.document_attempts') IS NOT NULL")
        )
        if has_attempts:
            return bool(
                await self.db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM app.document_attempts "
                        "WHERE organization_id=:org "
                        "AND status IN ('queued','processing'))"
                    ),
                    {"org": self.org_id},
                )
            )
        return False
