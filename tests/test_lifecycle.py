"""D17 destructive checks only use automatically created disposable PostgreSQL/S3 fixtures."""

from datetime import timedelta
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from model_fixture import ProviderFixture
from pydantic import ValidationError
from test_agents import Setup, configured, create
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_ingestion import process, worker
from test_knowledge import application, send
from test_knowledge import setup as setup
from test_runs import QAProvider, accept, conversation, execute, snapshot

from backend.config import Settings
from backend.models.identity import now
from backend.services.errors import AppError
from backend.services.lifecycle import Lifecycle
from backend.workers.outbox import Outbox

pytestmark = pytest.mark.integration


def required_first(row: tuple[Any, ...] | None) -> Any:
    assert row is not None
    return row[0]


def maintenance(setup: Setup) -> Lifecycle:
    return Lifecycle(application(setup[0]).state.engine, setup[3])


def outbox(setup: Setup) -> Outbox:
    app = application(setup[0])
    return Outbox(app.state.engine, setup[3], app.state.storage)


def test_terminal_retention_failure_retry_keeps_business_history(
    setup: Setup, database_url: str
) -> None:
    client, owner, _, _ = setup
    cid, agent = conversation(setup)
    completed = []
    for _ in range(3):
        rid = accept(setup, cid, agent).json()["data"]["runId"]
        execute(setup, rid, QAProvider())
        assert snapshot(setup, rid)["status"] == "succeeded"
        completed.append(rid)
    active = accept(setup, cid, agent).json()["data"]["runId"]
    old, exact, leased = completed
    clock = now()
    tables = [
        "runs",
        "messages",
        "run_sources",
        "run_citations",
        "usage_entries",
        "idempotency_keys",
    ]
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.runs SET finished_at=%s WHERE id=%s", (clock - timedelta(days=8), old)
        )
        db.execute(
            "UPDATE app.runs SET finished_at=%s WHERE id=%s", (clock - timedelta(days=7), exact)
        )
        db.execute(
            "UPDATE app.runs SET finished_at=%s,lease_until=%s WHERE id=%s",
            (clock - timedelta(days=8), clock + timedelta(hours=1), leased),
        )
        before = {
            table: db.execute(f"SELECT count(*) FROM app.{table}").fetchone() for table in tables
        }
        seq = db.execute("SELECT last_seq FROM app.runs WHERE id=%s", (old,)).fetchone()
        assert (
            required_first(
                db.execute(
                    "SELECT count(*) FROM checkpoints.checkpoints WHERE thread_id LIKE %s",
                    (f"%:{old}:%",),
                ).fetchone()
            )
            > 0
        )
    assert client.portal
    life = maintenance(setup)
    with (
        patch("backend.services.lifecycle.now", return_value=clock),
        patch(
            "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver.adelete_thread",
            new=AsyncMock(side_effect=OSError("private checkpoint detail")),
        ),
    ):
        failed = client.portal.call(life.sweep, 1)
    assert failed["runCleanupFailed"] == 1
    with connection(database_url) as db:
        assert (
            required_first(
                db.execute("SELECT count(*) FROM app.run_events WHERE run_id=%s", (old,)).fetchone()
            )
            > 0
        )
    with patch("backend.services.lifecycle.now", return_value=clock):
        result = client.portal.call(life.sweep, 1)
    assert result["runsCleaned"] == 1 and result["runCleanupFailed"] == 0
    with connection(database_url) as db:
        assert {
            table: db.execute(f"SELECT count(*) FROM app.{table}").fetchone() for table in tables
        } == before
        assert db.execute("SELECT last_seq FROM app.runs WHERE id=%s", (old,)).fetchone() == seq
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            assert db.execute(
                f"SELECT count(*) FROM checkpoints.{table} WHERE thread_id LIKE %s", (f"%:{old}:%",)
            ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s", (old,)
        ).fetchone() == (0,)
        assert (
            required_first(
                db.execute(
                    "SELECT count(*) FROM app.run_events WHERE run_id IN (%s,%s)", (exact, leased)
                ).fetchone()
            )
            > 0
        )
        assert db.execute("SELECT status FROM app.runs WHERE id=%s", (active,)).fetchone() == (
            "queued",
        )
    assert owner.request("GET", f"/runs/{old}/events").status_code == 409
    assert snapshot(setup, old)["result"]
    assert owner.request("GET", f"/runs/{old}/export").status_code == 200


def test_session_audit_retention_boundaries_and_batch(setup: Setup, database_url: str) -> None:
    client, owner, _, _ = setup
    actors = [Actor(client).register() for _ in range(4)]
    clock = now()
    with connection(database_url) as db:
        for actor, days in zip(actors[:2], [8, 7], strict=True):
            db.execute(
                "UPDATE app.auth_sessions SET expires_at=%s WHERE user_id=%s",
                (clock - timedelta(days=days), actor.id),
            )
        db.execute(
            "UPDATE app.auth_sessions SET revoked_at=%s WHERE user_id=%s",
            (clock - timedelta(days=8), actors[2].id),
        )
        audit = required_first(
            db.execute(
                "SELECT id FROM app.audit_entries WHERE organization_id=%s "
                "ORDER BY created_at LIMIT 1",
                (owner.org,),
            ).fetchone()
        )
        db.execute(
            "UPDATE app.audit_entries SET occurred_at=%s WHERE id=%s",
            (clock - timedelta(days=30), audit),
        )
    assert client.portal
    life = maintenance(setup)
    with patch("backend.services.lifecycle.now", return_value=clock):
        first = client.portal.call(life.expired_identity, 1)
        second = client.portal.call(life.expired_identity, 1)
    assert first["sessions"] == second["sessions"] == 1
    assert first["audits"] == second["audits"] == 0
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.auth_sessions WHERE user_id IN (%s,%s)",
            (actors[1].id, actors[3].id),
        ).fetchone() == (2,)
        db.execute(
            "UPDATE app.audit_entries SET occurred_at=%s WHERE id=%s",
            (clock - timedelta(days=30, seconds=1), audit),
        )
    with patch("backend.services.lifecycle.now", return_value=clock):
        assert client.portal.call(life.expired_identity, 1)["audits"] == 1
    assert owner.request("GET", "/organizations/current").status_code == 200


def test_failed_attempt_cleanup_skips_active_refs_and_retries_s3_failure(
    setup: Setup, database_url: str
) -> None:
    client, owner, kb, _ = setup
    config = configured(setup)
    docs = [
        send(owner, kb, f"Retained derived payload {n}".encode(), f"{n}.txt").json()["data"]
        for n in range(3)
    ]
    for doc in docs:
        process(setup, doc)
    agent = create(owner, **config)
    cid = owner.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": "qa"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["id"]
    active = owner.request(
        "POST",
        f"/conversations/{cid}/runs",
        json={
            "agentId": agent["id"],
            "input": {
                "question": "Check",
                "knowledgeBaseIds": [kb],
                "documentIds": [docs[0]["id"]],
            },
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert active.status_code == 202
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.document_attempts SET "
            "status='failed',finished_at=now()-interval '25 hours' WHERE document_id "
            "IN (%s,%s,%s)",
            tuple(d["id"] for d in docs),
        )
        db.execute(
            "UPDATE app.document_attempts SET lease_until=now()+interval '1 hour' "
            "WHERE document_id=%s",
            (docs[2]["id"],),
        )
        retained = db.execute(
            "SELECT id,extracted_object_key FROM app.document_attempts WHERE document_id=%s",
            (docs[1]["id"],),
        ).fetchone()
        original = required_first(
            db.execute(
                "SELECT object_key FROM app.documents WHERE id=%s", (docs[1]["id"],)
            ).fetchone()
        )
    assert retained and client.portal
    life, delivery = maintenance(setup), outbox(setup)
    try:
        assert client.portal.call(life.failed_attempts, 1) == 1
        with patch.object(
            delivery.storage,
            "delete",
            new=AsyncMock(side_effect=AppError(503, "STORAGE_UNAVAILABLE", "private")),
        ):
            assert client.portal.call(delivery.dispatch)["failed"] == 1
        with connection(database_url) as db:
            db.execute(
                "UPDATE app.outbox_jobs SET publish_attempts=5 WHERE "
                "job_type='attempt.cleanup' AND resource_version=%s",
                (retained[0],),
            )
            assert db.execute(
                "SELECT text IS NOT NULL FROM app.document_chunks WHERE attempt_id=%s",
                (retained[0],),
            ).fetchone() == (True,)
        assert client.portal.call(life.sweep)["cleanupExhausted"] == 1
        assert client.portal.call(delivery.retry_failed) == 1
        assert client.portal.call(delivery.dispatch)["published"] == 1
        with connection(database_url) as db:
            assert db.execute(
                "SELECT text,embedding FROM app.document_chunks WHERE attempt_id=%s", (retained[0],)
            ).fetchone() == (None, None)
            assert db.execute(
                "SELECT extracted_object_key,status FROM app.document_attempts WHERE id=%s",
                (retained[0],),
            ).fetchone() == (None, "failed")
            assert db.execute(
                "SELECT count(*) FROM app.document_chunks WHERE document_id IN (%s,%s) "
                "AND text IS NOT NULL",
                (docs[0]["id"], docs[2]["id"]),
            ).fetchone() == (2,)
        keys = [
            v["Key"]
            for v in delivery.storage.client.list_objects_v2(Bucket=setup[3].s3_bucket)["Contents"]
        ]
        assert retained[1] not in keys and original in keys
    finally:
        delivery.broker.close()


def test_deleted_document_waits_for_active_run_then_cleans(setup: Setup, database_url: str) -> None:
    client, owner, _, _ = setup
    cid, agent = conversation(setup)
    rid = accept(setup, cid, agent).json()["data"]["runId"]
    with connection(database_url) as db:
        doc = db.execute(
            "SELECT id,object_key FROM app.documents WHERE organization_id=%s", (owner.org,)
        ).fetchone()
    assert doc and client.portal
    assert owner.request("DELETE", f"/documents/{doc[0]}").status_code == 204
    delivery = outbox(setup)
    try:
        client.portal.call(delivery.dispatch)
        assert owner.request("GET", f"/documents/{doc[0]}/content").status_code == 404
        assert (
            delivery.storage.client.head_object(Bucket=setup[3].s3_bucket, Key=doc[1])[
                "ContentLength"
            ]
            > 0
        )
        assert owner.request("POST", f"/runs/{rid}/cancel").status_code in {200, 202}
        with connection(database_url) as db:
            db.execute(
                "UPDATE app.outbox_jobs SET available_at=now() WHERE "
                "job_type='document.cleanup' AND resource_id=%s",
                (doc[0],),
            )
        client.portal.call(delivery.dispatch)
        with connection(database_url) as db:
            assert db.execute(
                "SELECT cleanup_status FROM app.documents WHERE id=%s", (doc[0],)
            ).fetchone() == ("done",)
    finally:
        delivery.broker.close()


def test_orphan_scan_protects_unpublished_live_lease(setup: Setup) -> None:
    client, owner, kb, settings = setup
    doc = send(owner, kb).json()["data"]
    ingest = worker(setup)
    assert client.portal
    lease = client.portal.call(
        ingest.claim, UUID(owner.org), UUID(doc["id"]), UUID(doc["currentAttemptId"])
    )
    assert lease
    key = f"{settings.s3_prefix}{owner.org}/derived/{lease.attempt.id}/{lease.attempt.lease_owner}"
    client.portal.call(ingest.storage.put, key, BytesIO(b"in progress"), 11, "text/plain")
    delivery = outbox(setup)
    try:
        with patch("backend.workers.outbox.now", return_value=now() + timedelta(hours=25)):
            assert client.portal.call(delivery.clean_orphans)["deleted"] == 0
    finally:
        delivery.broker.close()


def test_capacity_defaults_and_history_limit_idempotency(setup: Setup) -> None:
    assert Settings().max_documents_per_organization == 1000
    assert Settings().max_chunks_per_organization == 50000
    assert Settings().max_runs_per_organization == 10000
    with pytest.raises(ValidationError):
        Settings(max_runs_per_organization=10001)
    client, owner, _, settings = setup
    cid, agent = conversation(setup)
    settings.max_runs_per_organization = 1
    key = uuid4().hex
    accepted = accept(setup, cid, agent, key=key)
    assert accepted.status_code == 202
    assert accept(setup, cid, agent, key=key).status_code == 202
    other = owner.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": "qa"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["id"]
    rejected = accept(setup, other, agent)
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "RUN_LIMIT_REACHED"
    settings.max_documents_per_organization = 1
    assert send(owner, setup[2]).json()["error"]["code"] == "DOCUMENT_LIMIT_REACHED"


def test_chunk_capacity_checks_before_embedding_and_preserves_prior_data(setup: Setup) -> None:
    _, owner, kb, settings = setup
    configured(setup)
    settings.max_chunks_per_organization = 1
    doc = send(owner, kb, b"Too many current chunks", "extra.txt").json()["data"]
    fixture = ProviderFixture()
    service = worker(setup, fixture)
    process(setup, doc, service)
    result = owner.request("GET", f"/documents/{doc['id']}").json()["data"]["document"]
    assert result["status"] == "failed" and result["errorCode"] == "INGEST_LIMIT_EXCEEDED"
    assert not fixture.requests
