import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete, literal, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.models.identity import (
    AuditEntry,
    AuthSession,
    IdempotencyKey,
    Membership,
    Organization,
    User,
    now,
)
from backend.repositories.identity import IdentityRepository
from backend.schemas import (
    DTO,
    AccountDTO,
    EmailInput,
    MemberDTO,
    OrganizationDTO,
    Page,
    PasswordInput,
    ProfileInput,
    RegisterInput,
    Role,
    RoleInput,
    SessionDTO,
    TransferInput,
    UserDTO,
)
from backend.services.errors import AppError
from backend.services.security import digest, hash_password, matches, token, verify_password


class PageCursor(DTO):
    organization_id: UUID | None
    actor_id: UUID
    access_revision: int
    created_at: datetime
    id: UUID


class IdentityService:
    def __init__(self, db: AsyncSession, settings: Settings, request_id: str) -> None:
        self.db = db
        self.repo = IdentityRepository(db)
        self.settings = settings
        self.request_id = request_id
        self.session: AuthSession | None = None
        self.user: User | None = None
        self.csrf = ""
        self.cookies: tuple[str, str, int] | None = None

    @classmethod
    @asynccontextmanager
    async def open(
        cls, engine: AsyncEngine, settings: Settings, request_id: str
    ) -> AsyncIterator["IdentityService"]:
        # Commit before the HTTP adapter sends a successful response or session cookies.
        async with async_sessionmaker(engine, expire_on_commit=False).begin() as db:
            yield cls(db, settings, request_id)

    async def authenticate(
        self, session_token: str, csrf_cookie: str, *, logout_only: bool = False
    ) -> None:
        row = await self.repo.session(digest(session_token)) if session_token else None
        if row and row.user_id:
            # All account/session writes take this lock, including login and password change.
            self.user = await self.repo.user(row.user_id, lock=True)
            row = await self.repo.session(digest(session_token))
        if not row:
            self.user = None
            return
        if not csrf_cookie or not matches(csrf_cookie, row.csrf_hash):
            self.user = None
            return
        if row.revoked_at or row.expires_at <= now():
            self.user = None
            if not logout_only:
                return
            # A retry may clear cookies again, but cannot restore authentication.
            self.session, self.csrf = row, csrf_cookie
            return
        self.session = row
        self.csrf = csrf_cookie
        row.last_seen_at = now()

    def check_csrf(self, origin: str | None, csrf_header: str | None) -> None:
        if origin not in self.settings.browser_origins:
            raise AppError(403, "ORIGIN_INVALID", "请求来源无效，请从本站重新操作。")
        if not self.session or not csrf_header or not matches(csrf_header, self.session.csrf_hash):
            raise AppError(403, "CSRF_INVALID", "页面验证已失效，请刷新后重试。")

    def require_user(self) -> User:
        if not self.user:
            raise AppError(401, "AUTH_REQUIRED", "请登录后继续。")
        return self.user

    def require_anonymous(self) -> None:
        if self.user:
            raise AppError(409, "ALREADY_AUTHENTICATED", "请先退出当前账号。")

    @staticmethod
    def user_dto(user: User) -> UserDTO:
        return UserDTO(
            id=user.id,
            email=user.normalized_email,
            display_name=user.display_name,
            revision=user.revision,
        )

    @staticmethod
    def role(org: Organization, member: Membership) -> Role:
        if member.user_id == org.owner_user_id:
            return "owner"
        return "admin" if member.base_role == "admin" else "member"

    def org_dto(self, org: Organization, member: Membership) -> OrganizationDTO:
        return OrganizationDTO(
            id=org.id,
            name=org.name,
            effective_role=self.role(org, member),
            access_revision=org.access_revision,
            settings_revision=org.settings_revision,
            created_at=org.created_at,
        )

    def member_dto(self, org: Organization, member: Membership, user: User) -> MemberDTO:
        return MemberDTO(
            user_id=user.id,
            email=user.normalized_email,
            display_name=user.display_name,
            effective_role=self.role(org, member),
            joined_at=member.joined_at,
            revision=member.revision,
        )

    async def rotate(self, user: User | None) -> None:
        if self.session:
            self.session.revoked_at = now()
        session_token, csrf = token(), token()
        seconds = 7 * 86400 if user else 1800
        row = AuthSession(
            user_id=user.id if user else None,
            token_hash=digest(session_token),
            csrf_hash=digest(csrf),
            expires_at=now() + timedelta(seconds=seconds),
        )
        self.db.add(row)
        await self.db.flush()
        self.session, self.user, self.csrf = row, user, csrf
        self.cookies = (session_token, csrf, seconds)

    async def session_info(self) -> SessionDTO:
        if not self.session:
            await self.rotate(None)
        return SessionDTO(
            user=self.user_dto(self.user) if self.user else None,
            csrf_token=self.csrf,
            organizations=await self.organization_list() if self.user else [],
            expires_at=self.session.expires_at if self.session else None,
        )

    async def register(self, data: RegisterInput) -> UserDTO:
        self.require_anonymous()
        password_hash = await hash_password(data.password)
        try:
            async with self.db.begin_nested():
                user = User(
                    normalized_email=str(data.email),
                    display_name=data.display_name,
                    password_hash=password_hash,
                )
                self.db.add(user)
                await self.db.flush()
        except IntegrityError as exc:
            raise AppError(409, "EMAIL_EXISTS", "该邮箱已注册，请直接登录。") from exc
        await self.rotate(user)
        return self.user_dto(user)

    async def login(self, email: str, password: str) -> UserDTO:
        self.require_anonymous()
        user = await self.repo.account(email, lock=True)
        valid = await verify_password(user.password_hash if user else None, password)
        if not user or not valid:
            raise AppError(401, "AUTH_INVALID", "邮箱或密码不正确。")
        await self.rotate(user)
        return self.user_dto(user)

    async def logout(self) -> None:
        if self.session:
            self.session.revoked_at = now()
        self.cookies = ("", "", 0)

    async def profile(self, data: ProfileInput) -> UserDTO:
        user = self.require_user()
        self.check_revision(data.expected_revision, user.revision)
        user.display_name = data.display_name
        user.revision += 1
        user.updated_at = now()
        return self.user_dto(user)

    async def password(self, data: PasswordInput) -> None:
        user = self.require_user()
        if not await verify_password(user.password_hash, data.current_password):
            raise AppError(422, "CURRENT_PASSWORD_INVALID", "当前密码不正确。")
        user.password_hash = await hash_password(data.new_password)
        user.password_changed_at = user.updated_at = now()
        user.revision += 1
        await self.db.execute(
            update(AuthSession).where(AuthSession.user_id == user.id).values(revoked_at=now())
        )
        self.cookies = ("", "", 0)

    async def organization_list(self) -> list[OrganizationDTO]:
        user = self.require_user()
        return [self.org_dto(org, member) for org, member in await self.repo.organizations(user.id)]

    async def organization_page(self, limit: int, cursor: str | None) -> Page[OrganizationDTO]:
        user = self.require_user()
        query = (
            select(Organization, Membership)
            .join(Membership, Membership.organization_id == Organization.id)
            .where(Membership.user_id == user.id)
        )
        if cursor:
            value = self.decode_cursor(cursor, None, 0)
            query = query.where(
                tuple_(Organization.created_at, Organization.id)
                < tuple_(literal(value.created_at), literal(value.id))
            )
        rows = (
            await self.db.execute(
                query.order_by(Organization.created_at.desc(), Organization.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1][0]
            next_cursor = self.encode_cursor(
                PageCursor(
                    organization_id=None,
                    actor_id=user.id,
                    access_revision=0,
                    created_at=last.created_at,
                    id=last.id,
                )
            )
        return Page(
            items=[self.org_dto(org, member) for org, member in rows[:limit]],
            next_cursor=next_cursor,
        )

    async def organization(self, org_id: UUID) -> tuple[Organization, Membership]:
        user = self.require_user()
        org = await self.repo.organization(org_id, user.id)
        # Recheck after locking: membership may have been removed while waiting for the lock.
        member = await self.repo.membership(org_id, user.id) if org else None
        if not org or not member:
            raise AppError(404, "NOT_FOUND", "组织不存在或你已无权访问。")
        return org, member

    def require_role(self, org: Organization, member: Membership, roles: set[str]) -> None:
        if self.role(org, member) not in roles:
            raise AppError(403, "FORBIDDEN", "你没有执行此操作的权限。")

    @staticmethod
    def check_revision(expected: int, current: int) -> None:
        if expected != current:
            raise AppError(
                409,
                "VERSION_CONFLICT",
                "内容已被更新，请刷新后重试。",
                {"currentRevision": current},
            )

    async def replay(
        self, org_id: UUID | None, operation: str, key: str, payload: dict[str, Any]
    ) -> IdempotencyKey | None:
        user = self.require_user()
        record = await self.db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.organization_id == org_id,
                IdempotencyKey.actor_id == user.id,
                IdempotencyKey.operation == operation,
                IdempotencyKey.resource_id.is_(None),
                IdempotencyKey.key == key,
            )
        )
        if record and record.request_digest != self.payload_digest(payload):
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "此提交标识已用于不同内容，请重新提交。")
        return record

    @staticmethod
    def payload_digest(payload: dict[str, Any]) -> str:
        return digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def remember(
        self,
        org_id: UUID | None,
        operation: str,
        key: str,
        payload: dict[str, Any],
        status: int,
        result: DTO,
        target_id: UUID,
    ) -> None:
        self.db.add(
            IdempotencyKey(
                organization_id=org_id,
                actor_id=self.require_user().id,
                operation=operation,
                key=key,
                request_digest=self.payload_digest(payload),
                response_status=status,
                response_data=result.model_dump(mode="json", by_alias=True),
                target_id=target_id,
            )
        )

    def audit(
        self,
        org: Organization,
        action: str,
        target_id: UUID,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        self.db.add(
            AuditEntry(
                organization_id=org.id,
                actor_id=self.require_user().id,
                action=action,
                target_type="organization" if "organization" in action else "member",
                target_id=target_id,
                before_summary=before,
                after_summary=after,
                request_id=self.request_id,
            )
        )

    async def create_organization(self, name: str, key: str) -> OrganizationDTO:
        user = self.require_user()
        payload = {"name": name}
        record = await self.replay(None, "organization.create", key, payload)
        if record:
            org, member = await self.organization(record.target_id)
            return self.org_dto(org, member)
        org = Organization(name=name, owner_user_id=user.id)
        self.db.add(org)
        await self.db.flush()
        member = Membership(organization_id=org.id, user_id=user.id, base_role="admin")
        self.db.add(member)
        await self.db.flush()
        result = self.org_dto(org, member)
        self.remember(None, "organization.create", key, payload, 201, result, org.id)
        self.audit(
            org, "organization.create", org.id, {}, {"name": name, "ownerUserId": str(user.id)}
        )
        return result

    def encode_cursor(self, value: PageCursor) -> str:
        if not self.session:
            raise AppError(401, "AUTH_REQUIRED", "请登录后继续。")
        body = base64.urlsafe_b64encode(value.model_dump_json().encode()).decode()
        signature = hmac.new(
            self.session.token_hash.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        return f"{body}.{signature}"

    def decode_cursor(self, cursor: str, org_id: UUID | None, access_revision: int) -> PageCursor:
        try:
            body, signature = cursor.split(".")
            if not self.session:
                raise ValueError
            expected = hmac.new(
                self.session.token_hash.encode(), body.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            value = PageCursor.model_validate_json(
                base64.b64decode(body, altchars=b"-_", validate=True)
            )
            if (
                value.organization_id != org_id
                or value.actor_id != self.require_user().id
                or value.access_revision != access_revision
                or value.created_at.tzinfo is None
            ):
                raise ValueError
            return value
        except (ValueError, ValidationError) as exc:
            raise AppError(422, "CURSOR_INVALID", "列表已变化或分页标识无效，请重新加载。") from exc

    async def members(self, org_id: UUID, limit: int, cursor: str | None) -> Page[MemberDTO]:
        org, _ = await self.organization(org_id)
        query = (
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.organization_id == org.id)
        )
        if cursor:
            value = self.decode_cursor(cursor, org.id, org.access_revision)
            query = query.where(
                tuple_(Membership.created_at, Membership.id)
                < tuple_(literal(value.created_at), literal(value.id))
            )
        rows = (
            await self.db.execute(
                query.order_by(Membership.created_at.desc(), Membership.id.desc()).limit(limit + 1)
            )
        ).all()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1][0]
            next_cursor = self.encode_cursor(
                PageCursor(
                    organization_id=org.id,
                    actor_id=self.require_user().id,
                    access_revision=org.access_revision,
                    created_at=last.created_at,
                    id=last.id,
                )
            )
        return Page(
            items=[self.member_dto(org, member, user) for member, user in rows[:limit]],
            next_cursor=next_cursor,
        )

    async def resolve(self, org_id: UUID, email: str) -> AccountDTO:
        org, actor = await self.organization(org_id)
        self.require_role(org, actor, {"owner", "admin"})
        user = await self.repo.account(email)
        if not user:
            raise AppError(404, "ACCOUNT_NOT_FOUND", "该账号尚未注册，请先让对方注册。")
        return AccountDTO(
            user_id=user.id, email=user.normalized_email, display_name=user.display_name
        )

    async def add_member(self, org_id: UUID, data: EmailInput, key: str) -> tuple[MemberDTO, int]:
        org, actor = await self.organization(org_id)
        self.require_role(org, actor, {"owner", "admin"})
        payload = data.model_dump(mode="json")
        record = await self.replay(org.id, "member.add", key, payload)
        user = await self.repo.account(str(data.email))
        if not user:
            raise AppError(404, "ACCOUNT_NOT_FOUND", "该账号尚未注册，请先让对方注册。")
        member = await self.repo.membership(org.id, user.id)
        if record:
            if not member:
                raise AppError(404, "NOT_FOUND", "该成员已被移除，请刷新后重新添加。")
            return self.member_dto(org, member, user), record.response_status
        status = 200 if member else 201
        if not member:
            member = Membership(organization_id=org.id, user_id=user.id)
            self.db.add(member)
            org.access_revision += 1
            await self.db.flush()
            self.audit(org, "member.add", user.id, {}, {"baseRole": "member"})
        result = self.member_dto(org, member, user)
        self.remember(org.id, "member.add", key, payload, status, result, user.id)
        return result, status

    async def change_role(self, org_id: UUID, user_id: UUID, data: RoleInput) -> MemberDTO:
        org, actor = await self.organization(org_id)
        self.require_role(org, actor, {"owner"})
        member = await self.repo.membership(org.id, user_id)
        user = await self.repo.user(user_id) if member else None
        if not member or not user:
            raise AppError(404, "NOT_FOUND", "成员不存在。")
        if user_id == org.owner_user_id:
            raise AppError(409, "OWNER_PROTECTED", "请通过转移所有权变更所有者。")
        self.check_revision(data.expected_revision, member.revision)
        before = member.base_role
        member.base_role = data.base_role
        member.revision += 1
        member.updated_at = now()
        org.access_revision += 1
        self.audit(org, "member.role", user_id, {"baseRole": before}, {"baseRole": data.base_role})
        return self.member_dto(org, member, user)

    async def remove_member(self, org_id: UUID, user_id: UUID) -> None:
        org, actor = await self.organization(org_id)
        self.require_role(org, actor, {"owner", "admin"})
        member = await self.repo.membership(org.id, user_id)
        if not member:
            return
        if member.user_id == org.owner_user_id:
            raise AppError(409, "OWNER_PROTECTED", "所有者须先转移所有权。")
        if self.role(org, actor) == "admin" and member.base_role != "member":
            raise AppError(403, "FORBIDDEN", "管理员只能移除普通成员。")
        await self.db.execute(
            delete(Membership).where(
                Membership.organization_id == org.id, Membership.user_id == user_id
            )
        )
        org.access_revision += 1
        self.audit(org, "member.remove", user_id, {"baseRole": member.base_role}, {})

    async def transfer(self, org_id: UUID, data: TransferInput, key: str) -> OrganizationDTO:
        org, actor = await self.organization(org_id)
        self.require_role(org, actor, {"owner"})
        payload = data.model_dump(mode="json")
        record = await self.replay(org.id, "organization.transfer", key, payload)
        if record:
            return self.org_dto(org, actor)
        self.check_revision(data.expected_revision, org.access_revision)
        target = await self.repo.membership(org.id, data.target_user_id)
        if not target:
            raise AppError(404, "NOT_FOUND", "目标账号不是当前组织成员。")
        if target.user_id == org.owner_user_id:
            raise AppError(422, "TRANSFER_TARGET_INVALID", "请选择另一位当前成员。")
        previous = org.owner_user_id
        org.owner_user_id = target.user_id
        org.access_revision += 1
        actor.base_role = "admin"
        actor.revision += 1
        actor.updated_at = now()
        target.revision += 1
        target.updated_at = now()
        result = self.org_dto(org, actor)
        self.remember(org.id, "organization.transfer", key, payload, 200, result, org.id)
        self.audit(
            org,
            "organization.transfer",
            org.id,
            {"ownerUserId": str(previous)},
            {"ownerUserId": str(target.user_id)},
        )
        return result
