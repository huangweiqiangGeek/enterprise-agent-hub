"""M6 deterministic SQL, authorization and timezone acceptance on disposable PG/S3."""

from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Any
from uuid import uuid4

import pytest
from test_agents import Setup
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_knowledge import setup as setup
from test_runs import QAProvider, accept, conversation, execute

pytestmark = pytest.mark.integration


def data(actor: Actor, path: str, **params: Any) -> dict[str, Any]:
    response = actor.request("GET", path, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]  # type: ignore[no-any-return]


def seed(setup: Setup, database_url: str) -> tuple[list[str], dict[str, Any]]:
    _, owner, _, _ = setup
    first, agent = conversation(setup)
    ids = []
    times = [
        "2026-03-08T04:59:00Z",
        "2026-03-08T05:00:00Z",
        "2026-03-08T08:00:00Z",
        "2026-03-08T20:00:00Z",
        "2026-03-09T03:59:00Z",
        "2026-03-09T04:00:00Z",
    ]
    states = ["succeeded", "succeeded", "failed", "cancelled", "queued", "timed_out"]
    for index, (stamp, state) in enumerate(zip(times, states, strict=True)):
        cid = (
            first
            if not index
            else owner.request(
                "POST",
                "/conversations",
                json={
                    "agentId": agent["id"],
                    "taskType": "qa",
                },
                headers={"Idempotency-Key": uuid4().hex},
            ).json()["data"]["id"]
        )
        rid = accept(setup, cid, agent).json()["data"]["runId"]
        ids.append(rid)
        with connection(database_url) as db:
            db.execute(
                """UPDATE app.runs SET created_at=%s, status=%s,
                started_at=CASE WHEN %s='queued' THEN NULL
                    ELSE %s::timestamptz+interval '2 seconds' END,
                finished_at=CASE WHEN %s='queued' THEN NULL
                    ELSE %s::timestamptz+interval '12 seconds' END
                WHERE id=%s""",
                (stamp, state, state, stamp, state, stamp, rid),
            )
            if index in (1, 2, 3):
                db.execute(
                    """INSERT INTO app.usage_entries
                    (id,created_at,organization_id,run_id,operation_kind,operation_id,attempt_no,
                     reserved_tokens,input_tokens,output_tokens,token_source,billed_units,duration_ms,status)
                    VALUES (%s,now(),%s,%s,'run.chat',%s,1,%s,%s,%s,%s,'{}',10,%s)""",
                    (
                        uuid4(),
                        owner.org,
                        rid,
                        uuid4(),
                        30 if index == 2 else 0,
                        10 if index == 1 else None,
                        5 if index == 1 else None,
                        {1: "actual", 2: "reserved", 3: "unavailable"}[index],
                        {1: "succeeded", 2: "failed", 3: "outcome_unknown"}[index],
                    ),
                )
    return ids, agent


def test_statistics_dates_pagination_scope_and_history(setup: Setup, database_url: str) -> None:
    client, owner, _, _ = setup
    ids, agent = seed(setup, database_url)
    filters = {"startDate": "2026-03-08", "endDate": "2026-03-08", "timezone": "America/New_York"}
    summary = data(owner, "/usage/summary", **filters)
    assert summary["runCount"] == 4 and summary["terminalCount"] == 3
    assert summary["successRate"] == pytest.approx(1 / 3)
    assert summary["rangeUtc"] == {
        "start": "2026-03-08T05:00:00Z",
        "endExclusive": "2026-03-09T04:00:00Z",
    }
    assert summary["chat"]["inputTokens"] == 10
    assert summary["chat"]["outputTokens"] == 5
    assert summary["chat"]["estimatedTokens"] == 30
    assert summary["chat"]["unavailableUsageCount"] == 1
    assert summary["chat"]["unknownOutcomeCount"] == 1
    assert summary["executionDuration"] == {
        "count": 3,
        "avg": 10000.0,
        "p50": 10000.0,
        "p95": 10000.0,
    }
    page = data(owner, "/runs", limit=2, **filters)
    second = data(owner, "/runs", limit=2, cursor=page["nextCursor"], **filters)
    assert [r["id"] for r in page["items"] + second["items"]] == list(reversed(ids[1:5]))
    assert second["nextCursor"] is None
    assert (
        owner.request(
            "GET", "/runs", params={**filters, "cursor": page["nextCursor"], "status": "failed"}
        ).status_code
        == 422
    )
    queued = data(owner, "/usage/summary", status="queued")
    assert queued["successRate"] is None and queued["terminalCount"] == 0
    assert data(owner, "/usage/summary", agentId=str(uuid4()))["runCount"] == 0
    member = Actor(client).register()
    owner.add(member)
    assert data(member, "/runs")["items"] == []
    assert data(member, "/usage/summary")["runCount"] == 0
    for path in ("/runs", "/usage/summary"):
        assert member.request("GET", path, params={"scope": "organization"}).status_code == 403
    assert member.request("GET", f"/runs/{ids[1]}/report").status_code == 404
    with connection(database_url) as db:
        db.execute("UPDATE app.agents SET name='Renamed current Agent' WHERE id=%s", (agent["id"],))
    report = data(owner, f"/runs/{ids[1]}/report")
    assert report["agentName"] == agent["name"]
    assert data(owner, "/runs")["items"][0]["agentName"] == agent["name"]
    assert report["isOwner"] is True
    assert data(owner, "/runs/agents")["items"] == [{"id": agent["id"], "name": agent["name"]}]
    # The owner can audit another initiator, but members cannot audit the owner.
    with connection(database_url) as db:
        db.execute("UPDATE app.runs SET initiator_id=%s WHERE id=%s", (member.id, ids[2]))
    assert data(owner, "/usage/summary")["runCount"] == 5
    assert data(owner, "/usage/summary", scope="organization")["runCount"] == 6
    assert data(owner, f"/runs/{ids[2]}/report")["isOwner"] is False
    assert data(member, "/usage/summary")["runCount"] == 1
    assert owner.request("POST", f"/runs/{ids[2]}/cancel").status_code == 404
    outsider = Actor(client).register()
    outsider.create_org()
    assert data(outsider, "/usage/summary")["runCount"] == 0
    assert outsider.request("GET", f"/runs/{ids[1]}/report").status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"startDate": "2026-03-08"},
        {"timezone": "Invalid/Zone"},
        {"startDate": "2026-03-09", "endDate": "2026-03-08"},
        {"status": "invented"},
    ],
)
def test_invalid_filters(setup: Setup, params: dict[str, str]) -> None:
    for path in ("/runs", "/usage/summary"):
        assert setup[1].request("GET", path, params=params).status_code == 422


def test_real_run_attempts_and_concurrent_settlement(setup: Setup, database_url: str) -> None:
    _, owner, _, _ = setup
    cid, agent = conversation(setup)
    rid = accept(setup, cid, agent).json()["data"]["runId"]
    execute(setup, rid, QAProvider("rate_once"))
    report = data(owner, f"/runs/{rid}/report")
    assert report["byKind"]["chat"]["attemptCount"] == 2
    assert report["byKind"]["chat"]["failedCount"] == 1
    assert report["byKind"]["embedding"]["attemptCount"] == 1
    before = data(owner, "/usage/summary")
    with connection(database_url) as db:
        db.execute(
            """INSERT INTO app.run_events (organization_id,run_id,seq,type,occurred_at,data)
            SELECT organization_id,run_id,1000,type,occurred_at,data FROM app.run_events
            WHERE run_id=%s AND type='tool.completed' LIMIT 1""",
            (rid,),
        )
    for _ in range(2):
        data(owner, f"/runs/{rid}")
        data(owner, f"/runs/{rid}/steps")
        data(owner, "/runs")
    after = data(owner, "/usage/summary")
    assert before["byKind"] == after["byKind"]
    with connection(database_url) as db:
        usage = db.execute(
            "SELECT id FROM app.usage_entries WHERE run_id=%s "
            "AND operation_kind='run.chat' AND status='failed'",
            (rid,),
        ).fetchone()
        assert usage

    def settle() -> None:
        with connection(database_url) as db:
            db.execute("SELECT id FROM app.organizations WHERE id=%s FOR UPDATE", (owner.org,))
            db.execute(
                "UPDATE app.usage_entries SET token_source='actual',input_tokens=7,output_tokens=3 "
                "WHERE id=%s",
                (usage[0],),
            )

    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(settle)
        concurrent = data(owner, "/usage/summary")
        future.result()
    assert concurrent["chat"]["inputTokens"] in (100, 107)
    final = data(owner, "/usage/summary")
    assert final["chat"]["inputTokens"] == 107
    assert final["chat"]["outputTokens"] == 53
    assert final["chat"]["attemptCount"] == 2
    assert final["terminalCount"] == 1
    assert final["byKind"]["rerank"]["billedUnits"]["search_units"] > 0


def test_retry_links_and_representative_list(setup: Setup, database_url: str) -> None:
    _, owner, _, _ = setup
    cid, agent = conversation(setup)
    original = accept(setup, cid, agent).json()["data"]["runId"]
    assert owner.request("POST", f"/runs/{original}/cancel").status_code == 202
    snapshot = data(owner, f"/runs/{original}")
    retry = owner.request(
        "POST",
        f"/conversations/{cid}/runs",
        json={
            "agentId": agent["id"],
            "input": snapshot["input"],
            "retryOfRunId": original,
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    rid = retry.json()["data"]["runId"]
    assert data(owner, f"/runs/{original}/report")["retryIds"] == [rid]
    assert data(owner, f"/runs/{rid}")["retryOfRunId"] == original
    # Terminal synthetic histories exercise bounded pages without invoking providers.
    with connection(database_url) as db:
        db.execute(
            """INSERT INTO app.runs
            (id,created_at,organization_id,conversation_id,initiator_id,agent_version_id,
             status,cancel_requested,input_snapshot,config_snapshot,budget_snapshot,coverage,
             last_seq,lease_generation,graph_version)
            SELECT gen_random_uuid(),created_at,organization_id,conversation_id,initiator_id,
             agent_version_id,'cancelled',true,input_snapshot,config_snapshot,budget_snapshot,
             coverage,0,0,graph_version FROM app.runs CROSS JOIN generate_series(1,1000)
             WHERE id=%s""",
            (original,),
        )
    start = perf_counter()
    rows = data(owner, "/runs", limit=20)
    list_ms = (perf_counter() - start) * 1000
    start = perf_counter()
    summary = data(owner, "/usage/summary")
    summary_ms = (perf_counter() - start) * 1000
    assert len(rows["items"]) == 20 and rows["nextCursor"]
    assert summary["runCount"] == 1002 and summary["terminalCount"] == 1001
    print({"runs": 1002, "pageSize": 20, "listMs": round(list_ms), "summaryMs": round(summary_ms)})
