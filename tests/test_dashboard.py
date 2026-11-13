"""D15 read-only dashboard against isolated PostgreSQL/S3; no live provider calls."""

from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from test_agents import Setup, configured, create
from test_github import add, save
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_ingestion import process
from test_knowledge import application, send
from test_knowledge import setup as setup
from test_progress import ProgressWire
from test_runs import accept

from backend.services.github_mcp import GitHubMCP

pytestmark = pytest.mark.integration


def dashboard(owner: Actor) -> dict[str, Any]:
    response = owner.request("GET", "/dashboard")
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json()["data"])


def templates(owner: Actor) -> dict[str, Any]:
    return {t["taskType"]: t for t in dashboard(owner)["templates"]}


def conversation(owner: Actor, agent: dict[str, Any], title: str = "我的会话") -> str:
    response = owner.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": agent["taskType"], "title": title},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["data"]["id"])


def test_dashboard_empty_prerequisites_and_member_actions(setup: Setup, database_url: str) -> None:
    client, owner, _, _ = setup
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.model_connections SET enabled=false WHERE organization_id=%s", (owner.org,)
        )
    result = dashboard(owner)
    assert result["organizationId"] == owner.org and result["effectiveRole"] == "owner"
    assert result["observedAt"] and result["recentConversations"] == []
    for template in result["templates"]:
        assert not template["ready"] and template["readyAgentCount"] == 0
        assert template["inputDescription"] and template["outputDescription"]
        codes = {i["code"] for i in template["issues"]}
        assert {"CHAT_REQUIRED", "EMBEDDING_REQUIRED", "RERANK_REQUIRED", "AGENT_REQUIRED"} <= codes
        assert ("GITHUB_UNAVAILABLE" in codes) == (template["taskType"] == "progress")
        assert (
            "COMPARE_DOCUMENTS_REQUIRED" in codes
            if template["taskType"] == "compare"
            else "DOCUMENT_REQUIRED" in codes
        )
        assert all(i["actionPath"].startswith(f"/o/{owner.org}/") for i in template["issues"])
    member = Actor(client).register()
    owner.add(member)
    view = dashboard(member)
    assert view["effectiveRole"] == "member"
    assert all(
        i["actionPath"] is None and i["actionLabel"] == "联系管理员"
        for t in view["templates"]
        for i in t["issues"]
    )


def test_dashboard_qa_compare_not_blocked_by_missing_github(setup: Setup) -> None:
    _, owner, kb, _ = setup
    config = configured(setup)
    create(owner, **config)
    create(owner, **{**config, "name": "对比", "taskType": "compare"})
    initial = templates(owner)
    assert initial["qa"]["ready"] and not initial["compare"]["ready"]
    assert "COMPARE_DOCUMENTS_REQUIRED" in {i["code"] for i in initial["compare"]["issues"]}
    document = send(owner, kb, b"Second independent release document.", "solution.txt").json()[
        "data"
    ]
    process(setup, document)
    current = templates(owner)
    assert current["qa"]["ready"] and current["compare"]["ready"]
    assert not current["qa"]["issues"] and not current["compare"]["issues"]
    assert not current["progress"]["ready"]
    assert "GITHUB_UNAVAILABLE" in {i["code"] for i in current["progress"]["issues"]}


@pytest.mark.parametrize("invalidation", ["vector", "attempt", "document"])
def test_dashboard_compare_requires_current_vector_documents(
    setup: Setup, database_url: str, invalidation: str
) -> None:
    _, owner, kb, _ = setup
    config = configured(setup)
    create(owner, **{**config, "taskType": "compare"})
    doc = send(owner, kb, b"Second current document.", "solution.txt").json()["data"]
    process(setup, doc)
    assert templates(owner)["compare"]["ready"]
    with connection(database_url) as db:
        if invalidation == "vector":
            db.execute(
                "UPDATE app.document_chunks SET embedding=NULL, text=NULL WHERE document_id=%s",
                (doc["id"],),
            )
        elif invalidation == "attempt":
            db.execute(
                "UPDATE app.document_attempts SET status='failed' WHERE document_id=%s",
                (doc["id"],),
            )
        else:
            db.execute("UPDATE app.documents SET deleted_at=now() WHERE id=%s", (doc["id"],))
    assert not templates(owner)["compare"]["ready"]


def test_dashboard_same_agent_scope_and_one_healthy_candidate(setup: Setup) -> None:
    client, owner, _, settings = setup
    config = configured(setup)
    first = create(owner, **{**config, "taskType": "compare", "name": "第一范围"})
    response = owner.request(
        "POST",
        "/knowledge-bases",
        json={"name": "第二范围"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    kb = response.json()["data"]["id"]
    doc = send(owner, kb, b"Another scope document.", "scope.txt").json()["data"]
    process((client, owner, kb, settings), doc)
    create(owner, **{**config, "taskType": "compare", "name": "第二范围", "knowledgeBaseIds": [kb]})
    assert not templates(owner)["compare"]["ready"]
    create(
        owner,
        **{
            **config,
            "taskType": "compare",
            "name": "完整范围",
            "knowledgeBaseIds": [*config["knowledgeBaseIds"], kb],
        },
    )
    result = templates(owner)["compare"]
    assert result["ready"] and result["readyAgentCount"] == 1 and not result["issues"]
    create(owner, **{**config, "name": "健康QA"})
    create(owner, **{**config, "name": "将失效QA", "knowledgeBaseIds": [kb]})
    assert owner.request("DELETE", "/documents/" + doc["id"]).status_code == 204
    result = templates(owner)["qa"]
    assert result["ready"] and result["readyAgentCount"] == 1 and not result["issues"]
    assert (
        owner.request(
            "PATCH",
            f"/agents/{first['id']}/status",
            json={"enabled": False, "expectedRevision": first["revision"]},
        ).status_code
        == 200
    )


def test_dashboard_github_readiness_is_separate_and_current(setup: Setup) -> None:
    client, owner, _, _ = setup
    config = configured(setup)
    wire = ProgressWire()
    application(client).state.github_mcp = GitHubMCP(httpx.MockTransport(wire.handle))
    assert save(owner).status_code == 200
    grant = add(owner).json()["data"]["id"]
    create(
        owner,
        **{
            **config,
            "taskType": "progress",
            "toolKeys": ["knowledge.search", "github.search_issues", "github.search_pull_requests"],
        },
    )
    count = len(wire.requests)
    assert templates(owner)["progress"]["ready"]
    assert len(wire.requests) == count
    assert owner.request("DELETE", "/connections/github/repositories/" + grant).status_code == 204
    result = templates(owner)["progress"]
    assert not result["ready"] and "GITHUB_UNAVAILABLE" in {i["code"] for i in result["issues"]}


def test_dashboard_recent_owner_only_stable_sort_and_disabled_privacy(
    setup: Setup, database_url: str
) -> None:
    client, owner, _, _ = setup
    agent = create(owner, **configured(setup))
    member = Actor(client).register()
    owner.add(member)
    ids = [conversation(owner, agent, "owner-" + str(n)) for n in range(7)]
    mine = conversation(member, agent, "member-only")
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.conversations SET updated_at='2026-01-01T00:00:00Z' "
            "WHERE organization_id=%s",
            (owner.org,),
        )
    recent = dashboard(owner)["recentConversations"]
    assert [c["id"] for c in recent] == sorted(ids, reverse=True)[:5]
    assert [c["id"] for c in dashboard(member)["recentConversations"]] == [mine]
    rid = accept(setup, ids[0], agent).json()["data"]["runId"]
    recent = dashboard(owner)["recentConversations"]
    assert recent[0]["id"] == ids[0] and recent[0]["lastRunStatus"] == "queued"
    assert recent[0]["activeRunId"] == rid
    assert (
        owner.request(
            "PATCH",
            f"/agents/{agent['id']}/status",
            json={"enabled": False, "expectedRevision": agent["revision"]},
        ).status_code
        == 200
    )
    member_view = dashboard(member)
    assert member_view["recentConversations"][0]["agentName"] == "已停用 Agent"
    assert agent["name"] not in str(member_view)
    assert not templates(owner)["qa"]["ready"]
    outsider = Actor(client).register()
    outsider.create_org("other-org")
    assert dashboard(outsider)["recentConversations"] == []
    outsider.org = owner.org
    assert outsider.request("GET", "/dashboard").status_code == 404


def test_dashboard_repeated_reads_have_no_work_side_effects(
    setup: Setup, database_url: str
) -> None:
    _, owner, _, _ = setup
    create(owner, **configured(setup))
    tables = ["runs", "usage_entries", "outbox_jobs", "conversations", "run_events"]
    with connection(database_url) as db:
        before = {
            table: cast(tuple[int], db.execute(f"SELECT count(*) FROM app.{table}").fetchone())[0]
            for table in tables
        }
    with (
        patch(
            "backend.services.model_probe.ModelProbe.request",
            new=AsyncMock(side_effect=AssertionError("provider called")),
        ),
        patch(
            "backend.services.model_probe.ModelProbe.test",
            new=AsyncMock(side_effect=AssertionError("model probe called")),
        ),
        patch(
            "backend.services.github_mcp.GitHubMCP.read",
            new=AsyncMock(side_effect=AssertionError("MCP called")),
        ),
        patch(
            "backend.services.github_mcp.GitHubMCP.test",
            new=AsyncMock(side_effect=AssertionError("MCP probe called")),
        ),
    ):
        first = dashboard(owner)
        second = dashboard(owner)
        assert first["templates"] == second["templates"]
    with connection(database_url) as db:
        after = {
            table: cast(tuple[int], db.execute(f"SELECT count(*) FROM app.{table}").fetchone())[0]
            for table in tables
        }
    assert before == after


def test_dashboard_current_embedding_profile_invalidates_ready_documents(
    setup: Setup, database_url: str
) -> None:
    _, owner, _, _ = setup
    create(owner, **configured(setup))
    assert templates(owner)["qa"]["ready"]
    # Simulate a maintenance profile replacement only in the disposable test database.
    profile = uuid4()
    with connection(database_url) as db:
        db.execute(
            "INSERT INTO app.model_profiles "
            "(id,created_at,organization_id,connection_id,provider_protocol,base_url,model_name,"
            "dimension,tokenizer_id,capabilities,parameters,version) "
            "SELECT %s,now(),p.organization_id,p.connection_id,p.provider_protocol,p.base_url,"
            "'maintenance-replacement',p.dimension,p.tokenizer_id,p.capabilities,"
            "p.parameters,p.version+1 "
            "FROM app.model_profiles p JOIN app.model_connections c ON c.active_profile_id=p.id "
            "WHERE c.organization_id=%s AND c.kind='embedding'",
            (profile, owner.org),
        )
        db.execute(
            "UPDATE app.model_connections SET active_profile_id=%s "
            "WHERE organization_id=%s AND kind='embedding'",
            (profile, owner.org),
        )
    result = templates(owner)["qa"]
    assert not result["ready"] and "DOCUMENT_REQUIRED" in {i["code"] for i in result["issues"]}


def test_dashboard_admin_role_removed_member_and_anonymous(setup: Setup, database_url: str) -> None:
    client, owner, _, _ = setup
    member = Actor(client).register()
    owner.add(member)
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.org_memberships SET base_role='admin' WHERE organization_id=%s "
            "AND user_id=%s",
            (owner.org, member.id),
        )
    result = dashboard(member)
    assert result["effectiveRole"] == "admin"
    for template in result["templates"]:
        issue = next(i for i in template["issues"] if i["code"] == "AGENT_REQUIRED")
        assert issue["actionPath"] == f"/o/{owner.org}/agents/new?taskType={template['taskType']}"
    with connection(database_url) as db:
        db.execute(
            "DELETE FROM app.org_memberships WHERE organization_id=%s AND user_id=%s",
            (owner.org, member.id),
        )
    assert member.request("GET", "/dashboard").status_code == 404
    anonymous = Actor(client)
    anonymous.org = owner.org
    assert anonymous.request("GET", "/dashboard").status_code == 401
