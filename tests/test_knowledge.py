"""D04 uses real PostgreSQL, local S3, Redis and controlled model protocol only."""

import asyncio
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from model_fixture import ProviderFixture
from redis import Redis
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_model_settings import candidate

from backend.api.app import create_app
from backend.config import Settings
from backend.models.identity import now
from backend.services.errors import AppError
from backend.services.knowledge import KnowledgeService
from backend.services.model_probe import ModelProbe
from backend.services.upload import Upload
from backend.workers.outbox import Outbox

pytestmark = pytest.mark.integration


def application(client: TestClient) -> FastAPI:
    assert isinstance(client.app, FastAPI)
    return client.app


@pytest.fixture
def setup(database_url: str) -> Iterator[tuple[TestClient, Actor, str, Settings]]:
    endpoint = os.getenv("EAH_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("UNVERIFIED: EAH_TEST_S3_ENDPOINT is required for real S3 protocol integration")
    settings = Settings(
        database_url=database_url,
        s3_endpoint=endpoint,
        s3_bucket=f"eah-test-{uuid4().hex}",
        ingestion_queue=f"eah.test.{uuid4().hex}",
        run_queue=f"eah.test.runs.{uuid4().hex}",
    )
    app = create_app(settings)
    app.state.storage.client.create_bucket(Bucket=settings.s3_bucket)
    app.state.model_probe = ModelProbe(settings, httpx.MockTransport(ProviderFixture().handle))
    owner: Actor | None = None
    try:
        with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
            owner = Actor(client).register()
            owner.create_org()
            assert (
                owner.request(
                    "PUT", "/settings/models/embedding", json=candidate("embedding")
                ).status_code
                == 200
            )
            kb = owner.request(
                "POST",
                "/knowledge-bases",
                json={"name": "研发资料", "description": "D04"},
                headers={"Idempotency-Key": uuid4().hex},
            ).json()["data"]["id"]
            yield client, owner, kb, settings
    finally:
        # This bucket/queue were uniquely created by this fixture, never the experience bucket.
        if owner and owner.org:
            with connection(database_url) as db:
                db.execute(
                    "UPDATE app.outbox_jobs SET published_at=now() WHERE organization_id=%s",
                    (owner.org,),
                )
        objects = app.state.storage.client.list_objects_v2(Bucket=settings.s3_bucket).get(
            "Contents", []
        )
        for obj in objects:
            app.state.storage.client.delete_object(Bucket=settings.s3_bucket, Key=obj["Key"])
        app.state.storage.client.delete_bucket(Bucket=settings.s3_bucket)
        with Redis.from_url(settings.redis_url) as broker:
            broker.delete(settings.ingestion_queue, f"_kombu.binding.{settings.ingestion_queue}")
            broker.delete(settings.run_queue, f"_kombu.binding.{settings.run_queue}")
            broker.delete(
                f"_kombu.binding_permanent.{settings.run_queue}",
                f"_kombu.binding_permanent.{settings.ingestion_queue}",
            )


def send(
    owner: Actor,
    kb: str,
    body: bytes = b"hello",
    name: str = "notes.txt",
    mime: str = "text/plain",
    key: str | None = None,
) -> Any:
    return owner.request(
        "POST",
        f"/knowledge-bases/{kb}/documents",
        files={"file": (name, body, mime)},
        headers={"Idempotency-Key": key or uuid4().hex},
    )


def test_knowledge_crud_roles_and_pages(setup: tuple[TestClient, Actor, str, Settings]) -> None:
    client, owner, kb, _ = setup
    member = Actor(client).register()
    owner.add(member)
    assert (
        member.request(
            "POST",
            "/knowledge-bases",
            json={"name": "no"},
            headers={"Idempotency-Key": uuid4().hex},
        ).status_code
        == 403
    )
    assert (
        owner.request(
            "POST",
            "/knowledge-bases",
            json={"name": "  研发资料  "},
            headers={"Idempotency-Key": uuid4().hex},
        ).status_code
        == 409
    )
    assert (
        owner.request(
            "POST", "/knowledge-bases", json={"name": " "}, headers={"Idempotency-Key": uuid4().hex}
        ).status_code
        == 422
    )
    changed = owner.request(
        "PATCH",
        f"/knowledge-bases/{kb}",
        json={"name": "技术方案", "description": "新说明", "expectedRevision": 1},
    )
    assert changed.status_code == 200 and changed.json()["data"]["revision"] == 2
    assert (
        owner.request(
            "PATCH", f"/knowledge-bases/{kb}", json={"name": "旧修改", "expectedRevision": 1}
        ).status_code
        == 409
    )
    key = uuid4().hex
    for _ in range(2):
        assert (
            owner.request(
                "POST",
                "/knowledge-bases",
                json={"name": "second"},
                headers={"Idempotency-Key": key},
            ).status_code
            == 201
        )
    page = member.request("GET", "/knowledge-bases?limit=1").json()["data"]
    assert len(page["items"]) == 1 and page["nextCursor"]
    assert (
        len(
            member.request("GET", f"/knowledge-bases?limit=1&cursor={page['nextCursor']}").json()[
                "data"
            ]["items"]
        )
        == 1
    )
    assert member.request("GET", "/knowledge-bases?q=技术").json()["data"]["items"][0]["id"] == kb
    outsider = Actor(client).register()
    outsider.create_org()
    assert outsider.request("GET", f"/knowledge-bases/{kb}").status_code == 404
    assert send(outsider, kb).status_code == 404


@pytest.mark.parametrize(
    ("name", "mime", "body", "status"),
    [
        ("a.txt", "text/plain", "中文文档".encode(), 202),
        ("a.md", "application/octet-stream", b"# Heading\ncontent", 202),
        ("a.pdf", "application/pdf", b"%PDF-1.7\nplaceholder", 202),
        ("a.exe", "application/octet-stream", b"MZbinary", 415),
        ("a.pdf", "text/plain", b"%PDF-1.7", 415),
        ("a.pdf", "application/pdf", b"not pdf", 415),
        ("a.txt", "text/plain", b"", 422),
        ("a.md", "text/markdown", b"   \n", 422),
        ("a.txt", "text/plain", b"hello\x00binary", 415),
        ("a.txt", "text/plain", b"hello\xe4", 422),
    ],
)
def test_file_validation(
    setup: tuple[TestClient, Actor, str, Settings], name: str, mime: str, body: bytes, status: int
) -> None:
    _, owner, kb, _ = setup
    response = send(owner, kb, body, name, mime)
    assert response.status_code == status, response.text
    if status == 202:
        assert response.json()["data"]["status"] == "queued"
        assert response.json()["data"]["chunkCount"] == 0
        assert "objectKey" not in response.text and "credential" not in response.text
    else:
        assert (
            owner.request("GET", f"/knowledge-bases/{kb}/documents").json()["data"]["items"] == []
        )


def test_byte_bounds_idempotency_and_same_names(
    setup: tuple[TestClient, Actor, str, Settings], database_url: str
) -> None:
    client, owner, kb, _ = setup
    limits = owner.request("GET", "/settings/limits").json()["data"]
    assert (
        owner.request(
            "PUT",
            "/settings/limits",
            json={"limits": {**limits["effective"], "maxUploadBytes": 8}, "expectedRevision": 0},
        ).status_code
        == 200
    )
    assert send(owner, kb, b"123456789").status_code == 413
    key = uuid4().hex
    with ThreadPoolExecutor() as pool:
        futures = [pool.submit(send, owner, kb, b"12345678", key=key) for _ in range(2)]
        responses = [f.result(timeout=20) for f in futures]
    assert [r.status_code for r in responses] == [202, 202]
    doc = responses[0].json()["data"]
    assert responses[1].json()["data"]["id"] == doc["id"]
    assert send(owner, kb, b"different", key=key).status_code == 413
    assert send(owner, kb, b"changed", key=key).status_code == 409
    assert send(owner, kb, b"12345678").json()["data"]["id"] != doc["id"]
    accepted = owner.request(
        "GET", f"/idempotency-requests/{key}?operation=document.upload&resourceId={kb}"
    )
    assert accepted.json()["data"]["targetId"] == doc["id"]
    member = Actor(client).register()
    owner.add(member)
    assert (
        member.request(
            "GET", f"/idempotency-requests/{key}?operation=document.upload&resourceId={kb}"
        ).json()["data"]["status"]
        == "absent"
    )
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.documents WHERE organization_id=%s", (owner.org,)
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT count(*) FROM app.outbox_jobs WHERE organization_id=%s", (owner.org,)
        ).fetchone() == (2,)
    assert owner.request(
        "GET", f"/knowledge-bases/{kb}/documents?q=notes&status=queued&limit=1"
    ).json()["data"]["nextCursor"]
    assert send(member, kb).status_code == 403


def test_storage_and_database_failure_no_false_acceptance(
    setup: tuple[TestClient, Actor, str, Settings], database_url: str
) -> None:
    client, owner, kb, settings = setup
    with patch.object(
        application(client).state.storage,
        "put",
        AsyncMock(side_effect=AppError(503, "STORAGE_UNAVAILABLE", "unavailable")),
    ):
        assert send(owner, kb).status_code == 503
    key = uuid4().hex
    with patch.object(
        KnowledgeService, "audit", side_effect=AppError(503, "DB_TEST_FAILURE", "unavailable")
    ):
        assert send(owner, kb, key=key).status_code == 503
    assert (
        owner.request(
            "GET", f"/idempotency-requests/{key}?operation=document.upload&resourceId={kb}"
        ).json()["data"]["status"]
        == "absent"
    )
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.documents WHERE organization_id=%s", (owner.org,)
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM app.outbox_jobs WHERE organization_id=%s", (owner.org,)
        ).fetchone() == (0,)
    assert (
        application(client).state.storage.client.list_objects_v2(Bucket=settings.s3_bucket)[
            "KeyCount"
        ]
        == 1
    )
    worker = Outbox(application(client).state.engine, settings, application(client).state.storage)
    assert client.portal is not None
    with patch("backend.workers.outbox.now", return_value=now() + timedelta(hours=25)):
        assert client.portal.call(worker.clean_orphans)["deleted"] == 1
    assert send(owner, kb, key=key).status_code == 202


def test_original_delete_outbox_and_model_lock(
    setup: tuple[TestClient, Actor, str, Settings], database_url: str
) -> None:
    client, owner, kb, settings = setup
    key = uuid4().hex
    doc = send(owner, kb, key=key).json()["data"]
    blocked = owner.request(
        "PUT",
        "/settings/models/embedding",
        json={**candidate("embedding", 1), "modelName": "new-model"},
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "EMBEDDING_LOCKED"
    raw = client.get(
        f"/api/v1/documents/{doc['id']}/content?view=original", headers=owner.headers()
    )
    assert raw.status_code == 200 and raw.content == b"hello"
    assert raw.headers["content-disposition"].startswith("attachment;")
    assert owner.request("GET", f"/documents/{doc['id']}/content?view=text").status_code == 409
    worker = Outbox(application(client).state.engine, settings, application(client).state.storage)
    assert client.portal is not None
    assert client.portal.call(worker.dispatch)["published"] >= 1
    with Redis.from_url(settings.redis_url) as broker:
        assert broker.llen(settings.ingestion_queue) >= 1
    assert owner.request("DELETE", f"/documents/{doc['id']}").status_code == 204
    tombstone = owner.request(
        "GET", f"/idempotency-requests/{key}?operation=document.upload&resourceId={kb}"
    ).json()["data"]
    assert tombstone["status"] == "accepted" and tombstone["document"]["deletedAt"]
    assert tombstone["document"]["status"] == "queued"
    assert owner.request("GET", f"/documents/{doc['id']}").status_code == 404
    assert (
        client.get(f"/api/v1/documents/{doc['id']}/content", headers=owner.headers()).status_code
        == 404
    )
    assert client.portal.call(worker.dispatch)["published"] >= 1
    with connection(database_url) as db:
        assert db.execute(
            "SELECT cleanup_status FROM app.documents WHERE id=%s", (doc["id"],)
        ).fetchone() == ("done",)
    assert (
        application(client).state.storage.client.list_objects_v2(Bucket=settings.s3_bucket)[
            "KeyCount"
        ]
        == 0
    )
    assert owner.request("DELETE", f"/knowledge-bases/{kb}").status_code == 204


def test_permissions_rechecked_after_s3_write(
    setup: tuple[TestClient, Actor, str, Settings],
) -> None:
    client, owner, kb, _ = setup
    admin = Actor(client).register()
    membership = owner.add(admin)
    owner.request(
        "PATCH",
        f"/members/{admin.id}",
        json={"baseRole": "admin", "expectedRevision": membership["revision"]},
    )
    assert client.portal is not None
    started, release = client.portal.call(asyncio.Event), client.portal.call(asyncio.Event)
    original = application(client).state.storage.put

    async def paused(*args: Any) -> None:
        await original(*args)
        started.set()
        await release.wait()

    with (
        patch.object(application(client).state.storage, "put", paused),
        ThreadPoolExecutor() as pool,
    ):
        future = pool.submit(send, admin, kb)
        client.portal.call(started.wait)
        assert (
            owner.request(
                "PATCH", f"/members/{admin.id}", json={"baseRole": "member", "expectedRevision": 2}
            ).status_code
            == 200
        )
        client.portal.call(release.set)
        assert future.result(timeout=10).status_code == 403
    assert owner.request("GET", f"/knowledge-bases/{kb}/documents").json()["data"]["items"] == []


@pytest.mark.parametrize("mode", ["exact", "oversize", "truncated", "multiple"])
async def test_streamed_bytes_and_multipart_completion(mode: str) -> None:
    # No Content-Length is supplied; split both UTF-8 and multipart headers across chunks.
    header = (
        b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
    )
    payload = "知识".encode()
    body = header + payload + (b"x" if mode == "oversize" else b"")
    if mode == "multiple":
        body += b"\r\n" + header + payload
    if mode != "truncated":
        body += b"\r\n--boundary--\r\n"

    async def stream() -> Any:
        for offset in range(0, len(body), 2):
            yield body[offset : offset + 2]

    upload = Upload(len(payload))
    try:
        if mode == "exact":
            await upload.read(stream(), "multipart/form-data; boundary=boundary")
            assert upload.body.read() == payload
        else:
            with pytest.raises(AppError) as failure:
                await upload.read(stream(), "multipart/form-data; boundary=boundary")
            assert failure.value.status == (413 if mode == "oversize" else 422)
    finally:
        upload.body.close()


def test_outbox_failure_recovery_and_duplicate_delivery(
    setup: tuple[TestClient, Actor, str, Settings], database_url: str
) -> None:
    client, owner, kb, settings = setup
    doc = send(owner, kb).json()["data"]
    app = application(client)
    worker = Outbox(app.state.engine, settings, app.state.storage)
    assert client.portal is not None
    try:
        with patch.object(worker.broker, "send_task", side_effect=TimeoutError):
            assert client.portal.call(worker.dispatch) == {"published": 0, "failed": 1}
        with connection(database_url) as db:
            job = db.execute(
                "SELECT id,published_at,last_error FROM app.outbox_jobs WHERE resource_id=%s",
                (doc["id"],),
            ).fetchone()
            assert job and job[1:] == (None, "OUTBOX_DELIVERY_FAILED")
            db.execute("UPDATE app.outbox_jobs SET available_at=now() WHERE id=%s", (job[0],))
        assert client.portal.call(worker.dispatch) == {"published": 1, "failed": 0}
        # Simulate publication succeeded but its DB acknowledgement was lost.
        with connection(database_url) as db:
            db.execute("UPDATE app.outbox_jobs SET published_at=NULL WHERE id=%s", (job[0],))
        assert client.portal.call(worker.dispatch) == {"published": 1, "failed": 0}
        import json

        with Redis.from_url(settings.redis_url) as broker:
            messages = [
                json.loads(value) for value in broker.lrange(settings.ingestion_queue, 0, -1)
            ]
        assert len(messages) == 2
        assert {message["headers"]["id"] for message in messages} == {str(job[0])}
        assert (
            owner.request("GET", f"/documents/{doc['id']}").json()["data"]["document"]["status"]
            == "queued"
        )
    finally:
        worker.broker.close()


def test_limits_rechecked_after_storage_and_capacity(
    setup: tuple[TestClient, Actor, str, Settings],
) -> None:
    client, owner, kb, settings = setup
    app = application(client)
    assert client.portal is not None
    started, release = client.portal.call(asyncio.Event), client.portal.call(asyncio.Event)
    original = app.state.storage.put

    async def paused(*args: Any) -> None:
        await original(*args)
        started.set()
        await release.wait()

    with patch.object(app.state.storage, "put", paused), ThreadPoolExecutor() as pool:
        future = pool.submit(send, owner, kb, b"12345")
        client.portal.call(started.wait)
        limits = owner.request("GET", "/settings/limits").json()["data"]
        assert (
            owner.request(
                "PUT",
                "/settings/limits",
                json={
                    "limits": {**limits["effective"], "maxUploadBytes": 4},
                    "expectedRevision": 0,
                },
            ).status_code
            == 200
        )
        client.portal.call(release.set)
        assert future.result(timeout=10).status_code == 413
    assert owner.request("GET", f"/knowledge-bases/{kb}/documents").json()["data"]["items"] == []
    settings.max_documents_per_organization = 1
    assert send(owner, kb, b"1234").status_code == 202
    assert send(owner, kb, b"1234").json()["error"]["code"] == "DOCUMENT_LIMIT_REACHED"
