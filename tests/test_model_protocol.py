import json

import httpx2
import pytest
from openai import AsyncOpenAI

from scripts.probe_external import probe_models


@pytest.mark.asyncio
async def test_chat_stream_usage_and_embedding_sdk_protocol() -> None:
    requests: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            assert body["stream_options"]["include_usage"] is True
            chunks = [
                {
                    "id": "probe",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "stub-chat",
                    "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}],
                },
                {
                    "id": "probe",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "stub-chat",
                    "choices": [],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
                },
            ]
            payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            return httpx2.Response(
                200,
                text=payload + "data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        assert request.url.path.endswith("/embeddings")
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": "stub-embedding",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )

    async with AsyncOpenAI(
        api_key="local-fixture",
        base_url="http://fixture/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
    ) as client:
        assert await probe_models(client, "stub-chat", "stub-embedding") == {
            "chatTokens": 6,
            "embeddingDimensions": 3,
        }
    assert requests == ["/v1/chat/completions", "/v1/embeddings"]


@pytest.mark.asyncio
async def test_sdk_parses_streamed_function_call() -> None:
    chunk = {
        "id": "probe",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "stub",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_probe",
                            "type": "function",
                            "function": {"name": "read_probe", "arguments": "{}"},
                        }
                    ]
                },
            }
        ],
    }
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(
            200,
            text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
    )
    async with AsyncOpenAI(
        api_key="fixture",
        base_url="http://fixture/v1",
        http_client=httpx2.AsyncClient(transport=transport),
    ) as client:
        stream = await client.chat.completions.create(
            model="stub",
            messages=[{"role": "user", "content": "probe"}],
            stream=True,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_probe",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
        async with stream:
            chunks = [item async for item in stream]
        calls = chunks[0].choices[0].delta.tool_calls
        assert calls and calls[0].function
        assert calls[0].function.name == "read_probe"
        assert json.loads(calls[0].function.arguments or "") == {}
