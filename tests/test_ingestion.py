"""D05 transaction and authorization checks on real PG/S3; model wire is controlled."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from model_fixture import ProviderFixture
from test_document_processing import pdf
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_knowledge import application, send
from test_knowledge import setup as setup

from backend.config import Settings
from backend.rag.documents import IngestError, extract, split
from backend.rag.embeddings import Embeddings
from backend.services.ingestion import Ingestion
from backend.workers.outbox import Outbox

pytestmark = pytest.mark.integration
Setup = tuple[TestClient, Actor, str, Settings]


def worker(setup: Setup, fixture: ProviderFixture | None = None) -> Ingestion:
    client, _, _, settings = setup
    app = application(client)
    return Ingestion(
        app.state.engine,
        settings,
        app.state.storage,
        Embeddings(settings, httpx.MockTransport((fixture or ProviderFixture()).handle)),
    )


def process(setup: Setup, doc: dict[str, Any], service: Ingestion | None = None) -> None:
    client, owner, _, _ = setup
    assert client.portal
    client.portal.call(
        (service or worker(setup)).run,
        UUID(owner.org),
        UUID(doc["id"]),
        UUID(doc["currentAttemptId"]),
    )


def current(owner: Actor, doc: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = owner.request("GET", f"/documents/{doc['id']}").json()["data"]
    return result["document"]  # type: ignore[no-any-return]


def test_publication_preview_scope_and_cleanup(setup: Setup, database_url: str) -> None:
    client, owner, kb, settings = setup
    body = ("# 开发记录\n\n" + "接口与数据库，review release。" * 300).encode()
    doc = send(owner, kb, body, "notes.md", "text/markdown").json()["data"]
    path = f"/documents/{doc['id']}"
    assert owner.request("GET", path + "/content?view=text").status_code == 409
    process(setup, doc)
    ready = current(owner, doc)
    assert ready["status"] == "ready" and ready["chunkCount"] > 20
    page = owner.request("GET", path + "/chunks").json()["data"]
    assert len(page["items"]) == 20 and page["nextCursor"]
    second = owner.request("GET", path + "/chunks?cursor=" + page["nextCursor"]).json()["data"]
    assert second["items"][0]["ordinal"] == 20
    member = Actor(client).register()
    owner.add(member)
    assert member.request("GET", path + "/chunks").status_code == 403
    text = member.request("GET", path + "/content?view=text").json()["data"]
    assert len(text["text"]) == 4000 and text["nextCursor"]
    assert (
        member.request("GET", path + "/content?view=text&attemptId=" + str(uuid4())).status_code
        == 409
    )
    outsider = Actor(client).register()
    outsider.create_org()
    assert outsider.request("GET", path + "/content?view=text").status_code == 404
    with connection(database_url) as db:
        row = db.execute(
            "SELECT count(*),min(public.vector_dims(embedding)),bool_and(text IS NOT NULL) "
            "FROM app.document_chunks WHERE document_id=%s",
            (doc["id"],),
        ).fetchone()
        assert row == (ready["chunkCount"], 3, True)
        assert db.execute(
            "SELECT bool_and(status='succeeded' AND token_source='actual') "
            "FROM app.usage_entries WHERE operation_id=%s",
            (doc["currentAttemptId"],),
        ).fetchone() == (True,)
    assert owner.request("DELETE", path).status_code == 204
    assert member.request("GET", path + "/content?view=text").status_code == 404
    outbox = Outbox(application(client).state.engine, settings, application(client).state.storage)
    try:
        assert client.portal
        client.portal.call(outbox.dispatch)
    finally:
        outbox.broker.close()
    with connection(database_url) as db:
        assert db.execute(
            "SELECT bool_and(text IS NULL AND embedding IS NULL) "
            "FROM app.document_chunks WHERE document_id=%s",
            (doc["id"],),
        ).fetchone() == (True,)
    objects = application(client).state.storage.client.list_objects_v2(Bucket=settings.s3_bucket)
    assert not objects.get("Contents")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"%PDF-1.7\nbroken", "PDF_INVALID"),
        (pdf(""), "DOCUMENT_NO_TEXT"),
        (pdf("locked", encrypted=True), "PDF_ENCRYPTED"),
    ],
)
def test_failed_pdf_never_ready(setup: Setup, body: bytes, code: str, database_url: str) -> None:
    _, owner, kb, _ = setup
    doc = send(owner, kb, body, "bad.pdf", "application/pdf").json()["data"]
    process(setup, doc)
    row = current(owner, doc)
    assert row["status"] == "failed" and row["errorCode"] == code and row["chunkCount"] == 0
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.document_chunks WHERE document_id=%s", (doc["id"],)
        ).fetchone() == (0,)


def test_failed_batch_retry_is_idempotent_and_preserves_history(
    setup: Setup, database_url: str
) -> None:
    _, owner, kb, _ = setup
    doc = send(owner, kb, ("内容" * 2000).encode()).json()["data"]
    provider = ProviderFixture()
    provider.mode = "dimension"
    process(setup, doc, worker(setup, provider))
    assert current(owner, doc)["errorCode"] == "EMBEDDING_DIMENSION_INVALID"
    path = f"/documents/{doc['id']}/retry"
    key = uuid4().hex

    def retry() -> Any:
        return owner.request(
            "POST",
            path,
            json={"expectedAttemptId": doc["currentAttemptId"]},
            headers={"Idempotency-Key": key},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: retry(), range(2)))
    assert all(r.status_code == 202 for r in responses)
    second = responses[0].json()["data"]
    assert second["currentAttemptId"] != doc["currentAttemptId"] and second["attemptNumber"] == 2
    assert responses[1].json()["data"] == second
    process(setup, doc)  # stale queue delivery cannot claim the new attempt
    assert current(owner, second)["status"] == "queued"
    process(setup, second)
    assert current(owner, second)["status"] == "ready"
    detail = owner.request("GET", f"/documents/{doc['id']}").json()["data"]
    assert {a["status"] for a in detail["attempts"]} == {"ready", "failed"}
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.document_chunks WHERE attempt_id=%s",
            (doc["currentAttemptId"],),
        ).fetchone() == (0,)


def test_expired_lease_cannot_publish_and_can_retry(setup: Setup, database_url: str) -> None:
    client, owner, kb, _ = setup
    service = worker(setup)
    doc = send(owner, kb).json()["data"]
    assert client.portal
    lease = client.portal.call(
        service.claim, UUID(owner.org), UUID(doc["id"]), UUID(doc["currentAttemptId"])
    )
    assert lease
    other = send(owner, kb).json()["data"]
    assert (
        client.portal.call(
            service.claim, UUID(owner.org), UUID(other["id"]), UUID(other["currentAttemptId"])
        )
        is None
    )
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.document_attempts SET lease_until=now()-interval '1 second' WHERE id=%s",
            (doc["currentAttemptId"],),
        )
    assert client.portal.call(service.reconcile) >= 1
    assert current(owner, doc)["errorCode"] == "INGEST_INTERRUPTED"
    with pytest.raises(IngestError, match="INGEST_LEASE_LOST"):
        client.portal.call(
            service.publish,
            lease,
            split(extract(b"hello", "text/plain")),
            [[0.1, 0.1, 0.1]],
            "unused",
            5,
            "unused",
        )


def test_delete_processing_fences_publication(setup: Setup) -> None:
    client, owner, kb, _ = setup
    doc = send(owner, kb).json()["data"]
    service = worker(setup)
    assert client.portal
    lease = client.portal.call(
        service.claim, UUID(owner.org), UUID(doc["id"]), UUID(doc["currentAttemptId"])
    )
    assert lease
    assert owner.request("DELETE", f"/documents/{doc['id']}").status_code == 204
    with pytest.raises(IngestError, match="INGEST_LEASE_LOST"):
        client.portal.call(
            service.publish,
            lease,
            split(extract(b"hello", "text/plain")),
            [[0.1, 0.1, 0.1]],
            "unused",
            5,
            "unused",
        )
    client.portal.call(service.fail, lease, "INGEST_FAILED")
    assert owner.request("GET", f"/documents/{doc['id']}").status_code == 404


def test_later_batch_failure_never_publishes_partial_chunks(
    setup: Setup, database_url: str
) -> None:
    client, owner, kb, settings = setup
    fixture = ProviderFixture()

    async def handle(request: httpx.Request) -> httpx.Response:
        if fixture.requests:
            return httpx.Response(401, json={"error": "fixture-only"})
        return await fixture.handle(request)

    app = application(client)
    service = Ingestion(
        app.state.engine,
        settings,
        app.state.storage,
        Embeddings(settings, httpx.MockTransport(handle)),
    )
    doc = send(owner, kb, ("资料" * 2000).encode()).json()["data"]
    process(setup, doc, service)
    assert current(owner, doc)["status"] == "failed"
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.document_chunks WHERE document_id=%s", (doc["id"],)
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT status FROM app.usage_entries WHERE operation_id=%s ORDER BY attempt_no",
            (doc["currentAttemptId"],),
        ).fetchall() == [("succeeded",), ("failed",)]


def test_database_rejects_mixed_dimension_and_cross_org_chunks(
    setup: Setup, database_url: str
) -> None:
    client, owner, kb, _ = setup
    doc = send(owner, kb).json()["data"]
    process(setup, doc)
    outsider = Actor(client).register()
    outsider.create_org()
    for field, value in [("embedding_dimensions", 4), ("organization_id", outsider.org)]:
        with pytest.raises(psycopg.IntegrityError), connection(database_url) as db:
            db.execute(
                psycopg.sql.SQL("UPDATE app.document_chunks SET {}=%s WHERE document_id=%s").format(
                    psycopg.sql.Identifier(field)
                ),
                (value, doc["id"]),
            )
