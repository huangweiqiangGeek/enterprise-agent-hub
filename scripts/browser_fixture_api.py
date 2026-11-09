"""Explicit isolated-browser fixture entry. Never used by the normal backend entry."""

import asyncio
import importlib.util
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select

from backend.api.app import create_app
from backend.config import Settings
from backend.models.knowledge import Document
from backend.rag.embeddings import Embeddings
from backend.runtime import run_async
from backend.services.ingestion import Ingestion
from backend.services.model_probe import ModelProbe


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
    app.state.model_probe = ModelProbe(settings, httpx.MockTransport(fixture.handle))
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

        @asynccontextmanager
        async def lifespan(instance: FastAPI) -> AsyncIterator[None]:
            async with original_lifespan(instance):
                consumer = asyncio.create_task(consume())
                try:
                    yield
                finally:
                    consumer.cancel()
                    with suppress(asyncio.CancelledError):
                        await consumer

        app.router.lifespan_context = lifespan
    run_async(
        uvicorn.Server(uvicorn.Config(app, host=settings.api_host, port=settings.api_port)).serve()
    )


if __name__ == "__main__":
    main()
