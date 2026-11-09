"""D07 uses real PostgreSQL/S3 and existing controlled model protocol fixtures."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_ingestion import process
from test_knowledge import send
from test_knowledge import setup as setup
from test_model_settings import candidate

from backend.config import Settings
from backend.db import migration_config

pytestmark = pytest.mark.integration
Setup = tuple[TestClient, Actor, str, Settings]


def create(owner: Actor, **values: Any) -> dict[str, Any]:
    response = owner.request(
        "POST",
        "/agents",
        json={"name": "团队助手", "taskType": "qa", **values},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()["data"]
    return result


def configured(setup: Setup) -> dict[str, Any]:
    _, owner, kb, _ = setup
    model = owner.request("PUT", "/settings/models/chat", json=candidate("chat"))
    assert model.status_code == 200
    assert (
        owner.request("PUT", "/settings/models/rerank", json=candidate("rerank")).status_code == 200
    )
    doc = send(owner, kb, b"The release process requires review.").json()["data"]
    process(setup, doc)
    return {
        "name": "发布助手",
        "taskType": "qa",
        "prompt": "仅根据证据回答。",
        "knowledgeBaseIds": [kb],
        "toolKeys": ["knowledge.search"],
        "chatConnectionId": model.json()["data"]["id"],
        "enabled": True,
    }


def test_draft_templates_permissions_and_idempotency(setup: Setup, database_url: str) -> None:
    client, owner, _, _ = setup
    options = owner.request("GET", "/agents/options").json()["data"]
    assert {t["taskType"] for t in options["templates"]} == {"qa", "compare", "progress"}
    assert all(t["prompt"] for t in options["templates"])
    member = Actor(client).register()
    owner.add(member)
    body = {"name": "未完成草稿", "taskType": "qa"}
    key = uuid4().hex
    response = owner.request("POST", "/agents", json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    agent = response.json()["data"]
    again = owner.request("POST", "/agents", json=body, headers={"Idempotency-Key": key}).json()[
        "data"
    ]
    assert again["id"] == agent["id"] and again["versionNumber"] == 1
    assert agent["enabled"] is False and agent["readiness"]
    assert (
        owner.request(
            "POST", "/agents", json={**body, "name": "改名"}, headers={"Idempotency-Key": key}
        ).status_code
        == 409
    )
    path = f"/agents/{agent['id']}"
    assert member.request("GET", path).status_code == 404
    assert member.request("GET", "/agents?enabled=false").json()["data"]["items"] == []
    assert member.request("GET", "/agents/options").status_code == 403
    assert (
        member.request(
            "POST", "/agents", json=body, headers={"Idempotency-Key": uuid4().hex}
        ).status_code
        == 403
    )
    assert (
        owner.request(
            "PATCH", path + "/status", json={"enabled": True, "expectedRevision": 1}
        ).status_code
        == 409
    )
    assert (
        owner.request(
            "PUT", path, json={**body, "expectedRevision": 1}, headers={"X-CSRF-Token": ""}
        ).status_code
        == 403
    )
    outsider = Actor(client).register()
    outsider.create_org()
    assert outsider.request("GET", path).status_code == 404
    assert (
        outsider.request(
            "POST",
            path + "/clone",
            json={"newName": "越权副本"},
            headers={"Idempotency-Key": uuid4().hex},
        ).status_code
        == 404
    )
    with connection(database_url) as db:
        assert db.execute(
            "SELECT count(*) FROM app.agent_versions WHERE agent_id=%s", (agent["id"],)
        ).fetchone() == (1,)
        assert db.execute("SELECT to_regclass('app.runs')").fetchone() == (None,)


def test_versions_status_history_and_current_kb_binding(setup: Setup, database_url: str) -> None:
    client, owner, kb, _ = setup
    body = configured(setup)
    agent = create(owner, **body)
    path = f"/agents/{agent['id']}"
    member = Actor(client).register()
    owner.add(member)
    visible = member.request("GET", path).json()["data"]
    assert (
        visible["versionNumber"] == 1
        and "prompt" not in visible
        and "chatConnectionId" not in visible
    )
    assert "credential" not in str(visible).lower()
    deleted = owner.request("DELETE", f"/knowledge-bases/{kb}")
    assert deleted.status_code == 409 and deleted.json()["error"]["code"] == "KB_IN_USE"
    assert agent["id"] in str(deleted.json()["error"]["details"])
    modified = owner.request("PUT", path, json={**body, "prompt": "第二版", "expectedRevision": 1})
    assert modified.status_code == 200, modified.text
    second = modified.json()["data"]
    assert second["versionNumber"] == 2 and second["currentVersionId"] != agent["currentVersionId"]
    assert owner.request("PUT", path, json={**body, "expectedRevision": 1}).status_code == 409
    assert (
        owner.request(
            "PUT", path, json={**body, "taskType": "compare", "expectedRevision": 2}
        ).json()["error"]["code"]
        == "TASK_TYPE_IMMUTABLE"
    )
    revision = owner.request("GET", "/organizations/current").json()["data"]["accessRevision"]
    stopped = owner.request(
        "PATCH", path + "/status", json={"enabled": False, "expectedRevision": 2}
    ).json()["data"]
    assert stopped["versionNumber"] == 2 and stopped["revision"] == 3
    assert (
        owner.request("GET", "/organizations/current").json()["data"]["accessRevision"] > revision
    )
    assert member.request("GET", path).status_code == 404
    assert owner.request("DELETE", f"/knowledge-bases/{kb}").status_code == 409
    cleared = owner.request(
        "PUT", path, json={**body, "enabled": False, "knowledgeBaseIds": [], "expectedRevision": 3}
    )
    assert cleared.status_code == 200
    assert owner.request("DELETE", f"/knowledge-bases/{kb}").status_code == 204
    with connection(database_url) as db:
        assert db.execute(
            "SELECT prompt_text FROM app.agent_versions WHERE id=%s", (agent["currentVersionId"],)
        ).fetchone() == (body["prompt"],)
        assert db.execute(
            "SELECT count(*) FROM app.agent_versions WHERE agent_id=%s", (agent["id"],)
        ).fetchone() == (3,)
        assert db.execute(
            "SELECT count(*) FROM app.agent_version_kbs WHERE agent_version_id=%s",
            (agent["currentVersionId"],),
        ).fetchone() == (1,)
    for sql in [
        "UPDATE app.agent_versions SET prompt_text='corrupt' WHERE id=%s",
        "DELETE FROM app.agent_versions WHERE id=%s",
        "DELETE FROM app.agent_version_kbs WHERE agent_version_id=%s",
    ]:
        with pytest.raises(psycopg.Error), connection(database_url) as db:
            db.execute(sql, (agent["currentVersionId"],))
    with pytest.raises(psycopg.Error), connection(database_url) as db:
        db.execute("UPDATE app.agents SET task_type='progress' WHERE id=%s", (agent["id"],))


def test_readiness_scope_and_validation(setup: Setup) -> None:
    client, owner, kb, _ = setup
    body = configured(setup)
    other = Actor(client).register()
    other.create_org()
    foreign = other.request(
        "POST",
        "/knowledge-bases",
        json={"name": "foreign"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]["id"]
    invalid: list[tuple[dict[str, Any], int]] = [
        ({"knowledgeBaseIds": [foreign]}, 404),
        ({"chatConnectionId": str(uuid4())}, 404),
        ({"toolKeys": ["shell"]}, 422),
        ({"knowledgeBaseIds": [kb, kb]}, 422),
        ({"toolKeys": ["knowledge.search", "knowledge.search"]}, 422),
        ({"credential": "never-echo-me"}, 422),
        ({"prompt": "x" * 20001}, 422),
    ]
    for changes, code in invalid:
        response = owner.request(
            "POST",
            "/agents",
            json={**body, **changes, "enabled": False},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == code, response.text
        assert "never-echo-me" not in response.text
    progress = create(
        owner,
        **{
            **body,
            "taskType": "progress",
            "enabled": False,
            "toolKeys": ["knowledge.search", "github.search_issues", "github.search_pull_requests"],
        },
    )
    assert "GITHUB_UNAVAILABLE" in {x["code"] for x in progress["readiness"]}
    assert (
        owner.request(
            "PATCH",
            f"/agents/{progress['id']}/status",
            json={"enabled": True, "expectedRevision": 1},
        ).status_code
        == 409
    )
    incomplete: list[tuple[dict[str, Any], str]] = [
        ({"prompt": " "}, "PROMPT_REQUIRED"),
        ({"toolKeys": []}, "TOOLS_REQUIRED"),
        ({"knowledgeBaseIds": []}, "KNOWLEDGE_REQUIRED"),
        ({"chatConnectionId": None}, "CHAT_REQUIRED"),
    ]
    for change, expected in incomplete:
        response = owner.request(
            "POST", "/agents", json={**body, **change}, headers={"Idempotency-Key": uuid4().hex}
        )
        assert response.status_code == 409
        assert expected in str(response.json()["error"]["details"])


def test_concurrent_edit_clone_and_paging(setup: Setup, database_url: str) -> None:
    _, owner, kb, _ = setup
    agent = create(
        owner,
        knowledgeBaseIds=[kb],
        toolKeys=["knowledge.search", "github.read_issue"],
        prompt="原版",
    )
    path = f"/agents/{agent['id']}"

    def save(prompt: str) -> int:
        return owner.request(
            "PUT",
            path,
            json={"name": "团队助手", "taskType": "qa", "prompt": prompt, "expectedRevision": 1},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["编辑甲", "编辑乙"])) == [200, 409]
    # Clone an existing saved version; unavailable optional tools are removed explicitly.
    source = create(
        owner,
        knowledgeBaseIds=[kb],
        toolKeys=["knowledge.search", "github.read_issue"],
        prompt="待复制",
    )
    key = uuid4().hex
    copied = owner.request(
        "POST",
        f"/agents/{source['id']}/clone",
        json={"newName": "独立副本"},
        headers={"Idempotency-Key": key},
    )
    assert copied.status_code == 201, copied.text
    clone = copied.json()["data"]
    assert clone["id"] != source["id"] and clone["enabled"] is False
    assert clone["toolKeys"] == ["knowledge.search"] and clone["removedReferences"]
    assert clone["currentVersionId"] != source["currentVersionId"] and clone["versionNumber"] == 1
    assert "credential" not in copied.text.lower()
    assert (
        owner.request(
            "POST",
            f"/agents/{source['id']}/clone",
            json={"newName": "独立副本"},
            headers={"Idempotency-Key": key},
        ).json()["data"]["id"]
        == clone["id"]
    )
    page = owner.request("GET", "/agents?limit=1").json()["data"]
    next_page = owner.request("GET", "/agents?limit=1&cursor=" + page["nextCursor"]).json()["data"]
    assert page["items"][0]["id"] != next_page["items"][0]["id"]
    assert (
        owner.request("GET", "/agents?q=独立副本&taskType=qa&enabled=false").json()["data"][
            "items"
        ][0]["id"]
        == clone["id"]
    )
    # Simulate a legacy invalid reference; clone must not perpetuate it.
    with connection(database_url) as db:
        db.execute("UPDATE app.knowledge_bases SET deleted_at=now() WHERE id=%s", (kb,))
    pruned = owner.request(
        "POST",
        f"/agents/{source['id']}/clone",
        json={"newName": "清理副本"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]
    assert pruned["knowledgeBaseIds"] == [] and pruned["removedReferences"]


def test_readiness_rechecks_resources_and_clone_drops_disabled_model(
    setup: Setup, database_url: str
) -> None:
    client, owner, kb, _ = setup
    agent = create(owner, **configured(setup))
    path = f"/agents/{agent['id']}"
    member = Actor(client).register()
    owner.add(member)
    for method, suffix, body in [
        ("PATCH", "/status", {"enabled": False, "expectedRevision": 1}),
        ("POST", "/clone", {"newName": "禁止复制"}),
        ("PUT", "", {"name": "禁止修改", "taskType": "qa", "expectedRevision": 1}),
    ]:
        assert (
            member.request(
                method, path + suffix, json=body, headers={"Idempotency-Key": uuid4().hex}
            ).status_code
            == 403
        )
    with connection(database_url) as db:
        db.execute(
            "UPDATE app.model_connections SET enabled=false WHERE id=%s",
            (agent["chatConnectionId"],),
        )
        db.execute("UPDATE app.documents SET deleted_at=now() WHERE knowledge_base_id=%s", (kb,))
    current = owner.request("GET", path).json()["data"]
    assert {"CHAT_REQUIRED", "DOCUMENT_REQUIRED"} <= {i["code"] for i in current["readiness"]}
    assert (
        owner.request(
            "PATCH", path + "/status", json={"enabled": True, "expectedRevision": 1}
        ).status_code
        == 409
    )
    copied = owner.request(
        "POST",
        path + "/clone",
        json={"newName": "模型失效副本"},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()["data"]
    assert copied["chatConnectionId"] is None and copied["enabled"] is False
    assert "已剔除不可用聊天模型。" in copied["removedReferences"]


def test_isolated_agent_migration_roundtrip(database_url: str) -> None:
    # Independent fixture database module; downgrade clears only D07 test rows in this database.
    command.downgrade(migration_config(), "0005_ingestion")
    with connection(database_url) as db:
        assert db.execute("SELECT to_regclass('app.agents')").fetchone() == (None,)
    command.upgrade(migration_config(), "head")
    with connection(database_url) as db:
        assert db.execute("SELECT to_regclass('app.agent_versions')").fetchone() == (
            "app.agent_versions",
        )
