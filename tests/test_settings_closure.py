"""D16 settings closure: real isolated PostgreSQL/S3 and explicitly controlled provider IO."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from model_fixture import ProviderFixture
from test_agents import Setup
from test_identity import PASSWORD, Actor, connection
from test_identity import client as client
from test_identity import database_url as database_url
from test_knowledge import application, send
from test_knowledge import setup as setup
from test_model_settings import candidate

from backend.services.model_probe import ModelProbe

pytestmark = pytest.mark.integration


def promote(owner: Actor, target: Actor) -> None:
    response = owner.request(
        "PATCH", f"/members/{target.id}", json={"baseRole": "admin", "expectedRevision": 1}
    )
    assert response.status_code == 200, response.text


def test_removed_transfer_target_never_becomes_owner(client: TestClient, database_url: str) -> None:
    owner, target = Actor(client).register(), Actor(client).register()
    owner.create_org()
    owner.add(target)
    old_revision = owner.request("GET", "/organizations/current").json()["data"]["accessRevision"]
    assert owner.request("DELETE", f"/members/{target.id}").status_code == 204
    for revision, code in ((old_revision, "VERSION_CONFLICT"), (old_revision + 1, "NOT_FOUND")):
        failed = owner.request(
            "POST",
            "/organizations/current/transfer-owner",
            json={"targetUserId": target.id, "expectedRevision": revision},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert failed.json()["error"]["code"] == code
    with connection(database_url) as db:
        assert db.execute(
            "SELECT owner_user_id FROM app.organizations WHERE id=%s", (owner.org,)
        ).fetchone() == (UUID(owner.id),)
        assert db.execute(
            "SELECT count(*) FROM app.audit_entries WHERE organization_id=%s AND "
            "action='organization.transfer'",
            (owner.org,),
        ).fetchone() == (0,)


def test_transfer_and_target_removal_race_preserves_one_owner(
    client: TestClient, database_url: str
) -> None:
    owner, admin, target = [Actor(client).register() for _ in range(3)]
    owner.create_org()
    owner.add(admin)
    owner.add(target)
    promote(owner, admin)
    revision = owner.request("GET", "/organizations/current").json()["data"]["accessRevision"]
    with ThreadPoolExecutor(2) as pool:
        transfer = pool.submit(
            owner.request,
            "POST",
            "/organizations/current/transfer-owner",
            json={"targetUserId": target.id, "expectedRevision": revision},
            headers={"Idempotency-Key": uuid4().hex},
        )
        remove = pool.submit(admin.request, "DELETE", f"/members/{target.id}")
        transferred, removed = transfer.result(timeout=15), remove.result(timeout=15)
    assert (transferred.status_code, removed.status_code) in {(200, 409), (409, 204)}
    roles = owner.request("GET", "/members").json()["data"]["items"]
    assert sum(row["effectiveRole"] == "owner" for row in roles) == 1
    with connection(database_url) as db:
        row = db.execute(
            "SELECT o.owner_user_id,m.user_id FROM app.organizations o JOIN "
            "app.org_memberships m ON m.organization_id=o.id AND "
            "m.user_id=o.owner_user_id WHERE o.id=%s",
            (owner.org,),
        ).fetchone()
        assert row and row[0] == row[1]
        assert db.execute(
            "SELECT count(*) FROM app.audit_entries WHERE organization_id=%s AND "
            "action='organization.transfer'",
            (owner.org,),
        ).fetchone() == (int(transferred.status_code == 200),)


def test_duplicate_member_add_is_atomic_and_does_not_demote(
    client: TestClient, database_url: str
) -> None:
    owner, admin, target = [Actor(client).register() for _ in range(3)]
    owner.create_org()
    owner.add(admin)
    promote(owner, admin)
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(
                actor.request,
                "POST",
                "/members",
                json={"email": target.email},
                headers={"Idempotency-Key": uuid4().hex},
            )
            for actor in (owner, admin)
        ]
        assert sorted(f.result(timeout=15).status_code for f in futures) == [200, 201]
    target.org = owner.org
    promote(owner, target)
    assert admin.add(target)["effectiveRole"] == "admin"
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.org_memberships WHERE organization_id=%s AND user_id=%s",
            (owner.org, target.id),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM app.audit_entries WHERE organization_id=%s AND "
            "target_id=%s AND action='member.add'",
            (owner.org, target.id),
        ).fetchone() == (1,)
    assert (
        admin.request(
            "PATCH", f"/members/{target.id}", json={"baseRole": "member", "expectedRevision": 2}
        ).status_code
        == 403
    )
    assert admin.request("DELETE", f"/members/{target.id}").status_code == 403
    ordinary = Actor(client).register()
    owner.add(ordinary)
    assert (
        admin.request(
            "PATCH", f"/members/{ordinary.id}", json={"baseRole": "admin", "expectedRevision": 1}
        ).status_code
        == 403
    )
    assert (
        ordinary.request(
            "PATCH", f"/members/{admin.id}", json={"baseRole": "member", "expectedRevision": 2}
        ).status_code
        == 403
    )


def test_transfer_lost_response_can_be_reconciled_by_current_org(
    client: TestClient, database_url: str
) -> None:
    owner, target = Actor(client).register(), Actor(client).register()
    owner.create_org()
    owner.add(target)
    revision = owner.request("GET", "/organizations/current").json()["data"]["accessRevision"]
    key = uuid4().hex
    value = {"targetUserId": target.id, "expectedRevision": revision}
    assert (
        owner.request(
            "POST",
            "/organizations/current/transfer-owner",
            json=value,
            headers={"Idempotency-Key": key},
        ).status_code
        == 200
    )
    current = owner.request("GET", "/organizations/current").json()["data"]
    assert current["effectiveRole"] == "admin"
    members = owner.request("GET", "/members").json()["data"]["items"]
    assert [m["userId"] for m in members if m["effectiveRole"] == "owner"] == [target.id]
    assert current["accessRevision"] == revision + 1
    assert (
        owner.request(
            "POST",
            "/organizations/current/transfer-owner",
            json=value,
            headers={"Idempotency-Key": key},
        ).status_code
        == 403
    )
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.audit_entries WHERE organization_id=%s AND "
            "action='organization.transfer'",
            (owner.org,),
        ).fetchone() == (1,)


def test_password_change_revokes_every_existing_session(
    client: TestClient, database_url: str
) -> None:
    owner = Actor(client).register()
    owner.create_org()
    peers = [Actor(client), Actor(client)]
    for peer in peers:
        result = peer.request(
            "POST", "/auth/login", json={"email": owner.email, "password": PASSWORD}
        )
        assert result.status_code == 200
        peer.org = owner.org
    bad = owner.request(
        "POST",
        "/me/password",
        json={"currentPassword": "wrong-current", "newPassword": "New-d16-password!"},
    )
    assert bad.status_code == 422
    assert all(peer.request("GET", "/organizations/current").status_code == 200 for peer in peers)
    old_headers = owner.headers()
    assert (
        owner.request(
            "POST",
            "/me/password",
            json={"currentPassword": PASSWORD, "newPassword": "New-d16-password!"},
        ).status_code
        == 204
    )
    assert client.get("/api/v1/organizations/current", headers=old_headers).status_code == 401
    assert all(peer.request("GET", "/organizations/current").status_code == 401 for peer in peers)
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.auth_sessions WHERE user_id=%s AND revoked_at IS NULL",
            (owner.id,),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM app.auth_sessions WHERE user_id=%s AND revoked_at IS NOT NULL",
            (owner.id,),
        ).fetchone() == (3,)
    fresh = Actor(client)
    assert (
        fresh.request(
            "POST", "/auth/login", json={"email": owner.email, "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        fresh.request(
            "POST", "/auth/login", json={"email": owner.email, "password": "New-d16-password!"}
        ).status_code
        == 200
    )


@pytest.mark.parametrize("change_semantic", [True, False])
def test_embedding_save_rechecks_documents_accepted_during_probe(
    setup: Setup, database_url: str, change_semantic: bool
) -> None:
    client, owner, kb, settings = setup
    initial = owner.request("GET", "/settings/models").json()["data"]["items"][0]
    fixture = ProviderFixture()
    app = application(client)
    app.state.model_probe = ModelProbe(settings, httpx.MockTransport(fixture.handle))
    assert client.portal is not None

    async def gate() -> None:
        fixture.started, fixture.release = asyncio.Event(), asyncio.Event()

    async def entered() -> None:
        assert fixture.started
        await asyncio.wait_for(fixture.started.wait(), 5)

    client.portal.call(gate)
    value: dict[str, Any] = {**candidate("embedding", 1), "credential": "synthetic-rotated-d16"}
    if change_semantic:
        value["modelName"] = "test-embedding-new-semantics"
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(owner.request, "PUT", "/settings/models/embedding", json=value)
        try:
            client.portal.call(entered)
            accepted = send(owner, kb, b"Document accepted while model test waits.", "race.txt")
            assert accepted.status_code == 202, accepted.text
            document = accepted.json()["data"]
        finally:
            assert fixture.release
            client.portal.call(fixture.release.set)
        result = pending.result(timeout=15)
    assert result.status_code == (409 if change_semantic else 200), result.text
    if change_semantic:
        assert result.json()["error"]["code"] == "EMBEDDING_LOCKED"
    current = owner.request("GET", "/settings/models").json()["data"]["items"][0]
    assert current["embeddingLocked"]
    assert current["profileId"] == initial["profileId"]
    assert current["revision"] == (1 if change_semantic else 2)
    with connection(database_url) as db:
        assert db.execute(
            "SELECT embedding_profile_id,status FROM app.document_attempts WHERE document_id=%s",
            (document["id"],),
        ).fetchone() == (UUID(initial["profileId"]), "queued")
        assert db.execute(
            "SELECT count(*) FROM app.model_profiles WHERE organization_id=%s", (owner.org,)
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM app.model_connection_versions WHERE organization_id=%s",
            (owner.org,),
        ).fetchone() == (1 if change_semantic else 2,)
        usage = db.execute(
            "SELECT status,token_source FROM app.usage_entries WHERE "
            "organization_id=%s ORDER BY created_at",
            (owner.org,),
        ).fetchall()
        assert usage == [("succeeded", "actual"), ("succeeded", "actual")]
        assert db.execute(
            "SELECT count(*) FROM app.outbox_jobs WHERE organization_id=%s AND resource_id=%s",
            (owner.org, document["id"]),
        ).fetchone() == (1,)
