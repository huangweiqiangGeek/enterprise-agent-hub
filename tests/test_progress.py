"""D14 real isolated PG/S3 + synthetic MCP/model protocols; no real service claim."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from github_fixture import GitHubFixture
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from test_agents import Setup, configured, create
from test_compare import submit
from test_github import add, save
from test_identity import connection
from test_identity import database_url as database_url
from test_knowledge import application
from test_knowledge import setup as setup
from test_runs import QAProvider, snapshot

from backend.checkpoints import setup_checkpoints
from backend.db import migration_config
from backend.run_schemas import ProgressInput
from backend.services.github_mcp import GitHubMCP
from backend.services.model_probe import ModelProbe
from backend.services.qa import QA
from backend.services.run_state import RunState

pytestmark = pytest.mark.integration
CASES = json.loads((Path(__file__).parent / "fixtures/progress.json").read_text("utf-8"))["cases"]


def item(kind: str, number: int = 1, **values: Any) -> dict[str, Any]:
    path = "issues" if kind == "issue" else "pull"
    return {
        "number": number,
        "html_url": f"https://github.com/example/hub/{path}/{number}",
        "title": "Release review",
        "body": "Review must precede release.",
        "state": "closed",
        "updated_at": "2026-01-02T12:00:00Z",
        "created_at": "2025-12-20T12:00:00Z",
        "closed_at": "2026-01-02T11:00:00Z",
        **values,
    }


class ProgressWire(GitHubFixture):
    def __init__(self) -> None:
        super().__init__()
        self.by_kind = {
            "issue": [item("issue")],
            "pr": [item("pr", merged_at="2026-01-02T11:00:00Z")],
        }
        self.fail_pr = False
        self.after_issue: Callable[[], None] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content) if request.content else {}
        if data.get("method") == "tools/call":
            name = data["params"]["name"]
            if name != "search_repositories":
                kind = "issue" if name in {"search_issues", "issue_read"} else "pr"
                self.items = self.by_kind[kind]
                self.mode = "call_error" if kind == "pr" and self.fail_pr else "ok"
        response = super().handle(request)
        if (
            data.get("method") == "tools/call"
            and data["params"]["name"] == "search_issues"
            and self.after_issue
        ):
            callback, self.after_issue = self.after_issue, None
            callback()
        return response


class ProgressProvider(QAProvider):
    def __init__(self, case: str = "progress", fake: bool = False):
        super().__init__()
        self.case, self.fake = case, fake

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
        source = str(uuid4()) if self.fake else evidence[0]["sourceId"]
        block = {
            "type": "unknown",
            "kind": "unknown",
            "text": CASES[self.case]["modelText"],
            "sourceIds": [],
        }
        if self.case in {"blocker", "risk", "progress"}:
            block = {
                "type": "suggestion" if self.case == "progress" else self.case,
                "kind": "suggestion" if self.case == "progress" else "inference",
                "text": CASES[self.case]["modelText"],
                "sourceIds": [source],
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


def prepare(
    setup: Setup, *, details: bool = False
) -> tuple[str, dict[str, Any], dict[str, Any], ProgressWire]:
    client, owner, kb, _ = setup
    config = configured(setup)
    wire = ProgressWire()
    application(client).state.github_mcp = GitHubMCP(httpx.MockTransport(wire.handle))
    response = save(owner)
    assert response.status_code == 200, response.text
    response = add(owner)
    assert response.status_code == 201, response.text
    grant = response.json()["data"]["id"]
    keys = ["knowledge.search", "github.search_issues", "github.search_pull_requests"]
    if details:
        keys += ["github.read_issue", "github.read_pull_request"]
    config.update(taskType="progress", name="进展报告", toolKeys=keys)
    agent = create(owner, **config)
    response = owner.request(
        "POST",
        "/conversations",
        json={"agentId": agent["id"], "taskType": "progress"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201, response.text
    value = {
        "repoId": grant,
        "startDate": "2026-01-01",
        "endDate": "2026-01-03",
        "timezone": "Asia/Shanghai",
        "knowledgeBaseIds": [kb],
    }
    return response.json()["data"]["id"], agent, value, wire


def execute(setup: Setup, rid: str, wire: ProgressWire, provider: ProgressProvider) -> None:
    client, owner, _, settings = setup
    app = application(client)
    assert client.portal
    client.portal.call(setup_checkpoints, settings)
    state = RunState(app.state.engine, settings, UUID(owner.org), UUID(rid))
    client.portal.call(
        QA(
            state,
            ModelProbe(settings, httpx.MockTransport(provider.handle)),
            GitHubMCP(httpx.MockTransport(wire.handle)),
        ).run
    )


@pytest.mark.parametrize(
    "case,group",
    [(case, values["group"]) for case, values in CASES.items()],
)
def test_progress_cases_and_exact_citations(
    setup: Setup, database_url: str, case: str, group: str
) -> None:
    cid, agent, value, wire = prepare(setup)
    wire.by_kind = {kind: [item(kind, **CASES[case][kind])] for kind in ("issue", "pr")}
    key = uuid4().hex
    accepted = submit(setup, cid, agent, value, key)
    assert accepted.status_code == 202, accepted.text
    rid = accepted.json()["data"]["runId"]
    assert submit(setup, cid, agent, value, key).json()["data"]["runId"] == rid
    provider = ProgressProvider(case)
    execute(setup, rid, wire, provider)
    result = snapshot(setup, rid)
    assert result["status"] == "succeeded", result
    assert any(c["text"] == CASES[case]["modelText"] for c in result["result"][group])
    sent = next(request for request in provider.requests if request.get("stream"))
    evidence = json.loads(next(m["content"] for m in sent["messages"] if m["role"] == "user"))[
        "evidence"
    ]
    for kind in ("issue", "pr"):
        assert any(CASES[case][kind]["body"] == e["text"] for e in evidence)
    assert len(result["result"]["progress"]) == 2
    assert result["coverage"]["observedIssueCount"] == result["coverage"]["observedPrCount"] == 1
    assert result["coverage"]["pagesFetched"] == 2 and result["coverage"]["partial"] is True
    calls = [r["params"] for r in wire.requests if r["method"] == "tools/call"]
    assert {c["name"] for c in calls} >= {"search_issues", "search_pull_requests"}
    for claim in result["result"]["progress"]:
        source = (
            setup[1].request("GET", f"/runs/{rid}/sources/{claim['sourceIds'][0]}").json()["data"]
        )
        assert source["kind"] == "github" and source["repoId"] == value["repoId"]
        assert source["documentId"] is None and source["htmlUrl"].startswith(
            "https://github.com/example/hub/"
        )
    exported = setup[1].request("GET", f"/runs/{rid}/export")
    assert exported.status_code == 200 and "GitHub" in exported.text and "进展事实" in exported.text
    execute(setup, rid, wire, provider)
    assert provider.chat_calls == 1
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s AND type='run.completed'", (rid,)
        ).fetchone() == (1,)


def test_progress_dedup_truncation_and_utc_filter(setup: Setup) -> None:
    cid, agent, value, wire = prepare(setup)
    wire.by_kind["issue"] = (
        [item("issue", 1), item("issue", 1)]
        + [item("issue", i) for i in range(2, 19)]
        + [item("issue", 50, updated_at="2026-01-03T16:00:00Z")]
    )
    wire.by_kind["pr"] = [item("pr", merged_at="2025-12-30T00:00:00Z")]
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    execute(setup, rid, wire, ProgressProvider())
    result = snapshot(setup, rid)
    assert result["status"] == "succeeded", result
    assert result["coverage"]["observedIssueCount"] == 18
    assert result["coverage"]["hasMore"] is True
    assert result["coverage"]["omittedGitHubCount"] == 14
    assert len(result["result"]["progress"]) == 5
    assert "范围外" in result["result"]["progress"][-1]["text"]


def test_progress_required_query_failure_and_fake_citation(setup: Setup) -> None:
    cid, agent, value, wire = prepare(setup)
    wire.fail_pr = True
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    provider = ProgressProvider()
    execute(setup, rid, wire, provider)
    assert snapshot(setup, rid)["status"] == "failed" and provider.chat_calls == 0
    wire.fail_pr = False
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    execute(setup, rid, wire, ProgressProvider("risk", fake=True))
    assert snapshot(setup, rid)["errorCode"] == "CITATION_INVALID"


def test_progress_revocation_hides_all_mixed_projections(setup: Setup) -> None:
    cid, agent, value, wire = prepare(setup)
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    execute(setup, rid, wire, ProgressProvider())
    before = snapshot(setup, rid)
    assert before["status"] == "succeeded", before
    sid = before["result"]["progress"][0]["sourceIds"][0]
    assert (
        setup[1]
        .request("DELETE", "/connections/github/repositories/" + value["repoId"])
        .status_code
        == 204
    )
    after = snapshot(setup, rid)
    assert (
        after["resultVisibility"] == "withheld"
        and after["result"] is None
        and not after["citations"]
    )
    assert not after["partialMessage"] and not after["coverage"]
    messages = setup[1].request("GET", f"/conversations/{cid}/messages").json()["data"]["items"]
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert (
        assistant["resultVisibility"] == "withheld"
        and not assistant["content"]
        and assistant["result"] is None
    )
    for suffix in ("export", "sources/" + sid, "events"):
        assert setup[1].request("GET", f"/runs/{rid}/{suffix}").status_code == 403
    steps = setup[1].request("GET", f"/runs/{rid}/steps").json()["data"]["items"]
    assert all(not s["publicOutputSummary"] and not s["sourceIds"] for s in steps)


def test_progress_missing_input_scope_and_downgrade(setup: Setup, database_url: str) -> None:
    cid, agent, value, _ = prepare(setup)
    changes: list[dict[str, Any]] = [
        {"timezone": "Not/AZone"},
        {"endDate": "2025-01-01"},
        {"endDate": "2999-01-01"},
        {"knowledgeBaseIds": []},
        {"startDate": ""},
    ]
    for change in changes:
        assert submit(setup, cid, agent, {**value, **change}).status_code == 422
    assert submit(setup, cid, agent, {**value, "repoId": str(uuid4())}).status_code == 403
    with pytest.raises(IntegrityError):
        command.downgrade(migration_config(), "0010_github")
    with connection(database_url) as db:
        assert db.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "0011_progress",
        )


def test_progress_timezone_dst_and_invalid_range() -> None:
    base = {
        "repoId": str(uuid4()),
        "knowledgeBaseIds": [str(uuid4())],
        "startDate": "2026-03-08",
        "endDate": "2026-03-08",
        "timezone": "America/New_York",
    }
    start, end = ProgressInput.model_validate(base).utc_range()
    assert start == datetime(2026, 3, 8, 5, tzinfo=UTC)
    assert (end - start).total_seconds() == 23 * 3600
    with pytest.raises(ValidationError):
        ProgressInput.model_validate({**base, "timezone": "invalid"})


def test_progress_empty_queries_are_successful_but_not_whole_repo_claim(setup: Setup) -> None:
    cid, agent, value, wire = prepare(setup)
    wire.by_kind = {"issue": [], "pr": []}
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    execute(setup, rid, wire, ProgressProvider("unknown"))
    result = snapshot(setup, rid)
    assert result["status"] == "succeeded", result
    assert result["result"]["progress"] == []
    assert result["coverage"]["observedIssueCount"] == result["coverage"]["observedPrCount"] == 0
    assert result["coverage"]["pagesFetched"] == 2
    assert any("不代表没有工作发生" in c["text"] for c in result["result"]["toVerify"])
    assert {c["itemType"] for c in result["coverage"]["queryCriteria"]} == {"issue", "pr"}


@pytest.mark.parametrize("details", [False, True])
def test_progress_optional_details_bounded_and_authorized(setup: Setup, details: bool) -> None:
    cid, agent, value, wire = prepare(setup, details=details)
    wire.by_kind = {kind: [item(kind, n, body="") for n in range(1, 5)] for kind in ("issue", "pr")}
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    provider = ProgressProvider()
    execute(setup, rid, wire, provider)
    result = snapshot(setup, rid)
    assert result["status"] == "succeeded", result
    calls = [r["params"] for r in wire.requests if r["method"] == "tools/call"]
    for name, number in (("issue_read", "issue_number"), ("pull_request_read", "pullNumber")):
        reads = [c for c in calls if c["name"] == name]
        assert len(reads) == int(details)
        if details:
            assert reads[0]["arguments"][number] == 1
            assert reads[0]["arguments"]["method"] == "get"
    assert len(result["result"]["progress"]) == 8
    execute(setup, rid, wire, provider)
    assert provider.chat_calls == 1


@pytest.mark.parametrize("revocation", ["repository", "connection", "tool"])
def test_progress_mid_call_revocation_discards_results(
    setup: Setup, database_url: str, revocation: str
) -> None:
    cid, agent, value, wire = prepare(setup)
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]

    def revoke() -> None:
        with connection(database_url) as db:
            if revocation == "repository":
                db.execute(
                    "UPDATE app.github_repositories SET enabled=false WHERE id=%s",
                    (value["repoId"],),
                )
            elif revocation == "connection":
                db.execute(
                    "UPDATE app.github_connections SET enabled=false WHERE organization_id=%s",
                    (setup[1].org,),
                )
            else:
                db.execute(
                    "INSERT INTO app.org_tool_settings (organization_id,tool_key,enabled,revision) "
                    "VALUES (%s,'github.search_issues',false,1)",
                    (setup[1].org,),
                )

    wire.after_issue = revoke
    provider = ProgressProvider()
    execute(setup, rid, wire, provider)
    result = snapshot(setup, rid)
    assert result["status"] == "failed", result
    assert result["errorCode"] == "GITHUB_PERMISSION_REVOKED"
    assert result["resultVisibility"] == "withheld" and result["result"] is None
    assert provider.chat_calls == 0
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.run_sources WHERE run_id=%s AND source_kind='github'", (rid,)
        ).fetchone() == (0,)
    assert not any(r.get("params", {}).get("name") == "search_pull_requests" for r in wire.requests)


def test_progress_connection_auth_failure_and_budget_prevent_calls(
    setup: Setup, database_url: str
) -> None:
    cid, agent, value, wire = prepare(setup)
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    wire.mode = "auth"
    provider = ProgressProvider()
    execute(setup, rid, wire, provider)
    result = snapshot(setup, rid)
    assert result["status"] == "failed", result
    assert provider.chat_calls == 0
    wire.mode = "ok"
    rid = submit(setup, cid, agent, value).json()["data"]["runId"]
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.runs SET budget_snapshot=jsonb_set(budget_snapshot,"
            "'{max_tool_attempts}','1') WHERE id=%s",
            (rid,),
        )
    count = len(wire.requests)
    execute(setup, rid, wire, provider)
    result = snapshot(setup, rid)
    assert result["errorCode"] == "BUDGET_EXCEEDED", result
    assert len(wire.requests) == count and provider.chat_calls == 0
