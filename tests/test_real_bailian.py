"""Opt-in paid-provider acceptance with disposable PostgreSQL/S3, never a mock transport."""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from test_identity import Actor, connection
from test_identity import database_url as database_url

from backend.api.app import create_app
from backend.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EAH_RUN_REAL_BAILIAN") != "1", reason="Explicit real-provider opt-in required"
    ),
]


def test_real_models_saved_usage_and_reliable_upload(database_url: str) -> None:
    secret = os.getenv("EAH_PROBE_API_KEY") or dotenv_values(".env").get("EAH_PROBE_API_KEY")
    if not secret:
        pytest.fail("UNVERIFIED: Bailian probe credential missing")
    settings = Settings(database_url=database_url, s3_bucket=f"eah-real-{uuid4().hex}")
    app = create_app(settings)
    storage = app.state.storage.client
    storage.create_bucket(Bucket=settings.s3_bucket)
    try:
        with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
            owner, outsider = Actor(client).register(), Actor(client).register()
            owner.create_org("百炼隔离验收")
            profiles = {}
            for kind, protocol, model, base in (
                ("chat", "dashscope-chat", "qwen-plus", "compatible-mode/v1"),
                ("embedding", "openai-embeddings", "text-embedding-v4", "compatible-mode/v1"),
                ("rerank", "dashscope-rerank", "gte-rerank-v2", "api/v1"),
            ):
                response = owner.request(
                    "PUT",
                    f"/settings/models/{kind}",
                    json={
                        "providerProtocol": protocol,
                        "baseUrl": f"https://dashscope.aliyuncs.com/{base}",
                        "modelName": model,
                        "credential": secret,
                        "expectedRevision": 0,
                    },
                )
                if secret in response.text:
                    pytest.fail("Credential exposed in model response")
                assert response.status_code == 200
                profiles[kind] = response.json()["data"]
            assert profiles["embedding"]["dimension"] == 1024
            assert profiles["chat"]["tokenizerId"] == "dashscope-qwen-1.26.7"
            outsider.org = owner.org
            assert outsider.request("GET", "/settings/models").status_code == 404
            kb = owner.request(
                "POST",
                "/knowledge-bases",
                json={"name": "真实模型上传验收"},
                headers={"Idempotency-Key": uuid4().hex},
            ).json()["data"]["id"]
            key, body = uuid4().hex, "# D04\n真实配置仅接受排队，不伪装向量入库。".encode()
            for _ in range(2):
                response = owner.request(
                    "POST",
                    f"/knowledge-bases/{kb}/documents",
                    files={"file": ("acceptance.md", body, "text/markdown")},
                    headers={"Idempotency-Key": key},
                )
                assert response.status_code == 202
            doc = response.json()["data"]
            assert doc["status"] == "queued" and doc["chunkCount"] == 0
            lookup = owner.request(
                "GET", f"/idempotency-requests/{key}?operation=document.upload&resourceId={kb}"
            ).json()["data"]
            assert lookup["status"] == "accepted" and lookup["targetId"] == doc["id"]
            original = client.get(f"/api/v1/documents/{doc['id']}/content", headers=owner.headers())
            assert original.status_code == 200 and original.content == body
            assert outsider.request("GET", f"/documents/{doc['id']}").status_code == 404
            with connection(database_url) as db:
                assert db.execute("SELECT count(*) FROM app.documents").fetchone() == (1,)
                assert db.execute("SELECT count(*) FROM app.outbox_jobs").fetchone() == (1,)
                snapshot = db.execute(
                    "SELECT embedding_profile_id FROM app.document_attempts WHERE id=%s",
                    (doc["currentAttemptId"],),
                ).fetchone()
                assert snapshot and str(snapshot[0]) == profiles["embedding"]["profileId"]
                rows = db.execute(
                    "SELECT operation_kind,status,token_source,billed_units FROM app.usage_entries"
                ).fetchall()
                assert len(rows) == 4 and all(row[1] == "succeeded" for row in rows)
                assert all(row[2] == "actual" for row in rows if row[0] != "model_test_rerank")
                assert (
                    next(row[3] for row in rows if row[0] == "model_test_rerank")["total_tokens"]
                    > 0
                )
            # Verify the documented output bound with a deliberately too-small limit.
            with httpx.Client(timeout=25, trust_env=False, follow_redirects=False) as external:
                limited = external.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers={"Authorization": f"Bearer {secret}"},
                    json={
                        "model": "qwen-plus",
                        "enable_thinking": False,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "List the numbers 1 to 20."}],
                    },
                )
            assert limited.status_code == 200
            assert limited.json()["choices"][0]["finish_reason"] == "length"
            assert limited.json()["usage"]["completion_tokens"] == 1
    finally:
        objects = storage.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", [])
        for item in objects:
            storage.delete_object(Bucket=settings.s3_bucket, Key=item["Key"])
        storage.delete_bucket(Bucket=settings.s3_bucket)
