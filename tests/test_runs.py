"""D08 acceptance uses isolated real PG/S3 and an explicit provider wire fixture."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from model_fixture import ProviderFixture
from test_agents import Setup, configured, create
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_knowledge import application
from test_knowledge import setup as setup

from backend.checkpoints import setup_checkpoints
from backend.run_schemas import ResultBlock
from backend.services.errors import AppError
from backend.services.model_probe import ModelProbe
from backend.services.qa import QA, QAState, validate
from backend.services.run_state import RunState

pytestmark = pytest.mark.integration


class QAProvider(ProviderFixture):
    def __init__(self, mode: str = "answer"):
        super().__init__()
        self.qa_mode = mode
        self.chat_calls = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content)
        if (
            not request.url.path.endswith("chat/completions")
            or "tools" in value
            or not value.get("stream")
        ):
            return await super().handle(request)
        self.requests.append(value)
        self.chat_calls += 1
        if self.qa_mode == "auth":
            return httpx.Response(401, json={"error": "private-provider-error"})
        if self.qa_mode == "invalid_request":
            return httpx.Response(400, json={"error": "private-provider-error"})
        if self.qa_mode == "wire_invalid":
            return httpx.Response(200, content="data: []\n\ndata: [DONE]\n\n")
        if self.qa_mode == "rate_once" and self.chat_calls == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        evidence = json.loads(
            next(
                m["content"]
                for m in value["messages"]
                if m["role"] == "user" and '"evidence"' in m["content"]
            )
        )["evidence"]
        source = evidence[0]["sourceId"]
        if self.qa_mode == "fake":
            source = str(uuid4())
        blocks = [
            {
                "type": "claim",
                "text": "The release process requires review.",
                "kind": "fact",
                "sourceIds": [source],
            }
        ]
        if self.qa_mode == "unknown":
            blocks = [
                {
                    "type": "unknown",
                    "text": "资料未说明负责人，请补充负责人信息。",
                    "kind": "unknown",
                    "sourceIds": [],
                }
            ]
        if self.qa_mode == "conflict":
            blocks = [
                {
                    "type": "conflict",
                    "text": "资料对评审要求有冲突，需确认适用版本。",
                    "kind": "fact",
                    "sourceIds": [e["sourceId"] for e in evidence],
                }
            ]
        text = "\n".join(json.dumps(b, ensure_ascii=False) for b in blocks) + "\n"
        chunks = [
            {"id": "fixture-request", "choices": [{"index": 0, "delta": {"content": text[:20]}}]},
            {"choices": [{"index": 0, "delta": {"content": text[20:]}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": None
                if self.qa_mode == "no_usage"
                else {"prompt_tokens": 100, "completion_tokens": 50},
            },
        ]
        return httpx.Response(
            200,
            content="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
            + ("" if self.qa_mode == "interrupted" else "data: [DONE]\n\n"),
            headers={"content-type": "text/event-stream"},
        )


def conversation(setup: Setup) -> tuple[str, dict[str, Any]]:
    _, owner, _, _ = setup
    agent = create(owner, **configured(setup))
    response = owner.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": "qa"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"], agent


def accept(
    setup: Setup,
    conv: str,
    agent: dict[str, Any],
    question: str = "What is required for release?",
    key: str | None = None,
) -> Any:
    _, owner, kb, _ = setup
    return owner.request(
        "POST",
        f"/conversations/{conv}/runs",
        json={"agentId": agent["id"], "input": {"question": question, "knowledgeBaseIds": [kb]}},
        headers={"Idempotency-Key": key or uuid4().hex},
    )


def execute(setup: Setup, run_id: str, provider: QAProvider) -> None:
    client, owner, _, settings = setup
    app = application(client)
    assert client.portal
    client.portal.call(setup_checkpoints, settings)
    state = RunState(app.state.engine, settings, UUID(owner.org), UUID(run_id))
    client.portal.call(QA(state, ModelProbe(settings, httpx.MockTransport(provider.handle))).run)


def snapshot(setup: Setup, run_id: str) -> dict[str, Any]:
    response = setup[1].request("GET", "/runs/" + run_id)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["data"]
    return result


def test_accept_idempotency_busy_scope_and_terminal(setup: Setup, database_url: str) -> None:
    conv, agent = conversation(setup)
    key = uuid4().hex
    response = accept(setup, conv, agent, key=key)
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["runId"]
    assert accept(setup, conv, agent, key=key).json()["data"]["runId"] == run_id
    assert accept(setup, conv, agent, "changed", key).status_code == 409
    assert accept(setup, conv, agent).json()["error"]["code"] == "CONVERSATION_BUSY"
    assert snapshot(setup, run_id)["status"] == "queued"
    lookup = setup[1].request(
        "GET",
        f"/idempotency-requests/{key}",
        params={"operation": "run.create", "resourceId": conv},
    )
    assert lookup.status_code == 200 and lookup.json()["data"]["targetId"] == run_id
    assert (
        setup[1]
        .request(
            "GET",
            f"/idempotency-requests/{uuid4().hex}",
            params={"operation": "run.create", "resourceId": conv},
        )
        .json()["data"]["status"]
        == "absent"
    )
    provider = QAProvider()
    execute(setup, run_id, provider)
    result = snapshot(setup, run_id)
    assert result["status"] == "succeeded", result
    assert result["citations"] and "review" in result["result"]["answer"]
    execute(setup, run_id, provider)
    assert provider.chat_calls == 1
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s AND type='run.completed'",
            (run_id,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM app.usage_entries WHERE run_id=%s", (run_id,)
        ).fetchone() == (3,)
        events = db.execute(
            "SELECT seq FROM app.run_events WHERE run_id=%s ORDER BY seq", (run_id,)
        ).fetchall()
        assert [e[0] for e in events] == list(range(1, len(events) + 1))
    steps = setup[1].request("GET", f"/runs/{run_id}/steps").json()["data"]["items"]
    assert len(steps) == 3 and any(s["usage"] for s in steps)
    member = Actor(setup[0]).register()
    setup[1].add(member)
    assert member.request("GET", f"/runs/{run_id}").status_code == 404
    assert (
        member.request(
            "GET",
            f"/idempotency-requests/{key}",
            params={"operation": "run.create", "resourceId": conv},
        ).status_code
        == 404
    )
    assert (
        member.request(
            "POST",
            f"/conversations/{conv}/runs",
            json={
                "agentId": agent["id"],
                "input": {"question": "x", "knowledgeBaseIds": [setup[2]]},
            },
            headers={"Idempotency-Key": uuid4().hex},
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "mode,status,error,calls",
    [
        ("unknown", "succeeded", None, 1),
        ("fake", "failed", "CITATION_INVALID", 3),
        ("auth", "failed", "MODEL_AUTH_FAILED", 1),
        ("no_usage", "succeeded", None, 1),
        ("invalid_request", "failed", "MODEL_REQUEST_INVALID", 1),
        ("wire_invalid", "failed", "MODEL_RESPONSE_INVALID", 1),
        ("rate_once", "succeeded", None, 2),
        ("interrupted", "failed", "MODEL_STREAM_INTERRUPTED", 1),
    ],
)
def test_candidates_failure_and_usage(
    setup: Setup, database_url: str, mode: str, status: str, error: str | None, calls: int
) -> None:
    conv, agent = conversation(setup)
    response = accept(setup, conv, agent)
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["runId"]
    provider = QAProvider(mode)
    execute(setup, run_id, provider)
    result = snapshot(setup, run_id)
    assert (result["status"], result["errorCode"]) == (status, error), result
    assert provider.chat_calls == calls
    with connection(database_url) as db:
        entries = db.execute(
            "SELECT status,token_source,reserved_tokens FROM app.usage_entries "
            "WHERE run_id=%s AND operation_kind='run.chat'",
            (run_id,),
        ).fetchall()
        assert len(entries) == calls and all(e[0] != "pending" and e[2] > 0 for e in entries)
        if mode in {"auth", "no_usage"}:
            assert entries[0][1] == "reserved"
    if mode == "unknown":
        assert result["result"]["missingInformation"] and not result["citations"]
    if mode == "fake":
        assert result["result"] is None and not result["citations"]


def test_budget_and_duplicate_acceptance(setup: Setup, database_url: str) -> None:
    conv, agent = conversation(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: accept(setup, conv, agent), range(2)))
    assert sorted(r.status_code for r in responses) == [202, 409]
    run_id = next(r for r in responses if r.status_code == 202).json()["data"]["runId"]
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.runs SET budget_snapshot="
            "jsonb_set(budget_snapshot,'{max_run_tokens}','1') WHERE id=%s",
            (run_id,),
        )
    provider = QAProvider()
    execute(setup, run_id, provider)
    result = snapshot(setup, run_id)
    assert result["errorCode"] == "BUDGET_EXCEEDED", result
    assert provider.chat_calls == 0


def test_fixed_conflict_and_followup(setup: Setup, database_url: str) -> None:
    from test_ingestion import process
    from test_knowledge import send

    conv, agent = conversation(setup)
    doc = send(setup[1], setup[2], b"The release process does not require review.").json()["data"]
    process(setup, doc)
    run_id = accept(setup, conv, agent).json()["data"]["runId"]
    provider = QAProvider("conflict")
    execute(setup, run_id, provider)
    result = snapshot(setup, run_id)
    assert result["status"] == "succeeded", result
    assert len(result["result"]["conflicts"][0]["sourceIds"]) == 2
    followup = accept(setup, conv, agent, "What about review?").json()["data"]["runId"]
    second = QAProvider()
    execute(setup, followup, second)
    assert snapshot(setup, followup)["status"] == "succeeded"
    assert any(
        m["role"] == "assistant" for r in second.requests if r.get("stream") for m in r["messages"]
    )


def test_validation_does_not_trust_summary() -> None:
    with pytest.raises(AppError):
        validate([ResultBlock(type="summary", text="unsupported")], set(), {})
    with pytest.raises(AppError):
        validate([ResultBlock(type="claim", kind="fact", text="unsupported")], set(), {})


@pytest.mark.parametrize("revoke", ["agent", "source"])
def test_revocation_during_model_call(setup: Setup, database_url: str, revoke: str) -> None:
    conv, agent = conversation(setup)
    run_id = accept(setup, conv, agent).json()["data"]["runId"]
    provider = QAProvider()
    original = provider.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        response = await original(request)
        if provider.chat_calls:
            with connection(database_url) as db:
                if revoke == "agent":
                    db.execute("UPDATE app.agents SET enabled=false WHERE id=%s", (agent["id"],))
                else:
                    db.execute(
                        "UPDATE app.documents SET deleted_at=now() WHERE organization_id=%s",
                        (setup[1].org,),
                    )
        return response

    provider.handle = handle  # type: ignore[method-assign]
    execute(setup, run_id, provider)
    result = snapshot(setup, run_id)
    assert result["status"] == "failed" and not result["result"] and not result["citations"]
    assert result["partialMessage"] == ""
    assert provider.chat_calls == 1
    with connection(database_url) as db:
        row = db.execute(
            "SELECT status,reserved_tokens FROM app.usage_entries WHERE run_id=%s "
            "AND operation_kind='run.chat'",
            (run_id,),
        ).fetchone()
        assert row and row[0] == "failed" and row[1] > 0


def test_claim_terminal_races_and_late_metering(setup: Setup, database_url: str) -> None:
    from backend.services.model_probe import Attempt

    conv, agent = conversation(setup)
    run_id = accept(setup, conv, agent).json()["data"]["runId"]
    client, owner, _, settings = setup
    engine = application(client).state.engine
    assert client.portal

    async def scenario() -> None:
        first = RunState(engine, settings, UUID(owner.org), UUID(run_id))
        second = RunState(engine, settings, UUID(owner.org), UUID(run_id))
        claims = await asyncio.gather(first.claim(), second.claim())
        assert sum(r is not None for r in claims) == 1
        state = first if claims[0] else second
        assert not await state.terminal(error="RUN_INTERRUPTED", expired_only=True)
        usage = Attempt(1, reserved=100, token_source="reserved")
        await state.reserve("answer", "model", "chat.generate", {}, usage)
        outcomes = await asyncio.gather(
            state.terminal(error="RUN_CANCELLED"), state.terminal(error="MODEL_UNAVAILABLE")
        )
        assert sum(outcomes) == 1
        with pytest.raises(AppError):
            await state.delta("late content")
        usage.status, usage.token_source, usage.input_tokens = "succeeded", "actual", 10
        await state.settle(usage, 100, "late-request")

    client.portal.call(scenario)
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s AND type LIKE 'run.%%' "
            "AND type<>'run.started'",
            (run_id,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT input_tokens,provider_request_id FROM app.usage_entries WHERE run_id=%s",
            (run_id,),
        ).fetchone() == (10, "late-request")


@pytest.mark.parametrize("change", ["agent", "source", "deadline", "lease", "queue"])
def test_revocation_and_reconcile(setup: Setup, database_url: str, change: str) -> None:
    from backend.workers.runs import reconcile

    conv, agent = conversation(setup)
    run_id = accept(setup, conv, agent).json()["data"]["runId"]
    client, owner, _, settings = setup
    state = RunState(application(client).state.engine, settings, UUID(owner.org), UUID(run_id))
    assert client.portal
    if change == "queue":
        with connection(database_url) as db:
            db.execute(
                "UPDATE app.runs SET created_at=now()-interval '6 minutes' WHERE id=%s", (run_id,)
            )
        client.portal.call(reconcile, state.engine, settings)
        assert snapshot(setup, run_id)["errorCode"] == "RUN_QUEUE_TIMEOUT"
        return
    assert client.portal.call(state.claim)
    with connection(database_url) as db:
        if change == "agent":
            db.execute("UPDATE app.agents SET enabled=false WHERE id=%s", (agent["id"],))
        elif change == "source":
            db.execute(
                "UPDATE app.documents SET deleted_at=now() WHERE organization_id=%s", (owner.org,)
            )
        else:
            column = "deadline_at" if change == "deadline" else "lease_until"
            from psycopg import sql

            db.execute(
                sql.SQL("UPDATE app.runs SET {}=now()-interval '1 second' WHERE id=%s").format(
                    sql.Identifier(column)
                ),
                (run_id,),
            )
    if change == "lease":
        client.portal.call(reconcile, state.engine, settings)
        assert snapshot(setup, run_id)["status"] == "running"
        with pytest.raises(AppError):
            client.portal.call(state.check)
        execute(setup, run_id, QAProvider())
        assert snapshot(setup, run_id)["status"] == "succeeded"
    elif change != "source":
        with pytest.raises(AppError) as exc:
            client.portal.call(state.check)
        assert exc.value.code == (
            "PERMISSION_REVOKED" if change == "agent" else "RUN_DEADLINE_EXCEEDED"
        )
        client.portal.call(state.terminal, None, exc.value.code)
    else:
        # Deletion after acceptance must prevent retrieval even without stored run_sources yet.
        graph = QA(state, ModelProbe(settings, httpx.MockTransport(QAProvider().handle)))
        with pytest.raises(AppError):
            client.portal.call(graph.retrieve, QAState())


def test_no_evidence_and_deleted_source_cleanup(
    setup: Setup, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.repositories.retrieval import RetrievalRepository
    from backend.workers.outbox import Outbox

    conv, agent = conversation(setup)
    run_id = accept(setup, conv, agent).json()["data"]["runId"]
    provider = QAProvider()
    execute(setup, run_id, provider)
    result = snapshot(setup, run_id)
    doc = result["citations"][0]["documentId"]
    assert setup[1].request("DELETE", f"/documents/{doc}").status_code == 204
    assert snapshot(setup, run_id)["resultVisibility"] == "partially_unavailable"
    outbox = Outbox(
        application(setup[0]).state.engine, setup[3], application(setup[0]).state.storage
    )
    assert setup[0].portal
    try:
        setup[0].portal.call(outbox.dispatch)
    finally:
        outbox.broker.close()
    with connection(database_url) as db:
        assert db.execute(
            "SELECT excerpt FROM app.run_sources WHERE run_id=%s", (run_id,)
        ).fetchone() == (None,)
    from test_ingestion import process
    from test_knowledge import send

    document = send(setup[1], setup[2], b"Release review is required.").json()["data"]
    process(setup, document)

    async def empty(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(RetrievalRepository, "search", empty)
    run_id = accept(setup, conv, agent).json()["data"]["runId"]
    provider = QAProvider()
    execute(setup, run_id, provider)
    assert provider.chat_calls == 0
    result = snapshot(setup, run_id)
    assert result["status"] == "succeeded" and result["result"]["missingInformation"]
