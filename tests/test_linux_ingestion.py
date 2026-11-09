"""Opt-in real Bailian + Linux Celery acceptance, isolated DB/bucket/queue/container."""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from redis import Redis
from test_document_processing import pdf
from test_identity import Actor, connection
from test_identity import database_url as database_url
from test_knowledge import send

from backend.api.app import create_app
from backend.config import Settings
from backend.services.ingestion import Ingestion
from backend.workers.outbox import Outbox

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("EAH_RUN_LINUX_INGESTION") != "1",
        reason="Explicit real Linux/provider opt-in required",
    ),
]


def test_linux_worker_real_vectors_crash_retry(database_url: str) -> None:
    docker = os.getenv("EAH_DOCKER") or shutil.which("docker")
    assert docker, "Docker CLI required"
    secret = os.getenv("EAH_PROBE_API_KEY") or dotenv_values(".env").get("EAH_PROBE_API_KEY")
    assert secret, "Bailian credential required"
    token = uuid4().hex
    name = f"eah-d05-test-{token}"
    settings = Settings(
        database_url=database_url,
        s3_bucket=f"eah-linux-{token}",
        ingestion_queue=f"eah.linux.{token}",
        redis_url="redis://127.0.0.1:6379/1",
    )
    app = create_app(settings)
    storage = app.state.storage
    storage.client.create_bucket(Bucket=settings.s3_bucket)
    started = False
    outbox = None
    try:
        with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
            assert client.portal
            owner = Actor(client).register()
            owner.create_org("D05 Linux 隔离验收")
            configured = owner.request(
                "PUT",
                "/settings/models/embedding",
                json={
                    "providerProtocol": "openai-embeddings",
                    "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "modelName": "text-embedding-v4",
                    "credential": secret,
                    "expectedRevision": 0,
                },
            )
            assert configured.status_code == 200, configured.json().get("error", {}).get("code")
            kb = owner.request(
                "POST",
                "/knowledge-bases",
                json={"name": "Linux 验收"},
                headers={"Idempotency-Key": token},
            ).json()["data"]["id"]
            env = {
                "EAH_DATABASE_URL": database_url.replace("127.0.0.1", "host.docker.internal"),
                "EAH_REDIS_URL": settings.redis_url.replace("127.0.0.1", "host.docker.internal"),
                "EAH_S3_ENDPOINT": settings.s3_endpoint.replace(
                    "127.0.0.1", "host.docker.internal"
                ),
                "EAH_S3_BUCKET": settings.s3_bucket,
                "EAH_INGESTION_QUEUE": settings.ingestion_queue,
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
            command.append("eah-worker:local")
            subprocess.run(command, check=True, capture_output=True)
            started = True
            outbox = Outbox(app.state.engine, settings, storage)
            service = Ingestion(app.state.engine, settings, storage)

            def state(doc: dict[str, Any]) -> dict[str, Any]:
                value: dict[str, Any] = owner.request("GET", f"/documents/{doc['id']}").json()[
                    "data"
                ]["document"]
                return value

            def wait(doc: dict[str, Any], statuses: set[str], seconds: int = 90) -> dict[str, Any]:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    value = state(doc)
                    if value["status"] in statuses:
                        return value
                    time.sleep(0.1)
                raise AssertionError(f"Worker deadline exceeded; last state={state(doc)['status']}")

            for filename, mime, body in (
                ("notes.txt", "text/plain", "中文接口说明与 API release".encode()),
                ("plan.md", "text/markdown", "# 开发计划\n\n第一阶段完成。".encode()),
                ("pages.pdf", "application/pdf", pdf("First page API", "Second page database")),
            ):
                doc = send(owner, kb, body, filename, mime).json()["data"]
                client.portal.call(outbox.dispatch)
                finished = wait(doc, {"ready", "failed"})
                assert finished["status"] == "ready", finished["errorCode"]
                with connection(database_url) as db:
                    assert db.execute(
                        "SELECT min(public.vector_dims(embedding)) "
                        "FROM app.document_chunks WHERE document_id=%s",
                        (doc["id"],),
                    ).fetchone() == (1024,)
                preview = owner.request("GET", f"/documents/{doc['id']}/content?view=text")
                assert preview.status_code == 200
            # A bounded larger sample keeps work observable long enough to kill the actual worker.
            doc = send(owner, kb, ("研发进度与技术风险说明。" * 800).encode()).json()["data"]
            client.portal.call(outbox.dispatch)
            assert wait(doc, {"processing", "ready", "failed"})["status"] == "processing"
            subprocess.run([docker, "kill", name], check=True, capture_output=True)
            assert state(doc)["status"] == "processing"
            with connection(database_url) as db:
                assert db.execute(
                    "SELECT count(*) FROM app.document_chunks WHERE document_id=%s", (doc["id"],)
                ).fetchone() == (0,)
            # Real elapsed lease, not a fabricated success or database timestamp override.
            deadline = time.monotonic() + 40
            while state(doc)["status"] == "processing" and time.monotonic() < deadline:
                client.portal.call(service.reconcile)
                time.sleep(0.5)
            assert state(doc)["errorCode"] == "INGEST_INTERRUPTED"
            retried = owner.request(
                "POST",
                f"/documents/{doc['id']}/retry",
                json={"expectedAttemptId": doc["currentAttemptId"]},
                headers={"Idempotency-Key": uuid4().hex},
            )
            assert retried.status_code == 202
            subprocess.run([docker, "start", name], check=True, capture_output=True)
            client.portal.call(outbox.dispatch)
            final = wait(doc, {"ready", "failed"}, 180)
            assert final["status"] == "ready", final["errorCode"]
            assert final["attemptNumber"] == 2
            with connection(database_url) as db:
                usage = db.execute(
                    "SELECT count(*) FROM app.usage_entries WHERE operation_kind="
                    "'document_embedding' AND status='succeeded' AND token_source='actual'"
                    " AND input_tokens>0"
                ).fetchone()
                assert usage and usage[0] > 3
            print("Linux PDF/TXT/MD + real 1024D vectors + kill/lease/restart/retry: PASS")
    finally:
        if started:
            if sys.exc_info()[0] is not None:
                logs = subprocess.run([docker, "logs", "--tail", "25", name], capture_output=True)
                print((logs.stdout + logs.stderr).decode(errors="replace")[-4000:])
            subprocess.run([docker, "rm", "-f", name], check=True, capture_output=True)
        if outbox:
            outbox.broker.close()
        objects = storage.client.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", [])
        for item in objects:
            storage.client.delete_object(Bucket=settings.s3_bucket, Key=item["Key"])
        storage.client.delete_bucket(Bucket=settings.s3_bucket)
        with Redis.from_url(settings.redis_url) as broker:
            broker.delete(settings.ingestion_queue, f"_kombu.binding.{settings.ingestion_queue}")
            # Remove only this test queue's unacked deliveries after the killed worker.
            for key, value in broker.hgetall("unacked").items():
                if settings.ingestion_queue.encode() in value:
                    broker.hdel("unacked", key)
                    broker.zrem("unacked_index", key)
        print("Temporary Linux worker, bucket and queue removed; DB fixture owns database cleanup.")
