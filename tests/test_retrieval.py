"""D06 authorization/query-plan integration on real PG/S3; provider wire controlled."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx
import httpx2
import pytest
from model_fixture import ProviderFixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_ingestion import Setup, process
from test_knowledge import application, send
from test_knowledge import setup as setup
from test_model_settings import candidate

from backend.models.knowledge import Document, DocumentAttempt
from backend.rag.documents import lexemes
from backend.repositories.retrieval import HYBRID
from backend.services.model_probe import ModelProbe

pytestmark = pytest.mark.integration


class RetrievalProvider(ProviderFixture):
    def __init__(self) -> None:
        super().__init__()
        self.rerank_mode = "ok"
        self.rank_started: asyncio.Event | None = None
        self.rank_release: asyncio.Event | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("rerank"):
            return await super().handle(request)
        value = json.loads(request.content)
        self.requests.append(value)
        if self.rank_started and self.rank_release:
            self.rank_started.set()
            await self.rank_release.wait()
        dashscope = "input" in value
        source = value["input"] if dashscope else value
        query, docs = source["query"], source["documents"]
        top = value["parameters"]["top_n"] if dashscope else value["top_n"]
        terms = set(lexemes(query).split())
        results = sorted(
            [
                {
                    "index": i,
                    "relevance_score": len(terms & set(lexemes(doc).split())) / max(1, len(terms)),
                }
                for i, doc in enumerate(docs)
            ],
            key=lambda row: (-row["relevance_score"], row["index"]),
        )[:top]
        if self.rerank_mode == "auth":
            return httpx.Response(401, json={"secret": "do-not-return"})
        if self.rerank_mode == "timeout":
            raise httpx.ReadTimeout("private provider error")
        if self.rerank_mode == "index":
            results[0]["index"] = len(docs)
        if self.rerank_mode == "bool":
            results[0]["index"] = True
        if self.rerank_mode == "duplicate":
            results[1]["index"] = results[0]["index"]
        if self.rerank_mode == "score":
            results[0]["relevance_score"] = 1.1
        if self.rerank_mode == "order":
            results[0]["relevance_score"], results[1]["relevance_score"] = 0.1, 0.9
        if self.rerank_mode == "missing":
            results = []
        if self.rerank_mode == "nan":
            return httpx.Response(200, content='{"results":[{"index":0,"relevance_score":NaN}]}')
        if self.rerank_mode == "usage":
            return httpx.Response(
                200, json={"results": results, "meta": {"billed_units": {"search_units": -1}}}
            )
        return httpx.Response(
            200,
            json=(
                {"output": {"results": results}, "usage": {"total_tokens": 19}}
                if dashscope
                else {"results": results, "meta": {"billed_units": {"search_units": 1}}}
            ),
        )


def ready(setup: Setup, body: str, name: str = "研发.txt") -> dict[str, Any]:
    _, owner, kb, _ = setup
    response = send(owner, kb, body.encode(), name)
    assert response.status_code == 202, response.text
    doc: dict[str, Any] = response.json()["data"]
    process(setup, doc)
    assert (
        owner.request("GET", f"/documents/{doc['id']}").json()["data"]["document"]["status"]
        == "ready"
    )
    return doc


def configure(setup: Setup) -> RetrievalProvider:
    client, owner, _, settings = setup
    assert (
        owner.request("PUT", "/settings/models/rerank", json=candidate("rerank")).status_code == 200
    )
    provider = RetrievalProvider()
    application(client).state.model_probe = ModelProbe(
        settings, httpx.MockTransport(provider.handle)
    )
    return provider


def search(
    owner: Actor, kb: str, query: str = "发布回滚", docs: list[str] | None = None
) -> httpx2.Response:
    return owner.request(
        "POST",
        f"/knowledge-bases/{kb}/search-preview",
        json={"query": query, "documentIds": docs or []},
    )


def test_hybrid_scope_metering_and_plan(setup: Setup, database_url: str) -> None:
    client, owner, kb, settings = setup
    provider = configure(setup)
    target = ready(
        setup, "发布回滚：失败后回滚至上一版本。\nDeployment rollback restores previous release."
    )
    unrelated = ready(setup, "午餐菜单：烤鱼和沙拉。")
    pending = send(owner, kb, b"unready secret").json()["data"]
    deleted = ready(setup, "已删除的私密内容")
    assert owner.request("DELETE", f"/documents/{deleted['id']}").status_code == 204
    other_kb = owner.request(
        "POST",
        "/knowledge-bases",
        json={"name": "另一个库"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["id"]
    other = ready((client, owner, other_kb, settings), "跨库禁止内容")
    stranger = Actor(client).register()
    stranger.create_org()
    config = candidate("embedding")
    config["embeddingDimension"] = 4
    assert stranger.request("PUT", "/settings/models/embedding", json=config).status_code == 200
    foreign_kb = stranger.request(
        "POST",
        "/knowledge-bases",
        json={"name": "异维度组织"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["id"]
    foreign = ready((client, stranger, foreign_kb, settings), "跨组织绝密发布回滚")
    provider.requests.clear()
    for query in ("发布回滚", "Deployment rollback"):
        response = search(owner, kb, query)
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["items"][0]["documentId"] == target["id"]
        assert set(result["searchedDocumentIds"]) == {target["id"], unrelated["id"]}
        assert result["coverage"]["keywordCount"] > 0
        assert result["coverage"]["vectorCount"] > 0
        assert result["items"][0]["attemptId"] == target["currentAttemptId"]
        assert len({r["chunkId"] for r in result["items"]}) == len(result["items"])
        assert len(result["usage"]) == 2
        assert result["usage"][1]["billedUnits"] == {"search_units": 1}
    outgoing = json.dumps(provider.requests, ensure_ascii=False)
    for forbidden in ("跨组织", "跨库", "私密", "unready secret"):
        assert forbidden not in outgoing
    for forbidden_document in (foreign, other, deleted):
        assert search(owner, kb, docs=[forbidden_document["id"]]).status_code == 404
    assert search(owner, kb, docs=[pending["id"]]).status_code == 409
    assert search(owner, foreign_kb).status_code == 404
    assert search(owner, kb, docs=[target["id"]]).json()["data"]["searchedDocumentIds"] == [
        target["id"]
    ]
    with connection(database_url) as db:
        rows = db.execute(
            "SELECT run_id,step_id,status,operation_kind FROM app.usage_entries "
            "WHERE operation_id=%s",
            (result["operationId"],),
        ).fetchall()
        assert rows == [(None, None, "succeeded", "search_preview")] * 2
        profile_row = db.execute(
            "SELECT id,dimension FROM app.model_profiles "
            "WHERE organization_id=%s AND dimension IS NOT NULL",
            (owner.org,),
        ).fetchone()
        assert profile_row is not None
        profile, dimension = profile_row
        assert db.execute(
            "SELECT (SELECT count(*) FROM app.runs),(SELECT count(*) FROM app.conversations)"
        ).fetchone() == (0, 0)

    async def explain() -> Any:
        async with async_sessionmaker(application(client).state.engine)() as db:
            return await db.scalar(
                text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + HYBRID),
                {
                    "org": UUID(owner.org),
                    "kb": UUID(kb),
                    "docs": [UUID(target["id"]), UUID(unrelated["id"])],
                    "profile": profile,
                    "dimension": dimension,
                    "vector": "[0.1,0.1,0.1]",
                    "terms": "回滚",
                },
            )

    assert client.portal
    plan = client.portal.call(explain)
    rendered = json.dumps(plan)
    assert '"CTE Name": "authorized"' in rendered
    assert "CTE authorized" in rendered
    print(
        "D06 EXPLAIN: materialized authorized CTE; actual rows=",
        plan[0]["Plan"]["Actual Rows"],
        "execution_ms=",
        plan[0]["Execution Time"],
    )


@pytest.mark.parametrize(
    "mode",
    ["index", "bool", "duplicate", "score", "order", "missing", "nan", "usage", "auth", "timeout"],
)
def test_rerank_failure_is_not_empty_success(setup: Setup, database_url: str, mode: str) -> None:
    _, owner, kb, _ = setup
    provider = configure(setup)
    ready(setup, "发布回滚")
    ready(setup, "unrelated text")
    provider.rerank_mode = mode
    response = search(owner, kb)
    assert response.status_code == 503, response.text
    assert "items" not in response.text and "private" not in response.text
    with connection(database_url) as db:
        assert db.execute(
            "SELECT status FROM app.usage_entries WHERE organization_id=%s "
            "AND operation_kind='search_preview' ORDER BY created_at",
            (owner.org,),
        ).fetchall() == [("succeeded",), ("failed",)]


def test_empty_roles_and_validation(setup: Setup) -> None:
    client, owner, kb, _ = setup
    provider = configure(setup)
    provider.requests.clear()
    empty = search(owner, kb)
    assert empty.status_code == 200
    assert empty.json()["data"]["items"] == [] and provider.requests == []
    member = Actor(client).register()
    owner.add(member)
    assert search(member, kb).status_code == 403
    for query in (" ", "a" * 4001, "中" * 1400):
        assert search(owner, kb, query).status_code == 422
    doc = str(uuid4())
    assert search(owner, kb, docs=[doc, doc]).status_code == 422


@pytest.mark.parametrize("mutation", ["delete", "remove", "downgrade", "model"])
def test_revocation_during_rerank(setup: Setup, database_url: str, mutation: str) -> None:
    client, owner, kb, _ = setup
    provider = configure(setup)
    doc = ready(setup, "发布回滚验证")
    admin = Actor(client).register()
    owner.add(admin)
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.org_memberships SET base_role='admin' "
            "WHERE organization_id=%s AND user_id=%s",
            (owner.org, admin.id),
        )
    provider.rank_started, provider.rank_release = asyncio.Event(), asyncio.Event()
    assert client.portal
    with ThreadPoolExecutor() as pool:
        future = pool.submit(search, admin, kb)
        client.portal.call(asyncio.wait_for, provider.rank_started.wait(), 10)
        if mutation == "delete":
            assert owner.request("DELETE", f"/documents/{doc['id']}").status_code == 204
        elif mutation == "remove":
            assert owner.request("DELETE", f"/members/{admin.id}").status_code == 204
        elif mutation == "downgrade":
            with connection(database_url) as db:
                db.execute(
                    "UPDATE app.org_memberships SET base_role='member' "
                    "WHERE organization_id=%s AND user_id=%s",
                    (owner.org, admin.id),
                )
        else:
            with connection(database_url) as db:
                db.execute(
                    "UPDATE app.model_connections SET enabled=false "
                    "WHERE organization_id=%s AND kind='rerank'",
                    (owner.org,),
                )
        client.portal.call(provider.rank_release.set)
        response = future.result(10)
    assert response.status_code in {403, 404, 409}, response.text
    assert "验证" not in response.text and "items" not in response.text


def test_bounds_and_empty_keywords(setup: Setup) -> None:
    _, owner, kb, _ = setup
    provider = configure(setup)
    ready(setup, ("发布回滚细节和验证步骤。" * 30 + "\n") * 5)
    response = search(owner, kb, "发布回滚")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    coverage = result["coverage"]
    assert coverage["candidateCount"] == 12 and coverage["returnedCount"] == 8
    assert coverage["partial"] and coverage["excerptTruncatedCount"] > 0
    assert coverage["omittedChunkCount"] > 0
    assert coverage["evidenceBytes"] <= 4000
    assert all(len(s.encode()) <= 160 for s in provider.requests[-1]["documents"])
    assert search(owner, kb, "!!!").status_code == 200
    long_query = " ".join(f"word{i}" for i in range(70))
    assert search(owner, kb, long_query).json()["data"]["coverage"]["queryTermsTruncated"]


def test_stale_attempt_and_failed_publication_do_not_retrieve(setup: Setup) -> None:
    client, owner, kb, _ = setup
    provider = configure(setup)
    good = ready(setup, "发布回滚正常片段")
    old = ready(setup, "旧版本禁止片段")
    failed = ready(setup, "失败发布禁止片段")

    async def change() -> None:
        async with async_sessionmaker(application(client).state.engine).begin() as db:
            previous = await db.get(DocumentAttempt, UUID(old["currentAttemptId"]))
            assert previous
            replacement = DocumentAttempt(
                organization_id=previous.organization_id,
                document_id=previous.document_id,
                attempt_no=2,
                pipeline_version=previous.pipeline_version,
                embedding_connection_id=previous.embedding_connection_id,
                embedding_profile_id=previous.embedding_profile_id,
                model_connection_version_id=previous.model_connection_version_id,
                limits_snapshot=previous.limits_snapshot,
                status="ready",
            )
            db.add(replacement)
            await db.flush()
            doc = await db.get(Document, UUID(old["id"]))
            assert doc
            doc.current_attempt_id = replacement.id
            broken = await db.get(DocumentAttempt, UUID(failed["currentAttemptId"]))
            assert broken
            broken.status = "failed"

    assert client.portal
    client.portal.call(change)
    provider.requests.clear()
    result = search(owner, kb)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["searchedDocumentIds"] == [good["id"]]
    outgoing = json.dumps(provider.requests, ensure_ascii=False)
    assert "旧版本" not in outgoing and "失败发布" not in outgoing
