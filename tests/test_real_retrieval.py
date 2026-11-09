"""Opt-in D06 real Embedding + Rerank with a versioned bilingual tiny corpus."""

import asyncio
import json
import os
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from test_identity import Actor, connection
from test_identity import database_url as database_url

from backend.api.app import create_app
from backend.config import Settings
from backend.services.ingestion import Ingestion

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EAH_RUN_REAL_RETRIEVAL") != "1", reason="Explicit real-provider opt-in required"
    ),
]


def test_real_bilingual_retrieval(database_url: str) -> None:
    secret = os.getenv("EAH_PROBE_API_KEY") or dotenv_values(".env").get("EAH_PROBE_API_KEY")
    if not secret:
        pytest.fail("UNVERIFIED: Bailian probe credential missing")
    corpus = json.loads(
        (Path(__file__).parent / "fixtures/retrieval.json").read_text(encoding="utf-8")
    )
    settings = Settings(database_url=database_url, s3_bucket=f"eah-retrieval-{uuid4().hex}")
    app = create_app(settings)
    storage = app.state.storage.client
    storage.create_bucket(Bucket=settings.s3_bucket)
    try:
        with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
            owner = Actor(client).register()
            owner.create_org("D06真实隔离验收")
            for kind, protocol, model, base in (
                ("embedding", "openai-embeddings", "text-embedding-v4", "compatible-mode/v1"),
                ("rerank", "dashscope-rerank", "gte-rerank-v2", "api/v1"),
            ):
                response = owner.request(
                    "PUT",
                    f"/settings/models/{kind}",
                    json={
                        "providerProtocol": protocol,
                        "modelName": model,
                        "baseUrl": f"https://dashscope.aliyuncs.com/{base}",
                        "credential": secret,
                        "expectedRevision": 0,
                    },
                )
                assert response.status_code == 200, response.status_code
            kb = owner.request(
                "POST",
                "/knowledge-bases",
                json={"name": "中英固定检索样例"},
                headers={"Idempotency-Key": uuid4().hex},
            ).json()["data"]["id"]
            service = Ingestion(app.state.engine, settings, app.state.storage)
            assert client.portal
            for document in corpus["documents"]:
                response = owner.request(
                    "POST",
                    f"/knowledge-bases/{kb}/documents",
                    files={"file": (document["name"], document["text"].encode(), "text/plain")},
                    headers={"Idempotency-Key": uuid4().hex},
                )
                assert response.status_code == 202
                doc = response.json()["data"]
                client.portal.call(
                    service.run, UUID(owner.org), UUID(doc["id"]), UUID(doc["currentAttemptId"])
                )
                assert (
                    owner.request("GET", f"/documents/{doc['id']}").json()["data"]["document"][
                        "status"
                    ]
                    == "ready"
                )
            for sample in corpus["queries"]:
                started = monotonic()
                response = owner.request(
                    "POST", f"/knowledge-bases/{kb}/search-preview", json={"query": sample["query"]}
                )
                assert response.status_code == 200, response.text
                value = response.json()["data"]
                assert value["items"][0]["fileName"] == sample["expected"]
                assert value["usage"][0]["tokenSource"] == "actual"
                assert value["usage"][1]["billedUnits"]["total_tokens"] > 0
                print(
                    json.dumps(
                        {
                            "corpus": corpus["version"],
                            "expected": sample["expected"],
                            "top1": value["items"][0]["fileName"],
                            "elapsedMs": round((monotonic() - started) * 1000),
                            "usage": value["usage"],
                            "coverage": value["coverage"],
                        },
                        ensure_ascii=False,
                    )
                )
            with connection(database_url) as db:
                assert db.execute(
                    "SELECT count(*) FROM app.usage_entries "
                    "WHERE operation_kind='search_preview' AND status='succeeded'"
                ).fetchone() == (4,)
                assert db.execute("SELECT to_regclass('app.runs')").fetchone() == (None,)
    finally:
        for obj in storage.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", []):
            storage.delete_object(Bucket=settings.s3_bucket, Key=obj["Key"])
        storage.delete_bucket(Bucket=settings.s3_bucket)
