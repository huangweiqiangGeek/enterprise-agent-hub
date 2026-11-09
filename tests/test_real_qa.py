"""Opt-in real Bailian QA through Linux Celery, isolated from experience resources."""

import asyncio
import json
import os
import shutil
import subprocess
import time
from uuid import UUID, uuid4

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from redis import Redis
from test_agents import create
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_knowledge import send

from backend.api.app import create_app
from backend.checkpoints import setup_checkpoints
from backend.config import Settings
from backend.services.ingestion import Ingestion
from backend.workers.outbox import Outbox

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("EAH_RUN_REAL_QA") != "1", reason="Real QA opt-in required"),
]


def test_real_qa_linux_duplicate_delivery(database_url: str) -> None:
    docker = os.getenv("EAH_DOCKER") or shutil.which("docker")
    assert docker, "Docker CLI required"
    secret = os.getenv("EAH_PROBE_API_KEY") or dotenv_values(".env").get("EAH_PROBE_API_KEY")
    assert secret, "UNVERIFIED: real provider credential missing"
    token = uuid4().hex
    name = f"eah-d08-test-{token}"
    settings = Settings(
        database_url=database_url,
        s3_bucket=f"eah-qa-{token}",
        run_queue=f"eah.qa.{token}",
        ingestion_queue=f"eah.qa.ingest.{token}",
        redis_url="redis://127.0.0.1:6379/1",
    )
    app = create_app(settings)
    storage = app.state.storage
    storage.client.create_bucket(Bucket=settings.s3_bucket)
    outbox = None
    started = False
    try:
        with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
            assert client.portal
            outbox = Outbox(app.state.engine, settings, storage)
            client.portal.call(setup_checkpoints, settings)
            owner = Actor(client).register()
            owner.create_org("D08 真实隔离验收")
            chat_id = ""
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
                        "modelName": model,
                        "baseUrl": f"https://dashscope.aliyuncs.com/{base}",
                        "credential": secret,
                        "expectedRevision": 0,
                    },
                )
                assert response.status_code == 200, response.json().get("error", {}).get("code")
                if kind == "chat":
                    chat_id = response.json()["data"]["id"]
            kb = owner.request(
                "POST",
                "/knowledge-bases",
                json={"name": "QA 固定资料 v1"},
                headers={"Idempotency-Key": uuid4().hex},
            ).json()["data"]["id"]
            doc = send(
                owner,
                kb,
                "发布规范：发布前必须完成代码评审和自动化测试。"
                "发布负责人是林舟。禁止跳过评审。".encode(),
            ).json()["data"]
            client.portal.call(
                Ingestion(app.state.engine, settings, storage).run,
                UUID(owner.org),
                UUID(doc["id"]),
                UUID(doc["currentAttemptId"]),
            )
            agent = create(
                owner,
                knowledgeBaseIds=[kb],
                toolKeys=["knowledge.search"],
                chatConnectionId=chat_id,
                prompt="用中文依据来源回答。",
                enabled=True,
            )
            conv = owner.request(
                "POST",
                "/conversations",
                json={"agentId": agent["id"], "taskType": "qa"},
                headers={"Idempotency-Key": uuid4().hex},
            )
            assert conv.status_code == 201, conv.text
            accepted = owner.request(
                "POST",
                f"/conversations/{conv.json()['data']['id']}/runs",
                json={
                    "agentId": agent["id"],
                    "input": {
                        "question": "发布前必须完成哪些工作？谁负责发布？",
                        "knowledgeBaseIds": [kb],
                    },
                },
                headers={"Idempotency-Key": uuid4().hex},
            )
            assert accepted.status_code == 202, accepted.text
            run_id = accepted.json()["data"]["runId"]
            env = {
                "EAH_DATABASE_URL": database_url.replace("127.0.0.1", "host.docker.internal"),
                "EAH_REDIS_URL": settings.redis_url.replace("127.0.0.1", "host.docker.internal"),
                "EAH_RUN_QUEUE": settings.run_queue,
            }
            command = [
                docker,
                "run",
                "-d",
                "--name",
                name,
                "--init",
                "--read-only",
                "--dns",
                os.getenv("EAH_WORKER_DNS", "223.5.5.5"),
                "--add-host",
                "host.docker.internal:host-gateway",
                "--tmpfs",
                "/tmp:size=256m,mode=1777",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--pids-limit",
                "96",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
            ]
            for key, value in env.items():
                command.extend(["--env", f"{key}={value}"])
            command.extend(["eah-worker:local", "python", "-m", "backend.workers.runs"])
            subprocess.run(command, check=True, capture_output=True)
            started = True
            begin = time.monotonic()
            client.portal.call(outbox.dispatch)
            # A second Redis delivery races the real worker; only one may claim the run.
            outbox.broker.send_task(
                "eah.run",
                kwargs={"organizationId": owner.org, "runId": run_id},
                queue=settings.run_queue,
            )
            result = {}
            while time.monotonic() - begin < 150:
                result = owner.request("GET", f"/runs/{run_id}").json()["data"]
                if result["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.25)
            assert result["status"] == "succeeded", result
            assert result["citations"] and "评审" in result["result"]["answer"]
            assert "林舟" in result["result"]["answer"]
            # D09 projections must reference the exact evidence from this real-provider run.
            for citation in result["citations"]:
                detail = owner.request("GET", f"/runs/{run_id}/sources/{citation['sourceId']}")
                assert detail.status_code == 200
                source = detail.json()["data"]
                assert source["attemptId"] == citation["attemptId"]
                assert source["chunkId"] == citation["chunkId"] and source["excerpt"]
            exported = owner.request("GET", f"/runs/{run_id}/export")
            assert exported.status_code == 200 and "## 覆盖与限制" in exported.text
            assert all(c["attemptId"] in exported.text for c in result["citations"])
            events = owner.request("GET", f"/runs/{run_id}/events")
            assert events.status_code == 200 and "event: run.completed" in events.text
            with connection(database_url) as db:
                counts = db.execute(
                    "SELECT operation_kind,status,token_source,input_tokens,"
                    "output_tokens,provider_request_id FROM app.usage_entries "
                    "WHERE run_id=%s ORDER BY created_at",
                    (run_id,),
                ).fetchall()
                assert len(counts) == 3 and all(c[1] == "succeeded" for c in counts), counts
                assert all(c[5] for c in counts), "Every real request needs provider trace ID"
                assert counts[-1][2] == "actual" and counts[-1][3] > 0 and counts[-1][4] > 0
                assert db.execute(
                    "SELECT count(*) FROM app.run_events WHERE run_id=%s AND type='run.completed'",
                    (run_id,),
                ).fetchone() == (1,)
                checkpoints = db.execute(
                    "SELECT count(*) FROM checkpoints.checkpoints WHERE thread_id LIKE %s",
                    (f"%:{run_id}",),
                ).fetchone()
                assert checkpoints and checkpoints[0] >= 4
            print(
                json.dumps(
                    {
                        "fixture": "qa-release-v1",
                        "graph": "qa-v1",
                        "model": "qwen-plus",
                        "elapsedMs": round((time.monotonic() - begin) * 1000),
                        "answer": result["result"]["answer"],
                        "citationCount": len(result["citations"]),
                        "usage": counts,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        if started:
            subprocess.run([docker, "rm", "-f", name], check=True, capture_output=True)
        if outbox:
            outbox.broker.close()
        for obj in storage.client.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", []):
            storage.client.delete_object(Bucket=settings.s3_bucket, Key=obj["Key"])
        storage.client.delete_bucket(Bucket=settings.s3_bucket)
        with Redis.from_url(settings.redis_url) as broker:
            for queue in (settings.run_queue, settings.ingestion_queue):
                broker.delete(queue, f"_kombu.binding.{queue}", f"_kombu.binding_permanent.{queue}")
                for key, value in broker.hgetall("unacked").items():
                    if queue.encode() in value:
                        broker.hdel("unacked", key)
                        broker.zrem("unacked_index", key)
        print("D08 temporary container/bucket/queues removed; fixture drops test DB.")
