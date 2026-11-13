"""T2 acceptance on disposable PostgreSQL/S3 with bounded provider wire fixtures."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError
from test_agents import Setup, configured, create
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_ingestion import process
from test_knowledge import send
from test_knowledge import setup as setup
from test_runs import QAProvider, execute, snapshot

from backend.db import migration_config

CASES = {
    c["status"]: c
    for c in json.loads((Path(__file__).parent / "fixtures/compare.json").read_text("utf-8"))
}

pytestmark = pytest.mark.integration


class CompareProvider(QAProvider):
    def __init__(self, status: str = "covered", fake: bool = False):
        super().__init__()
        self.status, self.fake = status, fake

    async def handle(self, request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content)
        if not request.url.path.endswith("chat/completions") or not value.get("stream"):
            return await super().handle(request)
        self.chat_calls += 1
        self.requests.append(value)
        evidence = json.loads(
            next(
                m["content"]
                for m in value["messages"]
                if m["role"] == "user" and '"evidence"' in m["content"]
            )
        )["evidence"]
        requirement = [e["sourceId"] for e in evidence if e["role"] == "requirement"]
        solution = [e["sourceId"] for e in evidence if e["role"] == "solution"]
        block = {
            "type": "comparison",
            "text": "逐项比较",
            "kind": "fact",
            "sourceIds": [],
            "item": {
                "requirement": CASES[self.status]["requirement"],
                "requirementSourceIds": [str(uuid4())] if self.fake else requirement,
                "solutionEvidence": CASES[self.status]["evidence"],
                "solutionSourceIds": [] if self.status == "not_found" else solution,
                "status": self.status,
                "impact": "需人工核对适用范围",
                "suggestion": "建议补充验收证据",
            },
        }
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": json.dumps(block, ensure_ascii=False) + "\n"},
                        "finish_reason": "stop",
                    }
                ]
            },
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 100}},
        ]
        return httpx.Response(
            200,
            content="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n",
        )


def prepare(setup: Setup, status: str = "covered") -> tuple[str, dict[str, Any], dict[str, str]]:
    _, owner, kb, _ = setup
    config = configured(setup)
    documents = []
    for name, text in [
        ("requirements.txt", CASES[status]["requirement"]),
        ("solution.txt", CASES[status]["solution"]),
    ]:
        doc = send(owner, kb, text.encode(), name).json()["data"]
        process(setup, doc)
        documents.append(doc["id"])
    config.update(taskType="compare", name="需求对比")
    agent = create(owner, **config)
    response = owner.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": "compare"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return (
        response.json()["data"]["id"],
        agent,
        {"requirementDocumentId": documents[0], "solutionDocumentId": documents[1]},
    )


def submit(
    setup: Setup, cid: str, agent: dict[str, Any], value: dict[str, Any], key: str | None = None
) -> Any:
    return setup[1].request(
        "POST",
        f"/conversations/{cid}/runs",
        json={"agentId": agent["id"], "input": value},
        headers={"Idempotency-Key": key or uuid4().hex},
    )


@pytest.mark.parametrize("status", ["covered", "partial", "not_found", "conflict"])
def test_compare_dual_evidence_and_export(setup: Setup, database_url: str, status: str) -> None:
    cid, agent, value = prepare(setup, status)
    key = uuid4().hex
    accepted = submit(setup, cid, agent, value, key)
    assert accepted.status_code == 202, accepted.text
    rid = accepted.json()["data"]["runId"]
    assert submit(setup, cid, agent, value, key).json()["data"]["runId"] == rid
    provider = CompareProvider(status)
    execute(setup, rid, provider)
    result = snapshot(setup, rid)
    assert result["status"] == "succeeded", result
    assert result["taskType"] == "compare"
    item = result["result"]["items"][0]
    assert item["status"] == status
    assert bool(item["solutionSourceIds"]) == (status != "not_found")
    for role, field in [("requirement", "requirementSourceIds"), ("solution", "solutionSourceIds")]:
        for sid in item[field]:
            source = setup[1].request("GET", f"/runs/{rid}/sources/{sid}").json()["data"]
            assert source["documentId"] == value[role + "DocumentId"]
            assert source["locator"]["comparisonRole"] == role
    exported = setup[1].request("GET", f"/runs/{rid}/export").text
    assert "逐项对比" in exported and status in exported and "未检索到不代表全文没有" in exported
    assert len(result["coverage"]["selectedDocuments"]) == 2
    assert result["coverage"]["truncated"] is True
    execute(setup, rid, provider)
    assert provider.chat_calls == 1
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.usage_entries WHERE run_id=%s", (UUID(rid),)
        ).fetchone() == (5,)
        assert db.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s AND type='run.completed'",
            (UUID(rid),),
        ).fetchone() == (1,)


def test_compare_rejects_invalid_scope_and_forged_citations(setup: Setup) -> None:
    cid, agent, value = prepare(setup)
    assert (
        submit(
            setup, cid, agent, {**value, "solutionDocumentId": value["requirementDocumentId"]}
        ).status_code
        == 422
    )
    assert (
        submit(setup, cid, agent, {**value, "solutionDocumentId": str(uuid4())}).status_code == 409
    )
    assert (
        submit(setup, cid, agent, {"question": "qa", "knowledgeBaseIds": [setup[2]]}).status_code
        == 422
    )
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    execute(setup, rid, CompareProvider(fake=True))
    assert snapshot(setup, rid)["errorCode"] == "CITATION_INVALID"
    outsider = Actor(setup[0]).register()
    outsider.create_org()
    assert outsider.request("GET", f"/runs/{rid}").status_code == 404


def test_compare_selected_source_deleted_before_execution(setup: Setup) -> None:
    cid, agent, value = prepare(setup)
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    assert (
        setup[1].request("DELETE", "/documents/" + value["solutionDocumentId"]).status_code == 204
    )
    provider = CompareProvider()
    execute(setup, rid, provider)
    assert snapshot(setup, rid)["status"] == "failed"
    assert provider.chat_calls == 0


def test_compare_long_document_coverage_and_budget(setup: Setup, database_url: str) -> None:
    cid, agent, value = prepare(setup)
    doc = send(
        setup[1],
        setup[2],
        ("# 长需求\n\n" + "\n\n".join(f"发布要求{i}：评审和测试。" for i in range(240))).encode(),
        "long-requirements.txt",
    ).json()["data"]
    process(setup, doc)
    value["requirementDocumentId"] = doc["id"]
    value["focus"] = "x" * 1600
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    execute(setup, rid, CompareProvider())
    result = snapshot(setup, rid)
    assert result["status"] == "succeeded", result
    coverage = result["coverage"]
    assert (
        coverage["sides"]["requirement"]["eligibleChunkCount"]
        > coverage["sides"]["requirement"]["includedChunkCount"]
    )
    assert coverage["truncated"] and coverage["limitations"]
    assert any("1500" in text for text in coverage["limitations"])
    assert coverage["sides"]["solution"]["includedChunkCount"] > 0
    exported = setup[1].request("GET", f"/runs/{rid}/export").text
    assert "omittedContextCount" in exported and "eligibleChunkCount" in exported
    rid2 = submit(setup, cid, agent, value).json()["data"]["runId"]
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.runs SET budget_snapshot=jsonb_set(budget_snapshot,"
            "'{max_tool_attempts}','1') WHERE id=%s",
            (UUID(rid2),),
        )
    provider = CompareProvider()
    execute(setup, rid2, provider)
    assert snapshot(setup, rid2)["status"] == "failed"
    assert provider.chat_calls == 0


def test_compare_downgrade_refuses_to_discard_conversations(
    setup: Setup, database_url: str
) -> None:
    cid, _, _ = prepare(setup)
    with pytest.raises(IntegrityError):
        command.downgrade(migration_config(), "0008_conversations")
    with connection(database_url) as db:
        assert db.execute(
            "SELECT task_type FROM app.conversations WHERE id=%s", (UUID(cid),)
        ).fetchone() == ("compare",)
        assert db.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "0009_compare",
        )
