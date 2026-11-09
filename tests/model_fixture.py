"""Controlled provider protocol fixture; never a real-provider acceptance result."""

import asyncio
import json
from typing import Any

import httpx


class ProviderFixture:
    def __init__(self) -> None:
        self.mode = "ok"
        self.requests: list[dict[str, Any]] = []
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content)
        self.requests.append(value)
        if self.started and self.release:
            self.started.set()
            await self.release.wait()
        if self.mode == "auth":
            return httpx.Response(401, json={"error": request.headers["authorization"]})
        if self.mode == "redirect":
            return httpx.Response(307, headers={"location": "http://127.0.0.1:1/private"})
        if self.mode == "timeout":
            raise httpx.ReadTimeout("secret raw provider error")
        if self.mode == "invalid":
            return httpx.Response(200, json={"secret": request.headers["authorization"]})
        if request.url.path.endswith("embeddings"):
            dimension = value.get("dimensions", 3)
            if self.mode == "dimension":
                dimension += 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [0.1] * dimension}
                        for i in range(
                            len(value["input"]) if isinstance(value["input"], list) else 1
                        )
                    ],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
        if request.url.path.endswith("rerank"):
            top = value.get("top_n", value.get("parameters", {}).get("top_n", 2))
            results = [{"index": i, "relevance_score": 0.9 / (i + 1)} for i in range(top)]
            if request.url.path.endswith("text-rerank/text-rerank"):
                return httpx.Response(
                    200,
                    json={
                        "output": {"results": results},
                        "usage": {"total_tokens": 19},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": results,
                    "meta": {"billed_units": {"search_units": 1}},
                },
            )
        usage = (
            None
            if self.mode == "no_usage"
            else {"prompt_tokens": 80, "completion_tokens": 8, "total_tokens": 88}
        )
        if value.get("stream"):
            chunks = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"name": "eah_probe", "arguments": '{"ok":'},
                                    }
                                ]
                            },
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": "true}"}}]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                {"choices": [], "usage": usage},
            ]
            stream = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            if self.mode != "truncated":
                stream += "data: [DONE]\n\n"
            return httpx.Response(
                200, content=stream, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"content": '{"ok":true}'}}
                ],
                "usage": usage,
            },
        )
