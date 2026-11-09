"""D03 contracts with real PostgreSQL and controlled external protocol responses."""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from model_fixture import ProviderFixture
from sqlalchemy.engine import make_url
from test_identity import Actor
from test_identity import database_url as database_url

from backend.api.app import create_app
from backend.config import Settings
from backend.services.model_probe import ModelProbe

pytestmark = pytest.mark.integration
SECRET = "d03-private-credential-never-return"


def candidate(kind: str = "chat", revision: int = 0) -> dict[str, Any]:
    return {
        "providerProtocol": {
            "chat": "openai-chat",
            "embedding": "openai-embeddings",
            "rerank": "cohere-rerank-v2",
        }[kind],
        "baseUrl": "https://api.cohere.com/v2" if kind == "rerank" else "https://api.openai.com/v1",
        "modelName": "gpt-4o-mini-2024-07-18"
        if kind == "chat"
        else "test-embedding"
        if kind == "embedding"
        else "test-rerank",
        "credential": SECRET,
        "expectedRevision": revision,
        **({"embeddingDimension": 3} if kind == "embedding" else {}),
    }


@pytest.fixture
def setup(database_url: str) -> Iterator[tuple[TestClient, ProviderFixture]]:
    settings = Settings(database_url=database_url)
    app = create_app(settings)
    fixture = ProviderFixture()
    app.state.model_probe = ModelProbe(settings, httpx.MockTransport(fixture.handle))
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
        yield client, fixture


def connect(database_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


@pytest.mark.parametrize("kind", ["chat", "embedding", "rerank"])
def test_save_projection_rotation_and_immutable_versions(
    setup: tuple[TestClient, ProviderFixture], database_url: str, kind: str
) -> None:
    client, fixture = setup
    owner, member = Actor(client).register(), Actor(client).register()
    owner.create_org()
    owner.add(member)
    response = owner.request("PUT", f"/settings/models/{kind}", json=candidate(kind))
    assert response.status_code == 200, response.text
    first = response.json()["data"]
    assert first["revision"] == 1 and first["hasCredential"]
    assert SECRET not in response.text
    result = owner.request("GET", "/settings/models").json()["data"]
    assert len(result["items"]) == 1 and result["options"]
    visible = member.request("GET", "/settings/models").json()["data"]
    assert set(visible["items"][0]) == {"id", "kind", "name"} and visible["options"] == []
    forbidden = member.request("PUT", f"/settings/models/{kind}", json=candidate(kind, 1))
    assert forbidden.status_code == 403
    second = owner.request(
        "PUT",
        f"/settings/models/{kind}",
        json={**candidate(kind, 1), "credential": SECRET + "-rotated"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["profileId"] == first["profileId"]
    saved = owner.request(
        "POST", f"/settings/models/{kind}/test", json={"connectionId": first["id"]}
    )
    assert saved.status_code == 200 and saved.json()["data"]["ok"]
    outsider = Actor(client).register()
    outsider.org = owner.org
    assert outsider.request("GET", "/settings/models").status_code == 404
    with connect(database_url) as db:
        assert db.execute(
            "select count(*) from app.model_connection_versions where connection_id=%s",
            (first["id"],),
        ).fetchone() == (2,)
        rows = db.execute(
            "select status, token_source, reserved_tokens, billed_units "
            "from app.usage_entries where organization_id=%s",
            (owner.org,),
        ).fetchall()
        assert len(rows) == (6 if kind == "chat" else 3)
        assert all(r[0] == "succeeded" for r in rows)
        if kind == "rerank":
            assert rows[0][3] == {"search_units": 1}
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute(
                "update app.model_profiles set model_name='changed' where id=%s",
                (first["profileId"],),
            )
        db.rollback()
    assert fixture.requests


@pytest.mark.parametrize("mode", ["auth", "redirect", "timeout", "invalid", "truncated"])
def test_failed_candidate_keeps_active_and_records_usage(
    setup: tuple[TestClient, ProviderFixture], database_url: str, mode: str
) -> None:
    client, fixture = setup
    owner = Actor(client).register()
    owner.create_org()
    first = owner.request("PUT", "/settings/models/chat", json=candidate()).json()["data"]
    fixture.mode = mode
    failed = owner.request("PUT", "/settings/models/chat", json=candidate(revision=1))
    assert failed.status_code == 422, failed.text
    assert SECRET not in failed.text and "raw provider" not in failed.text
    current = owner.request("GET", "/settings/models").json()["data"]["items"][0]
    assert current["revision"] == 1 and current["profileId"] == first["profileId"]
    with connect(database_url) as db:
        assert db.execute(
            "select count(*) from app.usage_entries where organization_id=%s and status='failed'",
            (owner.org,),
        ).fetchone() == (1,)


def test_validation_and_estimated_usage(setup: tuple[TestClient, ProviderFixture]) -> None:
    client, fixture = setup
    owner = Actor(client).register()
    owner.create_org()
    changes: list[dict[str, Any]] = [
        {"baseUrl": "https://attacker.example/v1"},
        {"baseUrl": "https://api.openai.com@127.0.0.1/v1"},
        {"modelName": "unknown"},
        {"credential": ""},
        {"credential": None},
        {"userId": owner.id},
        {"embeddingDimension": 10},
    ]
    for change in changes:
        response = owner.request("PUT", "/settings/models/chat", json={**candidate(), **change})
        assert response.status_code == 422, response.text
        assert SECRET not in response.text
    assert not fixture.requests
    fixture.mode = "no_usage"
    result = owner.request("POST", "/settings/models/chat/test", json=candidate())
    assert result.json()["data"]["ok"]
    assert all(
        u["tokenSource"] == "reserved" and u["reservedTokens"] > 0
        for u in result.json()["data"]["usage"]
    )
    assert owner.request("GET", "/settings/models").json()["data"]["items"] == []


def test_embedding_lock_and_dimension_recheck(
    setup: tuple[TestClient, ProviderFixture], database_url: str
) -> None:
    client, fixture = setup
    owner = Actor(client).register()
    owner.create_org()
    first = owner.request("PUT", "/settings/models/embedding", json=candidate("embedding")).json()[
        "data"
    ]
    # D04 tests exercise the real document guard; this test isolates rotation semantics.
    with patch(
        "backend.repositories.model_settings.ModelRepository.embedding_locked",
        new=AsyncMock(return_value=True),
    ):
        changed = owner.request(
            "PUT",
            "/settings/models/embedding",
            json={**candidate("embedding", 1), "modelName": "different"},
        )
        assert changed.status_code == 409
        rotation = candidate("embedding", 1)
        rotation.pop("embeddingDimension")
        rotated = owner.request("PUT", "/settings/models/embedding", json=rotation)
        assert (
            rotated.status_code == 200 and rotated.json()["data"]["profileId"] == first["profileId"]
        )
        assert rotated.json()["data"]["embeddingLocked"]
        fixture.mode = "dimension"
        mismatch = owner.request(
            "PUT", "/settings/models/embedding", json=candidate("embedding", 2)
        )
        assert mismatch.json()["error"]["code"] == "MODEL_DIMENSION_MISMATCH"


def test_limits_authority_revisions_and_hard_bounds(
    setup: tuple[TestClient, ProviderFixture],
) -> None:
    client, _ = setup
    owner, member = Actor(client).register(), Actor(client).register()
    owner.create_org()
    owner.add(member)
    initial = member.request("GET", "/settings/limits").json()["data"]
    assert initial["effective"] == initial["system"] and initial["revision"] == 0
    body = {"limits": {**initial["effective"], "maxRunSteps": 4}, "expectedRevision": 0}
    assert member.request("PUT", "/settings/limits", json=body).status_code == 403
    saved = owner.request("PUT", "/settings/limits", json=body)
    assert saved.status_code == 200 and saved.json()["data"]["effective"]["maxRunSteps"] == 4
    assert owner.request("PUT", "/settings/limits", json=body).status_code == 409
    for invalid in [0, -1, 11, True, 1.5]:
        response = owner.request(
            "PUT",
            "/settings/limits",
            json={
                "limits": {**initial["effective"], "maxRunSteps": invalid},
                "expectedRevision": 1,
            },
        )
        assert response.status_code == 422
    assert owner.request("GET", "/organizations/current").json()["data"]["settingsRevision"] == 2


def test_concurrent_candidates_one_publish_and_revoke_during_probe(
    setup: tuple[TestClient, ProviderFixture],
) -> None:
    client, fixture = setup
    owner, admin = Actor(client).register(), Actor(client).register()
    owner.create_org()
    membership = owner.add(admin)
    owner.request(
        "PATCH",
        f"/members/{admin.id}",
        json={"baseRole": "admin", "expectedRevision": membership["revision"]},
    )
    assert client.portal is not None

    async def start_gate() -> None:
        fixture.started, fixture.release = asyncio.Event(), asyncio.Event()

    client.portal.call(start_gate)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(admin.request, "PUT", "/settings/models/chat", json=candidate())
        assert fixture.started is not None and fixture.release is not None
        client.portal.call(fixture.started.wait)
        changed = owner.request(
            "PATCH", f"/members/{admin.id}", json={"baseRole": "member", "expectedRevision": 2}
        )
        assert changed.status_code == 200
        client.portal.call(fixture.release.set)
        assert future.result(timeout=10).status_code == 403
    assert owner.request("GET", "/settings/models").json()["data"]["items"] == []
    fixture.started = fixture.release = None
    with ThreadPoolExecutor() as pool:
        futures = [
            pool.submit(owner.request, "PUT", "/settings/models/chat", json=candidate())
            for _ in range(2)
        ]
        assert sorted(f.result(timeout=15).status_code for f in futures) == [200, 409]
