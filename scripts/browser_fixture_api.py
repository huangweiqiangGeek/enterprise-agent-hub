"""Explicit isolated-browser fixture entry. Never used by the normal backend entry."""

import asyncio
import importlib.util
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.app import create_app
from backend.config import Settings
from backend.models.knowledge import Document
from backend.models.runs import Run
from backend.rag.embeddings import Embeddings
from backend.runtime import run_async
from backend.services.ingestion import Ingestion
from backend.services.model_probe import ModelProbe
from backend.services.qa import QA
from backend.services.run_state import RunState


class BrowserQAStream(httpx.AsyncByteStream):
    def __init__(self, sources: list[tuple[str, str]]):
        self.sources = sources

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for text, source_id in self.sources:
            block = (
                json.dumps(
                    {"type": "claim", "text": text, "kind": "fact", "sourceIds": [source_id]},
                    ensure_ascii=False,
                )
                + "\n"
            )
            yield (
                "data: "
                + json.dumps({"choices": [{"index": 0, "delta": {"content": block}}]})
                + "\n\n"
            ).encode()
            await asyncio.sleep(4)
        yield b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n'
        yield (
            b'data: {"choices": [], "usage": {"prompt_tokens": 100, '
            b'"completion_tokens": 50}}\n\ndata: [DONE]\n\n'
        )


def main() -> None:
    path = Path(__file__).resolve().parent.parent / "tests/model_fixture.py"
    spec = importlib.util.spec_from_file_location("eah_browser_model_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Test protocol fixture missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = Settings()
    app = create_app(settings)
    fixture = module.ProviderFixture()

    async def handle(request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content)
        if (
            os.getenv("EAH_BROWSER_QA") == "1"
            and request.url.path.endswith("chat/completions")
            and value.get("stream")
            and "tools" not in value
        ):
            evidence = json.loads(
                next(
                    m["content"]
                    for m in value["messages"]
                    if m["role"] == "user" and '"evidence"' in m["content"]
                )
            )["evidence"]
            if any(e.get("role") == "requirement" for e in evidence):
                block = {
                    "type": "comparison",
                    "kind": "fact",
                    "text": "对比结果",
                    "sourceIds": [],
                    "item": {
                        "requirement": "发布必须经过评审",
                        "requirementSourceIds": [
                            e["sourceId"] for e in evidence if e.get("role") == "requirement"
                        ],
                        "solutionEvidence": "方案要求评审",
                        "solutionSourceIds": [
                            e["sourceId"] for e in evidence if e.get("role") == "solution"
                        ],
                        "status": "covered",
                        "impact": "需核对实施证据",
                        "suggestion": "建议补充验收记录",
                    },
                }
                chunk = {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": json.dumps(block, ensure_ascii=False)},
                            "finish_reason": "stop",
                        }
                    ]
                }
                return httpx.Response(
                    200,
                    content="data: "
                    + json.dumps(chunk)
                    + '\n\ndata: {"choices": [], "usage": {"prompt_tokens": 100, '
                    '"completion_tokens": 100}}\n\ndata: [DONE]\n\n',
                )
            return httpx.Response(
                200,
                stream=BrowserQAStream(
                    [
                        (text, next(e["sourceId"] for e in evidence if keyword in e["text"]))
                        for text, keyword in [
                            ("发布必须经过评审。", "评审"),
                            ("发布应保留回滚记录。", "回滚"),
                        ]
                    ]
                ),
                headers={"content-type": "text/event-stream"},
            )
        response = await fixture.handle(request)
        assert isinstance(response, httpx.Response)
        return response

    app.state.model_probe = ModelProbe(settings, httpx.MockTransport(handle))
    if os.getenv("EAH_BROWSER_INGESTION") == "1":
        original_lifespan = app.router.lifespan_context

        async def consume() -> None:
            service = Ingestion(
                app.state.engine,
                settings,
                app.state.storage,
                Embeddings(settings, httpx.MockTransport(fixture.handle)),
            )
            while True:
                async with service.sessions() as db:
                    documents = (
                        await db.scalars(
                            select(Document)
                            .where(Document.status == "queued", Document.deleted_at.is_(None))
                            .limit(10)
                        )
                    ).all()
                for doc in documents:
                    await service.run(doc.organization_id, doc.id, doc.current_attempt_id)
                await asyncio.sleep(0.2)

        async def consume_runs() -> None:
            service_sessions = async_sessionmaker(app.state.engine, expire_on_commit=False)
            while True:
                async with service_sessions() as db:
                    rows = list(
                        await db.scalars(select(Run).where(Run.status == "queued").limit(4))
                    )
                for row in rows:
                    await QA(
                        RunState(app.state.engine, settings, row.organization_id, row.id),
                        app.state.model_probe,
                    ).run()
                await asyncio.sleep(0.2)

        @asynccontextmanager
        async def lifespan(instance: FastAPI) -> AsyncIterator[None]:
            async with original_lifespan(instance):
                consumer = asyncio.create_task(consume())
                runner = (
                    asyncio.create_task(consume_runs())
                    if os.getenv("EAH_BROWSER_QA") == "1"
                    else None
                )
                try:
                    yield
                finally:
                    consumer.cancel()
                    with suppress(asyncio.CancelledError):
                        await consumer
                    if runner:
                        runner.cancel()
                        with suppress(asyncio.CancelledError):
                            await runner

        app.router.lifespan_context = lifespan
    run_async(
        uvicorn.Server(uvicorn.Config(app, host=settings.api_host, port=settings.api_port)).serve()
    )


if __name__ == "__main__":
    main()
