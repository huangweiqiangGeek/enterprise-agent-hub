"""D09 HTTP acceptance against real, isolated PostgreSQL and S3."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic import command
from test_agents import Setup
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_knowledge import setup as setup
from test_runs import QAProvider, accept, conversation, execute, snapshot

from backend.db import migration_config

pytestmark = pytest.mark.integration


def test_conversations_revision_pagination_scope(setup: Setup) -> None:
    client, owner, _, _ = setup
    cid, agent = conversation(setup)
    other = owner.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": "qa"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["id"]
    member = Actor(client).register()
    owner.add(member)
    assert member.request("GET", "/conversations").json()["data"]["items"] == []
    assert member.request("GET", f"/conversations/{cid}").status_code == 404
    assert (
        member.request(
            "PATCH", f"/conversations/{cid}", json={"title": "x", "expectedRevision": 1}
        ).status_code
        == 404
    )
    opts = member.request("GET", f"/agents/{agent['id']}/task-options")
    assert opts.status_code == 200, opts.text
    assert opts.json()["data"]["documents"]
    assert "prompt" not in opts.text and "connectionId" not in opts.text
    with ThreadPoolExecutor(2) as pool:
        responses = list(
            pool.map(
                lambda title: owner.request(
                    "PATCH", f"/conversations/{cid}", json={"title": title, "expectedRevision": 1}
                ),
                ["release-one", "release-two"],
            )
        )
    assert sorted(r.status_code for r in responses) == [200, 409]
    rows = owner.request("GET", "/conversations?q=release").json()["data"]["items"]
    assert len(rows) == 1 and rows[0]["revision"] == 2
    first = owner.request("GET", "/conversations?limit=1").json()["data"]
    second = owner.request(
        "GET", "/conversations", params={"limit": 1, "cursor": first["nextCursor"]}
    ).json()["data"]
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {cid, other}
    assert (
        owner.request(
            "PATCH",
            f"/conversations/{cid}",
            json={"title": "x", "expectedRevision": 2, "agentId": agent["id"]},
        ).status_code
        == 422
    )


def test_sources_export_events_and_messages(setup: Setup, database_url: str) -> None:
    client, owner, _, _ = setup
    cid, agent = conversation(setup)
    rid = accept(setup, cid, agent, "<script>alert(1)</script> [x](javascript:alert(1))").json()[
        "data"
    ]["runId"]
    assert owner.request("GET", f"/runs/{rid}/export").status_code == 409
    execute(setup, rid, QAProvider())
    final = snapshot(setup, rid)
    events = owner.request("GET", f"/runs/{rid}/events")
    assert events.status_code == 200 and "text/event-stream" in events.headers["content-type"]
    values = [
        json.loads(line[6:]) for line in events.text.splitlines() if line.startswith("data: ")
    ]
    assert [e["seq"] for e in values] == list(range(1, final["lastSeq"] + 1))
    assert values[-1]["type"] == "run.completed"
    assert (
        owner.request(
            "GET", f"/runs/{rid}/events", headers={"Last-Event-ID": f"{rid}:{final['lastSeq']}"}
        ).text
        == ""
    )
    resumed = owner.request("GET", f"/runs/{rid}/events", headers={"Last-Event-ID": f"{rid}:1"})
    assert (
        json.loads(
            next(line[6:] for line in resumed.text.splitlines() if line.startswith("data: "))
        )["seq"]
        == 2
    )
    for cursor in ["bad", f"{uuid4()}:0", f"{rid}:-1", f"{rid}:{final['lastSeq'] + 1}"]:
        response = owner.request("GET", f"/runs/{rid}/events", headers={"Last-Event-ID": cursor})
        assert (
            response.status_code == 422
            and response.json()["error"]["code"] == "EVENT_CURSOR_INVALID"
        )
    citation = final["citations"][0]
    source = owner.request("GET", f"/runs/{rid}/sources/{citation['sourceId']}").json()["data"]
    with connection(database_url) as db:
        row = db.execute(
            "SELECT attempt_id,locator,text_hash FROM app.document_chunks WHERE id=%s",
            (source["chunkId"],),
        ).fetchone()
        assert row and (str(row[0]), row[1], row[2]) == (
            source["attemptId"],
            source["locator"],
            source["contentHash"],
        )
    export = owner.request("GET", f"/runs/{rid}/export")
    assert export.status_code == 200 and "attachment" in export.headers["content-disposition"]
    assert "<script>" not in export.text and "javascript:" not in export.text
    assert all(
        t in export.text
        for t in ["UTC", "## 范围", "## 覆盖与限制", source["attemptId"], source["chunkId"]]
    )
    page = owner.request("GET", f"/conversations/{cid}/messages?limit=1").json()["data"]
    assert page["items"][0]["role"] == "assistant" and not page["items"][0]["isPartial"]
    assert page["items"][0]["result"] == final["result"]
    previous = owner.request(
        "GET", f"/conversations/{cid}/messages", params={"cursor": page["nextCursor"]}
    ).json()["data"]
    assert previous["items"][0]["role"] == "user"
    member = Actor(client).register()
    owner.add(member)
    for suffix in ["events", "export", f"sources/{source['id']}"]:
        assert member.request("GET", f"/runs/{rid}/{suffix}").status_code == 404
    assert owner.request("GET", f"/runs/{rid}/sources/{uuid4()}").status_code == 404
    with connection(database_url) as db:
        db.execute("UPDATE app.documents SET deleted_at=now() WHERE id=%s", (source["documentId"],))
        db.execute("DELETE FROM app.run_events WHERE run_id=%s AND seq<=2", (rid,))
    tombstone = owner.request("GET", f"/runs/{rid}/sources/{source['id']}").json()["data"]
    assert tombstone["availability"] == "deleted" and tombstone["excerpt"] is None
    assert "来源已删除" in owner.request("GET", f"/runs/{rid}/export").text
    assert (
        owner.request("GET", f"/runs/{rid}/events").json()["error"]["code"]
        == "EVENT_CURSOR_EXPIRED"
    )
    assert snapshot(setup, rid)["resultVisibility"] == "partially_unavailable"


def test_admin_audit_cannot_enter_member_conversation(setup: Setup) -> None:
    client, owner, _, _ = setup
    _, agent = conversation(setup)
    member = Actor(client).register()
    owner.add(member)
    cid = member.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": "qa"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["id"]
    rid = member.request(
        "POST",
        f"/conversations/{cid}/runs",
        json={
            "agentId": agent["id"],
            "input": {"question": "release", "knowledgeBaseIds": [setup[2]]},
        },
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["runId"]
    execute(setup, rid, QAProvider())
    assert owner.request("GET", f"/runs/{rid}/export").status_code == 200
    assert owner.request("GET", f"/conversations/{cid}").status_code == 404
    assert owner.request("GET", f"/conversations/{cid}/messages").status_code == 404
    assert (
        owner.request(
            "PATCH", f"/conversations/{cid}", json={"title": "x", "expectedRevision": 1}
        ).status_code
        == 404
    )
    assert accept(setup, cid, agent).status_code == 404


def test_migration_preserves_conversations(setup: Setup, database_url: str) -> None:
    cid, _ = conversation(setup)
    command.downgrade(migration_config(), "0007_runs")
    with connection(database_url) as db:
        assert db.execute("SELECT title FROM app.conversations WHERE id=%s", (cid,)).fetchone() == (
            "文档问答",
        )
    command.upgrade(migration_config(), "head")
    result = setup[1].request("GET", f"/conversations/{cid}")
    assert result.status_code == 200 and result.json()["data"]["revision"] == 1
