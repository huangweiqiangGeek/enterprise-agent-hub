"""Explicit subprocess-only fault fixture. Never imported by application entry points."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from test_runs import QAProvider

from backend.config import Settings
from backend.db import make_engine
from backend.runtime import run_async
from backend.services.model_probe import ModelProbe
from backend.services.qa import QA
from backend.services.run_state import RunState


async def main() -> None:
    url, org, run_id, phase, marker_name = sys.argv[1:]
    marker = Path(marker_name)

    async def pause(point: str) -> None:
        if phase != point or await asyncio.to_thread(marker.exists):
            return
        await asyncio.to_thread(marker.write_text, json.dumps({"phase": point}), encoding="utf-8")
        # A file gate lets the parent release a still-live stale worker after takeover.
        while not await asyncio.to_thread(marker.with_suffix(".release").exists):  # noqa: ASYNC110
            await asyncio.sleep(0.05)

    class FaultState(RunState):
        async def claim(self) -> Any:
            result = await super().claim()
            if result:
                await pause("claimed")
            return result

        async def finish_step(
            self,
            step_id: UUID,
            output: dict[str, Any],
            error: str | None = None,
            sources: Any = None,
        ) -> None:
            await super().finish_step(step_id, output, error, sources)
            if "sourceIds" in output:
                await pause("tool_saved")
            if "_blocks" in output:
                await pause("model_saved")

        async def delta(self, text: str, reset: bool = False) -> None:
            await super().delta(text, reset)
            if text:
                await pause("partial")

        async def terminal(
            self, result: Any = None, error: str | None = None, *, expired_only: bool = False
        ) -> bool:
            if result:
                await pause("before_terminal")
            outcome = await super().terminal(result, error, expired_only=expired_only)
            if result and outcome:
                await pause("after_terminal")
            return outcome

    class Provider(QAProvider):
        async def handle(self, request: httpx.Request) -> httpx.Response:
            result = await super().handle(request)
            if request.url.path.endswith("embeddings"):
                await pause("tool_unknown")
            return result

    settings = Settings(database_url=url)
    engine = make_engine(settings)
    try:
        await QA(
            FaultState(engine, settings, UUID(org), UUID(run_id)),
            ModelProbe(settings, httpx.MockTransport(Provider().handle)),
        ).run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    run_async(main())
