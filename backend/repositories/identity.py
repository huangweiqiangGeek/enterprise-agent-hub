from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.identity import AuthSession, Membership, Organization, User


class IdentityRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def session(self, token_hash: str) -> AuthSession | None:
        return (
            await self.db.scalars(
                select(AuthSession)
                .where(AuthSession.token_hash == token_hash)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()

    async def user(self, user_id: UUID, *, lock: bool = False) -> User | None:
        query = select(User).where(User.id == user_id)
        if lock:
            query = query.with_for_update()
        return (
            await self.db.scalars(query.execution_options(populate_existing=True))
        ).one_or_none()

    async def account(self, email: str, *, lock: bool = False) -> User | None:
        query = select(User).where(User.normalized_email == email)
        return (await self.db.scalars(query.with_for_update() if lock else query)).one_or_none()

    async def membership(self, organization_id: UUID, user_id: UUID) -> Membership | None:
        return (
            await self.db.scalars(
                select(Membership).where(
                    Membership.organization_id == organization_id, Membership.user_id == user_id
                )
            )
        ).one_or_none()

    async def organization(self, organization_id: UUID, user_id: UUID) -> Organization | None:
        # Scope before loading; lock the organization for the complete authorized use case.
        return (
            await self.db.scalars(
                select(Organization)
                .join(Membership, Membership.organization_id == Organization.id)
                .where(Organization.id == organization_id, Membership.user_id == user_id)
                .with_for_update(of=Organization)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()

    async def organizations(self, user_id: UUID) -> list[tuple[Organization, Membership]]:
        result = await self.db.execute(
            select(Organization, Membership)
            .join(Membership, Membership.organization_id == Organization.id)
            .where(Membership.user_id == user_id)
            .order_by(Organization.created_at.desc(), Organization.id.desc())
        )
        return [(org, member) for org, member in result.all()]
