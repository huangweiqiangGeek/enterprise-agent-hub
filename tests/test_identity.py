"""D02 contract and concurrency checks against a newly-created PostgreSQL database."""

import asyncio
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import httpx2 as httpx
import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy.engine import make_url

from backend.api.app import create_app
from backend.config import Settings
from backend.db import migration_config

pytestmark = pytest.mark.integration
PASSWORD = "  Eah-password-2026  "
ORIGIN = "http://127.0.0.1:3300"


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    admin = os.getenv("EAH_TEST_ADMIN_URL")
    if not admin:
        pytest.skip("UNVERIFIED: EAH_TEST_ADMIN_URL is required for D02 integration tests")
    url = make_url(admin)
    name = f"eah_d02_test_{uuid4().hex}"
    admin_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    target = url.set(database=name, drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    previous = os.environ.get("EAH_DATABASE_URL")
    try:
        os.environ["EAH_DATABASE_URL"] = target
        command.upgrade(migration_config(), "head")
        command.upgrade(migration_config(), "head")
        yield target
    finally:
        if previous is None:
            os.environ.pop("EAH_DATABASE_URL", None)
        else:
            os.environ["EAH_DATABASE_URL"] = previous
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(
        create_app(Settings(database_url=database_url)),
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as value:
        yield value


@contextmanager
def connection(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    ) as value:
        yield value


class Actor:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.cookies: dict[str, str] = {}
        self.csrf = ""
        self.email = f"{uuid4().hex}@example.com"
        self.id = ""
        self.org = ""
        self.refresh()

    def headers(self) -> dict[str, str]:
        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            "Origin": ORIGIN,
            "X-CSRF-Token": self.csrf,
        }
        if self.org:
            headers["X-Organization-Id"] = self.org
        return headers

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = self.headers()
        headers.update(kwargs.pop("headers", {}))
        response = self.client.request(method, f"/api/v1{path}", headers=headers, **kwargs)
        for cookie in response.cookies.jar:
            self.cookies[cookie.name] = cookie.value or ""
        if "eah_csrf" in self.cookies:
            self.csrf = self.cookies["eah_csrf"]
        assert response.headers["cache-control"] == "no-store"
        if response.content:
            assert response.json()["requestId"] == response.headers["x-request-id"]
        return response

    def refresh(self) -> dict[str, Any]:
        response = self.request("GET", "/auth/session")
        assert response.status_code == 200, response.text
        value: dict[str, Any] = response.json()["data"]
        self.csrf = value["csrfToken"]
        return value

    def register(self) -> "Actor":
        response = self.request(
            "POST",
            "/auth/register",
            json={
                "email": self.email,
                "password": PASSWORD,
                "displayName": "测试成员",
            },
        )
        assert response.status_code == 201, response.text
        self.id = response.json()["data"]["id"]
        return self

    def create_org(self, name: str = "研发团队") -> dict[str, Any]:
        response = self.request(
            "POST", "/organizations", json={"name": name}, headers={"Idempotency-Key": uuid4().hex}
        )
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()["data"]
        self.org = result["id"]
        return result

    def add(self, other: "Actor") -> dict[str, Any]:
        response = self.request(
            "POST",
            "/members",
            json={"email": other.email},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code in {200, 201}, response.text
        other.org = self.org
        result: dict[str, Any] = response.json()["data"]
        return result


def test_registration_cookie_csrf_validation_and_password_hash(
    client: TestClient, database_url: str
) -> None:
    actor = Actor(client)
    anonymous = actor.cookies.copy()
    assert actor.refresh()["user"] is None
    missing = actor.request("POST", "/auth/register", json={}, headers={"X-CSRF-Token": ""})
    assert missing.status_code == 403
    forged = actor.request(
        "POST", "/auth/register", json={}, headers={"Origin": "https://evil.example"}
    )
    assert forged.json()["error"]["code"] == "ORIGIN_INVALID"
    invalid = actor.request(
        "POST",
        "/auth/register",
        json={
            "email": "invalid",
            "password": "secret",
            "displayName": " ",
            "role": "owner",
        },
    )
    assert invalid.status_code == 422 and "secret" not in invalid.text
    actor.register()
    assert actor.cookies["eah_session"] != anonymous["eah_session"]
    assert actor.cookies["eah_csrf"] != anonymous["eah_csrf"]
    assert actor.refresh()["user"]["email"] == actor.email
    another = Actor(client)
    duplicate = another.request(
        "POST",
        "/auth/register",
        json={
            "email": f"  {actor.email.upper()} ",
            "password": PASSWORD,
            "displayName": "重复",
        },
    )
    assert duplicate.json()["error"]["code"] == "EMAIL_EXISTS"
    cookie_headers = another.request("GET", "/auth/session").headers
    assert cookie_headers["cache-control"] == "no-store"
    fresh = client.get("/api/v1/auth/session", headers={"Cookie": ""})
    assert len(fresh.headers.get_list("set-cookie")) == 2
    assert all(
        "HttpOnly" in c and "SameSite=lax" in c and "Path=/" in c
        for c in fresh.headers.get_list("set-cookie")
    )
    with connection(database_url) as db:
        row = db.execute("SELECT password_hash FROM app.users WHERE id=%s", (actor.id,)).fetchone()
        assert row and row[0].startswith("$argon2id$") and PASSWORD not in row[0]
        stored = db.execute(
            "SELECT token_hash, csrf_hash FROM app.auth_sessions WHERE user_id=%s", (actor.id,)
        ).fetchone()
        assert stored and stored[0] != actor.cookies["eah_session"] and stored[1] != actor.csrf


def test_logout_password_revocation_and_current_password(client: TestClient) -> None:
    actor = Actor(client).register()
    other = Actor(client)
    login = other.request("POST", "/auth/login", json={"email": actor.email, "password": PASSWORD})
    assert login.status_code == 200
    wrong = actor.request(
        "POST",
        "/me/password",
        json={
            "currentPassword": PASSWORD.strip(),
            "newPassword": "new-password-2026",
        },
    )
    assert wrong.json()["error"]["code"] == "CURRENT_PASSWORD_INVALID"
    old_cookies = actor.headers()
    change = actor.request(
        "POST",
        "/me/password",
        json={
            "currentPassword": PASSWORD,
            "newPassword": "new-password-2026",
        },
    )
    assert change.status_code == 204 and not change.content
    assert other.request("GET", "/organizations").status_code == 401
    assert client.get("/api/v1/organizations", headers=old_cookies).status_code == 401
    newcomer = Actor(client)
    assert (
        newcomer.request(
            "POST", "/auth/login", json={"email": actor.email, "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        newcomer.request(
            "POST", "/auth/login", json={"email": actor.email, "password": "new-password-2026"}
        ).status_code
        == 200
    )
    saved = newcomer.headers()
    assert newcomer.request("POST", "/auth/logout").status_code == 204
    assert client.get("/api/v1/organizations", headers=saved).status_code == 401
    assert client.post("/api/v1/auth/logout", headers=saved).status_code == 204
    newcomer.refresh()
    assert newcomer.request("POST", "/auth/logout").status_code == 204


def test_profile_revision_expiry_and_secure_cookies(client: TestClient, database_url: str) -> None:
    actor = Actor(client).register()
    result = actor.request("PATCH", "/me", json={"displayName": "更新姓名", "expectedRevision": 1})
    assert result.status_code == 200 and result.json()["data"]["revision"] == 2
    assert (
        actor.request(
            "PATCH", "/me", json={"displayName": "过期", "expectedRevision": 1}
        ).status_code
        == 409
    )
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.auth_sessions SET expires_at=now()-interval '1 second' WHERE user_id=%s",
            (actor.id,),
        )
    assert actor.request("GET", "/organizations").status_code == 401
    assert actor.refresh()["user"] is None
    with TestClient(
        create_app(Settings(database_url=database_url, cookie_secure=True)),
        base_url="https://example.com",
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as secure:
        response = secure.get("/api/v1/auth/session")
        assert all("Secure" in item for item in response.headers.get_list("set-cookie"))


def test_organization_idempotency_parallel_and_isolation(
    client: TestClient, database_url: str
) -> None:
    actor = Actor(client).register()
    outsider = Actor(client).register()
    key = uuid4().hex
    headers = {**actor.headers(), "Idempotency-Key": key}

    def create() -> httpx.Response:
        return client.post("/api/v1/organizations", headers=headers, json={"name": "并发组织"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create(), range(4)))
    assert [r.status_code for r in results] == [201] * 4, [r.text for r in results]
    ids = {r.json()["data"]["id"] for r in results}
    assert len(ids) == 1
    actor.org = next(iter(ids))
    conflict = actor.request(
        "POST", "/organizations", json={"name": "另一个"}, headers={"Idempotency-Key": key}
    )
    assert conflict.status_code == 409
    outsider.org = actor.org
    assert outsider.request("GET", "/organizations/current").status_code == 404
    assert outsider.request("GET", "/members").status_code == 404
    assert (
        outsider.request("POST", "/members/resolve", json={"email": actor.email}).status_code == 404
    )
    assert outsider.request("GET", "/organizations").json()["data"]["items"] == []
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.organizations WHERE id=%s", (actor.org,)
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM app.org_memberships WHERE organization_id=%s", (actor.org,)
        ).fetchone() == (1,)


def test_member_roles_live_revocation_and_owner_transfer(client: TestClient) -> None:
    owner, admin, member, other_admin = [Actor(client).register() for _ in range(4)]
    owner.create_org()
    for other in (admin, member, other_admin):
        owner.add(other)
    for other in (admin, other_admin):
        promote = owner.request(
            "PATCH", f"/members/{other.id}", json={"baseRole": "admin", "expectedRevision": 1}
        )
        assert promote.status_code == 200
    assert admin.request("GET", "/organizations/current").json()["data"]["effectiveRole"] == "admin"
    assert (
        member.request("POST", "/members/resolve", json={"email": owner.email}).status_code == 403
    )
    for target in (owner, other_admin):
        assert admin.request("DELETE", f"/members/{target.id}").status_code in {403, 409}
        assert (
            admin.request(
                "PATCH", f"/members/{target.id}", json={"baseRole": "member", "expectedRevision": 1}
            ).status_code
            == 403
        )
    assert (
        owner.request("DELETE", f"/members/{owner.id}").json()["error"]["code"] == "OWNER_PROTECTED"
    )
    assert (
        admin.request(
            "POST",
            "/members",
            json={"email": member.email, "baseRole": "admin"},
            headers={"Idempotency-Key": uuid4().hex},
        ).status_code
        == 422
    )
    assert (
        owner.request(
            "PATCH", f"/members/{admin.id}", json={"baseRole": "member", "expectedRevision": 2}
        ).status_code
        == 200
    )
    assert admin.request("POST", "/members/resolve", json={"email": owner.email}).status_code == 403
    revision = owner.request("GET", "/organizations/current").json()["data"]["accessRevision"]
    key = uuid4().hex
    data = {"targetUserId": member.id, "expectedRevision": revision}
    transfer = owner.request(
        "POST", "/organizations/current/transfer-owner", json=data, headers={"Idempotency-Key": key}
    )
    assert transfer.status_code == 200 and transfer.json()["data"]["effectiveRole"] == "admin"
    assert (
        member.request("GET", "/organizations/current").json()["data"]["effectiveRole"] == "owner"
    )
    assert (
        owner.request(
            "POST",
            "/organizations/current/transfer-owner",
            json=data,
            headers={"Idempotency-Key": key},
        ).status_code
        == 403
    )
    assert member.request("DELETE", f"/members/{admin.id}").status_code == 204
    assert admin.request("GET", "/members").status_code == 404
    roles = member.request("GET", "/members").json()["data"]["items"]
    assert sum(row["effectiveRole"] == "owner" for row in roles) == 1


def test_member_add_confirmation_paging_and_replay_permissions(client: TestClient) -> None:
    owner = Actor(client).register()
    member, other = [Actor(client).register() for _ in range(2)]
    owner.create_org()
    assert (
        owner.request("POST", "/members/resolve", json={"email": member.email}).json()["data"][
            "userId"
        ]
        == member.id
    )
    assert (
        owner.request("POST", "/members/resolve", json={"email": "missing@example.com"}).json()[
            "error"
        ]["code"]
        == "ACCOUNT_NOT_FOUND"
    )
    key = uuid4().hex
    payload = {"email": member.email}
    for _ in range(2):
        assert (
            owner.request(
                "POST", "/members", json=payload, headers={"Idempotency-Key": key}
            ).status_code
            == 201
        )
    member.org = owner.org
    assert owner.add(member)["effectiveRole"] == "member"
    owner.add(other)
    page1 = owner.request("GET", "/members?limit=1").json()["data"]
    cursor = page1["nextCursor"]
    page2 = owner.request("GET", "/members", params={"limit": 1, "cursor": cursor}).json()["data"]
    assert page1["items"][0]["userId"] != page2["items"][0]["userId"]
    assert member.request("GET", "/members", params={"cursor": cursor}).status_code == 422
    assert owner.request("GET", "/members", params={"cursor": cursor + "x"}).status_code == 422
    assert owner.request("DELETE", f"/members/{member.id}").status_code == 204
    assert (
        owner.request(
            "POST", "/members", json=payload, headers={"Idempotency-Key": key}
        ).status_code
        == 404
    )
    assert owner.request("GET", "/members", params={"cursor": cursor}).status_code == 422


def test_database_owner_constraints_and_concurrent_transfer(
    client: TestClient, database_url: str
) -> None:
    owner, first, second = [Actor(client).register() for _ in range(3)]
    owner.create_org()
    owner.add(first)
    owner.add(second)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection(database_url) as db:
        db.execute(
            "DELETE FROM app.org_memberships WHERE organization_id=%s AND user_id=%s",
            (owner.org, owner.id),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection(database_url) as db:
        db.execute(
            "UPDATE app.organizations SET owner_user_id=%s WHERE id=%s", (uuid4(), owner.org)
        )
    with pytest.raises(psycopg.errors.CheckViolation), connection(database_url) as db:
        db.execute(
            "UPDATE app.org_memberships SET base_role='owner' WHERE organization_id=%s",
            (owner.org,),
        )
    rev = owner.request("GET", "/organizations/current").json()["data"]["accessRevision"]
    headers = owner.headers()

    def transfer(target: Actor) -> httpx.Response:
        return client.post(
            "/api/v1/organizations/current/transfer-owner",
            headers={**headers, "Idempotency-Key": uuid4().hex},
            json={"targetUserId": target.id, "expectedRevision": rev},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(transfer, (first, second)))
    assert sorted(r.status_code for r in responses) == [200, 403]
    rows = owner.request("GET", "/members").json()["data"]["items"]
    assert sum(row["effectiveRole"] == "owner" for row in rows) == 1


def test_unknown_fields_and_user_supplied_role_are_rejected(client: TestClient) -> None:
    actor = Actor(client).register()
    actor.create_org()
    response = actor.request(
        "POST",
        "/organizations",
        json={"name": "伪造", "userId": str(uuid4()), "role": "owner"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 422
    response = actor.request(
        "GET",
        "/organizations/current",
        headers={"X-Organization-Id": str(uuid4()), "X-Role": "owner"},
    )
    assert response.status_code == 404


def test_organization_cursor_is_scoped_and_password_race_revokes_old_login(
    client: TestClient,
) -> None:
    actor = Actor(client).register()
    actor.create_org("第一页组织")
    actor.create_org("第二页组织")
    first = actor.request("GET", "/organizations?limit=1").json()["data"]
    second = actor.request(
        "GET", "/organizations", params={"limit": 1, "cursor": first["nextCursor"]}
    ).json()["data"]
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert second["nextCursor"] is None
    other = Actor(client).register()
    assert (
        other.request("GET", "/organizations", params={"cursor": first["nextCursor"]}).status_code
        == 422
    )
    login_actor = Actor(client)

    def login() -> httpx.Response:
        return login_actor.request(
            "POST", "/auth/login", json={"email": actor.email, "password": PASSWORD}
        )

    def change() -> httpx.Response:
        return actor.request(
            "POST",
            "/me/password",
            json={"currentPassword": PASSWORD, "newPassword": "concurrent-new-password"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        login_future, change_future = pool.submit(login), pool.submit(change)
        assert login_future.result().status_code in {200, 401}
        assert change_future.result().status_code == 204
    assert login_actor.request("GET", "/organizations").status_code == 401
